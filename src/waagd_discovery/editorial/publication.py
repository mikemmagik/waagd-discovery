"""Produce validated PublicationIntent v1 data for the separately trusted site.

The module is deliberately data-only: it owns neither site credentials nor a
network client.  An automatic intent is possible only when a host-controlled
PublicationPolicy opts in; a model decision never grants this authority.
"""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import (
    EditorialError,
    validate_decision,
    validate_verification,
)
from waagd_discovery.editorial.publication_policy import PublicationPolicy
from waagd_discovery.editorial.store import EditorialStore

_MAXIMUM = 256 * 1024
_LIMIT = 100


def _canonical(value: dict[str, Any]) -> bytes:
    """Match the website publisher's sorted-key UTF-8 payload hash exactly."""
    try:
        return json.dumps(
            value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise EditorialError("publication_payload_invalid") from None


def _source_item_id(intent_id: str) -> str:
    return "waagd:" + hashlib.sha256(intent_id.encode("ascii")).hexdigest()


def _valid_text(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _timestamp(value: Any) -> str:
    """Return the exact UTC-millisecond format required by the site consumer."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (AttributeError, TypeError, ValueError):
        raise EditorialError("publication_payload_invalid") from None


def _delivery_id(value: Any) -> str:
    try:
        parsed = uuid.UUID(value)
        if not isinstance(value, str) or parsed.version != 4 or str(parsed) != value:
            raise ValueError
        return value
    except (AttributeError, TypeError, ValueError):
        raise EditorialError("publication_payload_invalid") from None


def _payload(
    candidate: dict[str, Any],
    verification: dict[str, Any],
    decision: dict[str, Any],
    delivery_id: str,
    revision: int,
    policy: PublicationPolicy,
) -> dict[str, Any]:
    """Make one stable wire intent after all non-model safety gates pass."""
    key = candidate["candidate_key"]
    intent_id = f"auto:{key}:{revision}"
    evidence = {entry["id"]: entry for entry in verification["evidence"]}
    evidence_ids = decision["verification_evidence"]
    publisher = candidate.get("publisher") or verification.get("publisher")
    published_at_source = _timestamp(candidate["published_at"])
    if (
        decision["decision"] != "READY_FOR_REVIEW"
        or verification["verification_status"] != "VERIFIED"
        or decision["unverified_claims"]
        or decision["open_questions"]
        or decision["risks"]
        or not evidence_ids
        or len(evidence_ids) > 100
        or any(item not in evidence for item in evidence_ids)
        or not _delivery_id(delivery_id)
        or not _valid_text(publisher, 100)
        or not policy.permits(published_at_source)
    ):
        raise EditorialError("publication_not_eligible")

    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in evidence_ids:
        url = evidence[item]["url"]
        if url not in seen:
            references.append({"url": url, "title": "Verified evidence"})
            seen.add(url)
    post = {
        "type": "news",
        "origin": "auto",
        "url": candidate["url"],
        "canonical_url": candidate["url"],
        "source_item_id": _source_item_id(intent_id),
        "title": decision["headline"],
        "source": "WAAGD",
        "preview": None,
        "embed": None,
        "body": decision["article"],
        "references": references,
    }
    if (
        not _valid_text(post["title"], 300)
        or not _valid_text(post["source"], 100)
        or not _valid_text(post["body"], 100000)
        or not isinstance(post["url"], str)
        or not post["url"].startswith(("http://", "https://"))
        or len(post["url"]) > 2048
        or not _valid_text(post["source_item_id"], 255)
        or len(post["references"]) > 100
        or len(_canonical(post)) > _MAXIMUM
    ):
        raise EditorialError("publication_payload_invalid")
    return {
        "version": 1,
        "intent_id": intent_id,
        "origin": "waagd-discovery",
        "authorization": {"kind": "trusted_publication_policy", "automatic": True},
        "post": post,
        "candidate_key": key,
        "delivery_id": _delivery_id(delivery_id),
        "decision_revision": revision,
        "verification_status": "VERIFIED",
        "editorial_status": "READY_FOR_REVIEW",
        "publisher": publisher,
        "published_at_source": published_at_source,
        "risks": [],
        "content_hash": hashlib.sha256(_canonical(post)).hexdigest(),
        "publication_policy_version": policy.version,
        "evidence_ids": evidence_ids,
    }


def exportable(
    store: EditorialStore, limit: int = _LIMIT, *, policy: PublicationPolicy | None = None
) -> list[dict[str, Any]]:
    """Return deterministic, eligible intents only for an explicitly enabled policy."""
    if store.simulation:
        raise EditorialError("publication_simulation_forbidden")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _LIMIT:
        raise EditorialError("publication_limit_invalid")
    trusted_policy = policy or PublicationPolicy()
    rows = store.connection.execute(
        "SELECT c.candidate_key,c.current_revision,d.request,d.result AS decision_result,"
        "v.result AS verification_result,dc.delivery_id FROM candidates c "
        "JOIN decisions d ON d.candidate_key=c.candidate_key AND d.revision=c.current_revision "
        "JOIN verifications v ON v.candidate_key=c.candidate_key AND v.revision=c.current_revision "
        "JOIN (SELECT candidate_key,revision,MIN(delivery_id) AS delivery_id "
        "FROM delivery_candidates GROUP BY candidate_key,revision) dc "
        "ON dc.candidate_key=c.candidate_key AND dc.revision=c.current_revision "
        "WHERE c.current_revision > 0 ORDER BY c.candidate_key LIMIT ?",
        (limit,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            request = io.decode(row["request"].encode())
            decision = io.decode(row["decision_result"].encode())
            verification = io.decode(row["verification_result"].encode())
            candidate = request["candidate"]
            revision = row["current_revision"]
            if (
                not isinstance(row["candidate_key"], str)
                or candidate["candidate_key"] != row["candidate_key"]
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise EditorialError("publication_store_invalid")
            validate_verification(verification, candidate["candidate_key"])
            validate_decision(decision, request)
            if decision["decision"] == "READY_FOR_REVIEW":
                try:
                    results.append(
                        _payload(
                            candidate,
                            verification,
                            decision,
                            row["delivery_id"],
                            revision,
                            trusted_policy,
                        )
                    )
                except EditorialError as error:
                    if str(error) != "publication_not_eligible":
                        raise
        except (EditorialError, io.BridgeError, KeyError, TypeError, ValueError):
            raise EditorialError("publication_store_invalid") from None
    return results


def validate_publication_batch(value: Any) -> None:
    """Validate the shared v1 wire schema and the integrity binding of each post."""
    try:
        if not isinstance(value, dict) or set(value) != {"version", "producer", "intents"}:
            raise EditorialError("publication_schema_invalid")
        if value["version"] != 1 or value["producer"] != "waagd-discovery":
            raise EditorialError("publication_schema_invalid")
        if not isinstance(value["intents"], list) or len(value["intents"]) > _LIMIT:
            raise EditorialError("publication_schema_invalid")
        schema_path = files("waagd_discovery").joinpath("publication-intent-v1.schema.json")
        schema = json.loads(schema_path.read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        ids: set[str] = set()
        for intent in value["intents"]:
            validator.validate(intent)
            if intent["intent_id"] in ids or not isinstance(intent["post"], dict):
                raise EditorialError("publication_schema_invalid")
            ids.add(intent["intent_id"])
            post = intent["post"]
            if (
                post.get("type") != "news"
                or post.get("origin") != "auto"
                or not _valid_text(post.get("url"), 2048)
                or not _valid_text(post.get("body"), 100000)
                or post.get("source_item_id") != _source_item_id(intent["intent_id"])
                or hashlib.sha256(_canonical(post)).hexdigest() != intent["content_hash"]
            ):
                raise EditorialError("publication_schema_invalid")
    except (EditorialError, ValidationError, TypeError, ValueError, OSError, RecursionError):
        raise EditorialError("publication_schema_invalid") from None


def export_file(
    store_path: Path,
    output: Path,
    limit: int = _LIMIT,
    *,
    policy: PublicationPolicy | None = None,
) -> dict[str, Any]:
    """Atomically write one inspectable PublicationIntent v1 batch."""
    io.path_parts(store_path)
    io.path_parts(output)
    if not output.name or output.suffix != ".json" or output.parent == Path("/"):
        raise EditorialError("publication_output_invalid")
    try:
        with EditorialStore(store_path, simulation=False, readonly=True) as store:
            intents = exportable(store, limit, policy=policy)
    except EditorialError as error:
        if str(error) != "editorial_simulation_mismatch":
            raise
        try:
            with EditorialStore(store_path, simulation=True, readonly=True) as store:
                exportable(store, limit, policy=policy)
        except EditorialError as simulation:
            if str(simulation) == "publication_simulation_forbidden":
                raise
            raise EditorialError("publication_store_invalid") from None
        raise EditorialError("publication_store_invalid") from None
    result = {"version": 1, "producer": "waagd-discovery", "intents": intents}
    validate_publication_batch(result)
    encoded = io.encode(result)
    if len(encoded) > _MAXIMUM:
        raise EditorialError("publication_export_too_large")
    directory = io.open_directory(output.parent, create=True, private=True)
    try:
        io.atomic_write(directory, output.name, encoded)
    finally:
        os.close(directory)
    return {"status": "exported", "count": len(intents), "path": output.name}
