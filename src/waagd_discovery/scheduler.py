"""Explicit, synchronous fixed-delay discovery scheduler; no background activation."""

import fcntl
import json
import logging
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml

from waagd_discovery.locking import database_lock
from waagd_discovery.logging import event
from waagd_discovery.models import DiscoveryError
from waagd_discovery.storage import Repository


@dataclass(frozen=True)
class SchedulerPolicy:
    interval_seconds: float = 1800
    heartbeat_seconds: float = 15

    def __post_init__(self) -> None:
        for field, minimum, maximum in (
            ("interval_seconds", 1, 86400),
            ("heartbeat_seconds", 1, 60),
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DiscoveryError("scheduler_policy_invalid")
            try:
                normalized = float(value)
            except (ValueError, OverflowError) as error:
                raise DiscoveryError("scheduler_policy_invalid") from error
            if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
                raise DiscoveryError("scheduler_policy_invalid")
            object.__setattr__(self, field, normalized)
        if self.heartbeat_seconds > self.interval_seconds:
            raise DiscoveryError("scheduler_policy_invalid")


class _PolicyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _PolicyLoader, node: yaml.MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)  # type: ignore[no-untyped-call]
        if not isinstance(key, str) or key in result:
            raise DiscoveryError("scheduler_policy_invalid")
        result[key] = loader.construct_object(value_node)  # type: ignore[no-untyped-call]
    return result


_PolicyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_scheduler_policy(path: Path) -> SchedulerPolicy:
    try:
        with path.open("rb") as handle:
            raw = handle.read(65_537)
        if len(raw) > 65_536:
            raise DiscoveryError("scheduler_policy_too_large")
        data = yaml.load(raw.decode("utf-8"), Loader=_PolicyLoader)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError) as error:
        raise DiscoveryError("scheduler_policy_unreadable") from error
    if not isinstance(data, dict) or set(data) != {"scheduler"}:
        raise DiscoveryError("scheduler_policy_invalid")
    values = data["scheduler"]
    if not isinstance(values, dict) or set(values) - {"interval_seconds", "heartbeat_seconds"}:
        raise DiscoveryError("scheduler_policy_invalid")
    return SchedulerPolicy(**values)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _cycle(db: Path, sources_path: Path, policy_path: Path) -> dict[str, object]:
    # The same authoritative operation is used by the manual CLI.
    from waagd_discovery.collection import run_once

    return run_once(db, sources_path, policy_path)


def _never_stop() -> bool:
    return False


@dataclass(frozen=True)
class SchedulerRuntime:
    """Injectable clocks and synchronous boundaries for deterministic offline tests."""

    utc_now: Callable[[], datetime] = _utc_now
    monotonic: Callable[[], float] = time.monotonic
    wait: Callable[[float], None] = time.sleep
    cycle: Callable[[Path, Path, Path], dict[str, object]] = _cycle
    should_stop: Callable[[], bool] = _never_stop


@contextmanager
def _lifetime_lock(db: Path) -> Iterator[None]:
    db.parent.mkdir(parents=True, exist_ok=True)
    with db.with_name(db.name + ".scheduler.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DiscoveryError("scheduler_already_running") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler_clock_not_utc_aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _persist(db: Path, state: dict[str, object]) -> bool:
    """Short writes only; never recover or alter a manual operation's live run."""
    try:
        with database_lock(db), Repository(db) as repository:
            connection = repository.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                # A busy startup may not have loaded the previous success yet.
                # Merge under the same lock as the write, including after restart.
                row = connection.execute(
                    "SELECT state_json FROM service_state WHERE id = 1"
                ).fetchone()
                if state["last_success_at"] is None and row is not None:
                    try:
                        previous = json.loads(row[0])
                    except (ValueError, TypeError):
                        previous = None
                    if isinstance(previous, dict):
                        last_success = previous.get("last_success_at")
                        if isinstance(last_success, str):
                            try:
                                state["last_success_at"] = _stamp(
                                    datetime.fromisoformat(last_success)
                                )
                            except ValueError:
                                pass
                connection.execute(
                    "INSERT INTO service_state(id, state_json) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json",
                    (json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False),),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return True
    except DiscoveryError as error:
        if error.code != "database_busy":
            raise
        event("scheduler_state_busy", level=logging.WARNING)
    except (sqlite3.Error, OSError):
        event("scheduler_state_unavailable", level=logging.WARNING)
    return False


def _safe_error(error: Exception) -> str:
    if isinstance(error, DiscoveryError) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error.code):
        return error.code
    return "scheduler_cycle_failed"


def _wait_until_due(
    db: Path,
    state: dict[str, object],
    policy: SchedulerPolicy,
    runtime: SchedulerRuntime,
    wall_deadline: datetime,
    monotonic_deadline: float,
) -> None:
    while not runtime.should_stop():
        wall_remaining = (wall_deadline - runtime.utc_now()).total_seconds()
        remaining = min(wall_remaining, monotonic_deadline - runtime.monotonic())
        if remaining <= 0:
            return
        runtime.wait(min(policy.heartbeat_seconds, remaining))
        state["heartbeat_at"] = _stamp(runtime.utc_now())
        _persist(db, state)


def run_scheduler(
    db: Path,
    sources_path: Path,
    policy_path: Path,
    scheduler_policy: SchedulerPolicy,
    *,
    runtime: SchedulerRuntime | None = None,
) -> dict[str, object]:
    """Run until interrupted; lifetime lock prevents duplicate scheduler processes.

    The first cycle is immediate. Every outcome, including a busy database or an
    exception, is followed by a fresh fixed delay. Heartbeats run while waiting;
    collection stays synchronous and owns its own ordinary operation lock.
    """
    runtime = runtime or SchedulerRuntime()
    db = db.resolve()
    with _lifetime_lock(db):
        now = _stamp(runtime.utc_now())
        state: dict[str, object] = {
            "session_id": str(uuid4()),
            "status": "starting",
            "started_at": now,
            "heartbeat_at": now,
            "last_cycle_at": None,
            "last_cycle_finished_at": None,
            "last_success_at": None,
            "next_run_at": now,
            "consecutive_failures": 0,
            "last_result": None,
            "last_error": None,
        }
        try:
            _persist(db, state)
            while not runtime.should_stop():
                now = _stamp(runtime.utc_now())
                state.update(
                    status="collecting", heartbeat_at=now, last_cycle_at=now, next_run_at=None
                )
                _persist(db, state)
                try:
                    result = runtime.cycle(db, sources_path, policy_path)
                    state["last_result"] = result
                    status = result.get("status")
                    if status == "interrupted":
                        state["last_error"] = "collection_interrupted"
                        state["last_cycle_finished_at"] = _stamp(runtime.utc_now())
                        event("scheduler_cycle_finished", status="interrupted")
                        break
                    if status == "success":
                        state["last_success_at"] = _stamp(runtime.utc_now())
                        state["consecutive_failures"] = 0
                        state["last_error"] = None
                    else:
                        failures = state["consecutive_failures"]
                        assert isinstance(failures, int)
                        state["consecutive_failures"] = failures + 1
                        state["last_error"] = (
                            "collection_partial" if status == "partial" else "collection_failed"
                        )
                    event(
                        "scheduler_cycle_finished",
                        status=status if status in {"success", "partial", "failed"} else "unknown",
                    )
                except Exception as error:
                    code = _safe_error(error)
                    failures = state["consecutive_failures"]
                    assert isinstance(failures, int)
                    state["consecutive_failures"] = failures + 1
                    state["last_result"] = None
                    state["last_error"] = code
                    event("scheduler_cycle_failed", level=logging.WARNING, error=code)
                finished = runtime.utc_now()
                wall_deadline = finished + timedelta(seconds=scheduler_policy.interval_seconds)
                monotonic_deadline = runtime.monotonic() + scheduler_policy.interval_seconds
                state.update(
                    status="waiting",
                    heartbeat_at=_stamp(finished),
                    last_cycle_finished_at=_stamp(finished),
                    next_run_at=_stamp(wall_deadline),
                )
                _persist(db, state)
                _wait_until_due(
                    db, state, scheduler_policy, runtime, wall_deadline, monotonic_deadline
                )
        except KeyboardInterrupt:
            if state["status"] == "collecting":
                state["last_error"] = "collection_interrupted"
                state["last_cycle_finished_at"] = _stamp(runtime.utc_now())
        finally:
            state.update(status="stopped", next_run_at=None, heartbeat_at=_stamp(runtime.utc_now()))
            _persist(db, state)
        return state
