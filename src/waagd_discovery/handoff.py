"""Opt-in automatic data export, isolated from collection and scheduler health."""

import json
import math
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import jsonschema
import yaml

from waagd_discovery.export import export_candidates, write_json_atomic
from waagd_discovery.filtering import FilterPolicy
from waagd_discovery.models import DiscoveryError
from waagd_discovery.storage import Repository


@dataclass(frozen=True)
class HandoffPolicy:
    enabled: bool = False
    output: Path = Path("/exports/candidates.json")
    limit: int = 50

    def __post_init__(self) -> None:
        if (
            not isinstance(self.enabled, bool)
            or not isinstance(self.output, Path)
            or self.output.suffix.lower() != ".json"
            or isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 500
        ):
            raise DiscoveryError("handoff_config_invalid")


class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader: _Loader, node: yaml.MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)  # type: ignore[no-untyped-call]
        if not isinstance(key, str) or key in result:
            raise DiscoveryError("handoff_config_invalid")
        result[key] = loader.construct_object(value_node)  # type: ignore[no-untyped-call]
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_handoff_policy(path: Path) -> HandoffPolicy:
    try:
        with path.open("rb") as handle:
            raw = handle.read(65_537)
        if len(raw) > 65_536:
            raise DiscoveryError("handoff_config_too_large")
        data = yaml.load(raw.decode("utf-8"), Loader=_Loader)
        if not isinstance(data, dict) or set(data) != {"handoff"}:
            raise DiscoveryError("handoff_config_invalid")
        values = data["handoff"]
        if not isinstance(values, dict) or set(values) - {"enabled", "output", "limit"}:
            raise DiscoveryError("handoff_config_invalid")
        if "output" in values:
            if not isinstance(values["output"], str) or not values["output"].strip():
                raise DiscoveryError("handoff_config_invalid")
            values["output"] = Path(values["output"])
        return HandoffPolicy(**values)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError) as error:
        raise DiscoveryError("handoff_config_unreadable") from error


def validate_target(db: Path, output: Path, protected: tuple[Path, ...]) -> None:
    canonical = db.resolve()
    forbidden = {canonical, *(p.resolve() for p in protected)}
    for spelling in (db, canonical):
        forbidden.update(
            Path(str(spelling) + suffix).resolve()
            for suffix in ("-wal", "-shm", ".lock", ".scheduler.lock")
        )
    if output.resolve() in forbidden or canonical.parent in output.resolve().parents:
        raise DiscoveryError("handoff_target_protected")
    # This directory belongs to the trusted export producer, never the consumer.
    for path in (output, *output.absolute().parents):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise DiscoveryError("handoff_target_unsafe")
        if path == output and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise DiscoveryError("handoff_target_unsafe")


def publish(
    repository: Repository,
    db: Path,
    config: Path,
    policy: FilterPolicy,
    protected: tuple[Path, ...],
) -> dict[str, object]:
    handoff = load_handoff_policy(config)
    if not handoff.enabled:
        return {"status": "disabled"}
    validate_target(db, handoff.output, (config, *protected))
    count = export_candidates(repository, handoff.output, handoff.limit, policy=policy)
    return {"status": "exported", "candidates": count}


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_json_number")
    return number


def _read_json(path: Path, maximum: int) -> dict[str, object]:
    for component in (path, *path.absolute().parents):
        if component.is_symlink():
            raise DiscoveryError("handoff_export_unsafe")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DiscoveryError("handoff_export_unsafe")
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
        current = path.lstat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise DiscoveryError("handoff_export_changed")
    if len(raw) > maximum:
        raise DiscoveryError("handoff_export_too_large")

    def constant(value: str) -> object:
        raise ValueError("nonfinite_json_constant")

    data = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_pairs, parse_float=_finite, parse_constant=constant
    )
    if not isinstance(data, dict):
        raise ValueError("json_not_object")
    return data


def record_attempt(db: Path, result: dict[str, object], collection: dict[str, object]) -> bool:
    """Best-effort diagnostics in the private data directory, never ingestion rollback."""
    path = Path(str(db.resolve()) + ".handoff.json")
    previous: dict[str, object] = {}
    try:
        previous = _read_json(path, 16_384)
    except (OSError, ValueError, RecursionError, DiscoveryError):
        pass
    now = datetime.now(UTC).isoformat()
    last_success = now if result["status"] == "exported" else previous.get("last_success_at")
    state = {
        **result,
        "finished_at": now,
        "last_success_at": last_success,
        "collection_run_id": collection.get("run_id"),
    }
    try:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return False
        write_json_atomic(path, state)
        return True
    except (OSError, ValueError):
        return False


def integration_status(db: Path, config: Path) -> dict[str, object]:
    """Inspect only bounded JSON, without SQLite access or directory creation."""
    policy = load_handoff_policy(config)
    if not policy.enabled:
        return {"enabled": False, "status": "disabled", "healthy": True}
    attempt: dict[str, object] | None = None
    try:
        attempt = _read_json(Path(str(db.resolve()) + ".handoff.json"), 16_384)
    except FileNotFoundError:
        pass
    except (DiscoveryError, OSError, ValueError, RecursionError):
        return {
            "enabled": True,
            "status": "error",
            "healthy": False,
            "code": "handoff_state_unavailable",
            "last_attempt": None,
        }
    try:
        data = _read_json(policy.output, 8 * 1024 * 1024)
        schema = json.loads(
            files("waagd_discovery").joinpath("candidate-v2.schema.json").read_text()
        )
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
            data
        )
        generated_at = data["generated_at"]
        assert isinstance(generated_at, str)
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        age = (datetime.now(UTC) - generated).total_seconds()
        status = "current" if -300 <= age <= 7200 else "stale" if age > 7200 else "future"
        candidates = data["candidates"]
        assert isinstance(candidates, list)
        return {
            "enabled": True,
            "status": status,
            "healthy": status == "current"
            and (attempt is None or attempt.get("status") == "exported"),
            "generated_at": generated.isoformat(),
            "age_seconds": round(age, 3),
            "candidates": len(candidates),
            "limit": policy.limit,
            "last_attempt": attempt,
        }
    except DiscoveryError as error:
        return {
            "enabled": True,
            "status": "error",
            "healthy": False,
            "code": error.code,
            "last_attempt": attempt,
        }
    except (OSError, ValueError, RecursionError, jsonschema.ValidationError):
        return {
            "enabled": True,
            "status": "error",
            "healthy": False,
            "code": "handoff_export_unavailable",
            "last_attempt": attempt,
        }
