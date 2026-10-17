"""Deterministic, auditable discovery quality/freshness rules; no editorial scoring."""

import hashlib
import heapq
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import yaml

from waagd_discovery.models import DiscoveryError
from waagd_discovery.normalization import normalize_url
from waagd_discovery.storage import Repository
from waagd_discovery.storage.filter_state import (
    FilterRecord,
    batch_summary,
    input_records,
    insert_batch,
    insert_decision,
    latest_batch,
    transaction,
)

ENGINE_VERSION = "rules-v1"


@dataclass(frozen=True)
class FilterPolicy:
    max_age_hours: float = 72
    future_tolerance_minutes: float = 15
    min_description_chars: int = 40
    require_publisher: bool = False

    def __post_init__(self) -> None:
        for name in ("max_age_hours", "future_tolerance_minutes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DiscoveryError("filter_policy_invalid")
            try:
                normalized = float(value)
            except (ValueError, OverflowError) as error:
                raise DiscoveryError("filter_policy_invalid") from error
            if not math.isfinite(normalized) or normalized < 0:
                raise DiscoveryError("filter_policy_invalid")
            if name == "max_age_hours" and normalized == 0:
                raise DiscoveryError("filter_policy_invalid")
            object.__setattr__(self, name, normalized)
        if (
            isinstance(self.min_description_chars, bool)
            or not isinstance(self.min_description_chars, int)
            or self.min_description_chars < 0
            or not isinstance(self.require_publisher, bool)
        ):
            raise DiscoveryError("filter_policy_invalid")


class _PolicyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _PolicyLoader, node: yaml.MappingNode) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)  # type: ignore[no-untyped-call]
        if not isinstance(key, str) or key in result:
            raise DiscoveryError("filter_policy_invalid")
        result[key] = loader.construct_object(value_node)  # type: ignore[no-untyped-call]
    return result


_PolicyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_policy(path: Path) -> FilterPolicy:
    try:
        with path.open("rb") as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise DiscoveryError("filter_policy_too_large")
        data = yaml.load(raw.decode("utf-8"), Loader=_PolicyLoader)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError) as error:
        raise DiscoveryError("filter_policy_unreadable") from error
    if not isinstance(data, dict) or set(data) != {"filter"}:
        raise DiscoveryError("filter_policy_invalid")
    values = data["filter"]
    if not isinstance(values, dict) or set(values) - set(asdict(FilterPolicy())):
        raise DiscoveryError("filter_policy_invalid")
    return FilterPolicy(**values)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _policy_hash(policy: FilterPolicy) -> str:
    return hashlib.sha256(_json(asdict(policy)).encode("utf-8")).hexdigest()


def _reference(as_of: datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(UTC)
    try:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError
        return as_of.astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise DiscoveryError("filter_reference_time_invalid") from error


def _fingerprint(record: FilterRecord) -> str:
    quality = {
        key: record.candidate[key]
        for key in ("id", "url", "title", "description", "publisher", "published_at", "date_status")
    }
    quality["sources"] = [asdict(source) for source in record.sources]
    # Discovery/replay timestamps are not quality inputs. A 200 with identical
    # metadata may change last_seen_at without invalidating the persisted rules.
    return hashlib.sha256(_json(quality).encode("utf-8")).hexdigest()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _published(candidate: dict[str, object]) -> datetime | None:
    value = candidate["published_at"]
    if candidate["date_status"] != "valid" or not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            return None
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class _Decision:
    status: str
    reasons: list[str]
    checks: dict[str, bool]
    completeness: int
    published_at: datetime | None


def _evaluate(record: FilterRecord, policy: FilterPolicy, reference: datetime) -> _Decision:
    candidate = record.candidate
    url = candidate["url"]
    try:
        canonical_url = isinstance(url, str) and normalize_url(url) == url
    except ValueError:
        canonical_url = False
    description = candidate["description"]
    description_length = len(description.strip()) if isinstance(description, str) else 0
    published = _published(candidate)
    age_hours = (reference - published).total_seconds() / 3600 if published else None
    future_minutes = (published - reference).total_seconds() / 60 if published else None
    checks = {
        "canonical_url": canonical_url,
        "title": _nonempty(candidate["title"]),
        "enabled_source": any(source.enabled for source in record.sources),
        "description": description_length >= policy.min_description_chars,
        "publisher": not policy.require_publisher or _nonempty(candidate["publisher"]),
        "publication_date": published is not None,
        "future_tolerance": (
            future_minutes is not None and future_minutes <= policy.future_tolerance_minutes
        ),
        "freshness": age_hours is not None and age_hours <= policy.max_age_hours,
    }
    failed_reasons = {
        "canonical_url": "canonical_url_invalid",
        "title": "title_missing",
        "enabled_source": "enabled_source_missing",
        "description": "description_too_short",
        "publisher": "publisher_missing",
        "publication_date": (
            "publication_date_missing"
            if candidate["date_status"] == "missing"
            else "publication_date_invalid"
        ),
        "future_tolerance": "publication_in_future",
        "freshness": "publication_too_old",
    }
    reasons = [
        failed_reasons[name]
        for name, passed in checks.items()
        if not passed and (published is not None or name not in {"future_tolerance", "freshness"})
    ]
    if not checks["enabled_source"] or (published is not None and not checks["freshness"]):
        status = "excluded"
    elif all(checks.values()):
        status = "eligible"
    else:
        status = "deferred"
    completeness = sum(
        _nonempty(candidate[key]) for key in ("title", "description", "publisher", "published_at")
    )
    return _Decision(status, reasons, checks, completeness, published)


def assess(
    repository: Repository, policy: FilterPolicy, as_of: datetime | None = None
) -> dict[str, object]:
    reference = _reference(as_of)
    policy_hash = _policy_hash(policy)
    batch_id = str(uuid4())
    connection = repository.connection
    with transaction(connection, write=True):
        assessed_at = datetime.now(UTC).isoformat()
        counts = {"eligible": 0, "deferred": 0, "excluded": 0}
        for record in input_records(connection):
            decision = _evaluate(record, policy, reference)
            counts[decision.status] += 1
            insert_decision(
                connection,
                batch_id,
                cast(int, record.candidate["id"]),
                _fingerprint(record),
                decision.status,
                decision.reasons,
                decision.checks,
                decision.completeness,
            )
        insert_batch(
            connection,
            batch_id,
            policy_hash,
            _json(asdict(policy)),
            ENGINE_VERSION,
            reference.isoformat(),
            assessed_at,
            counts,
        )
        row = latest_batch(connection)
        assert row is not None and row["id"] == batch_id
        summary = batch_summary(row)
    return summary


def _quality(decision: _Decision, article_id: int) -> tuple[int, int, int]:
    published = decision.published_at
    assert published is not None
    microseconds = (
        published.toordinal() * 86400
        + published.hour * 3600
        + published.minute * 60
        + published.second
    ) * 1_000_000 + published.microsecond
    return microseconds, decision.completeness, -article_id


def select_candidates(
    repository: Repository,
    policy: FilterPolicy,
    as_of: datetime | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise DiscoveryError("filter_limit_invalid")
    reference = _reference(as_of)
    policy_hash = _policy_hash(policy)
    counts = {"eligible": 0, "deferred": 0, "excluded": 0, "unassessed": 0, "stale": 0}
    best: list[tuple[tuple[int, int, int], dict[str, object]]] = []
    connection = repository.connection
    with transaction(connection, write=False):
        batch = latest_batch(connection)
        batch_id = batch["id"] if batch is not None else None
        assessment_status = "current"
        if batch is None:
            assessment_status = "unassessed"
        elif batch["policy_hash"] != policy_hash:
            assessment_status = "policy_mismatch"
        elif batch["engine_version"] != ENGINE_VERSION:
            assessment_status = "engine_mismatch"
        for record in input_records(connection, batch_id):
            if record.persisted_status is None:
                counts["unassessed"] += 1
                continue
            if assessment_status != "current" or record.persisted_fingerprint != _fingerprint(
                record
            ):
                counts["stale"] += 1
                continue
            if record.persisted_status != "eligible":
                counts[record.persisted_status] += 1
                continue
            decision = _evaluate(record, policy, reference)
            counts[decision.status] += 1
            if decision.status != "eligible" or limit == 0:
                continue
            candidate = record.candidate
            candidate["selection"] = {
                "status": "eligible",
                "assessment_run_id": batch_id,
                "policy_hash": policy_hash,
                "reasons": decision.reasons,
                "checks": decision.checks,
            }
            entry = (_quality(decision, cast(int, candidate["id"])), candidate)
            if len(best) < limit:
                heapq.heappush(best, entry)
            elif entry[0] > best[0][0]:
                heapq.heapreplace(best, entry)
        context: dict[str, object] = {
            "policy_hash": policy_hash,
            "policy": asdict(policy),
            "engine_version": ENGINE_VERSION,
            "reference_time": reference.isoformat(),
            "assessment_run_id": batch_id,
            "assessed_at": batch["assessed_at"] if batch is not None else None,
            "assessment_status": assessment_status,
            "counts": counts,
        }
    return [
        candidate for _, candidate in sorted(best, key=lambda entry: entry[0], reverse=True)
    ], context


def filter_report(repository: Repository, limit: int = 100) -> dict[str, object]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 2**63 - 1:
        raise DiscoveryError("filter_limit_invalid")
    connection = repository.connection
    with transaction(connection, write=False):
        batch = latest_batch(connection)
        decisions: list[dict[str, object]] = []
        if batch is not None:
            rows = connection.execute(
                """SELECT article_id, input_fingerprint, status, reasons_json, checks_json
                FROM filter_decisions WHERE batch_id = ? ORDER BY article_id ASC LIMIT ?""",
                (batch["id"], limit),
            )
            decisions = [
                {
                    "article_id": row["article_id"],
                    "status": row["status"],
                    "input_fingerprint": row["input_fingerprint"],
                    "reasons": json.loads(row["reasons_json"]),
                    "checks": json.loads(row["checks_json"]),
                }
                for row in rows
            ]
        return {
            "assessment": batch_summary(batch) if batch is not None else None,
            "decisions": decisions,
            "limit": limit,
            "truncated": batch is not None and batch["articles_evaluated"] > len(decisions),
        }
