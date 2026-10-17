"""Offline scheduler clocks, locking, durability and shutdown contracts."""

import fcntl
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from waagd_discovery.locking import database_lock
from waagd_discovery.models import DiscoveryError
from waagd_discovery.scheduler import (
    SchedulerPolicy,
    SchedulerRuntime,
    load_scheduler_policy,
    run_scheduler,
)
from waagd_discovery.storage import Repository


@dataclass
class Clock:
    wall: datetime = datetime(2026, 10, 18, tzinfo=UTC)
    mono: float = 0
    calls: int = 0
    max_calls: int = 1
    waits: list[float] = field(default_factory=list)
    starts: list[float] = field(default_factory=list)
    jumps: list[float] = field(default_factory=list)
    duration: float = 0
    results: list[object] = field(default_factory=list)

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)
        if self.jumps:
            self.wall += timedelta(seconds=self.jumps.pop(0))

    def cycle(self, db: Path, sources: Path, policy: Path) -> dict[str, object]:
        self.starts.append(self.mono)
        self.calls += 1
        self.mono += self.duration
        self.wall += timedelta(seconds=self.duration)
        result = self.results.pop(0) if self.results else {"status": "success"}
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, dict)
        return result

    def runtime(self) -> SchedulerRuntime:
        return SchedulerRuntime(
            utc_now=lambda: self.wall,
            monotonic=lambda: self.mono,
            wait=self.wait,
            cycle=self.cycle,
            should_stop=lambda: self.calls >= self.max_calls,
        )


def state(db: Path) -> dict[str, object]:
    with Repository(db) as repository:
        row = repository.connection.execute(
            "SELECT state_json FROM service_state WHERE id=1"
        ).fetchone()
        result = json.loads(row[0])
        assert isinstance(result, dict)
        return result


def execute(db: Path, clock: Clock) -> dict[str, object]:
    return run_scheduler(
        db,
        Path("sources.yaml"),
        Path("filter.yaml"),
        SchedulerPolicy(10, 2),
        runtime=clock.runtime(),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("interval_seconds", 0),
        ("interval_seconds", 86401),
        ("interval_seconds", True),
        ("interval_seconds", "10"),
        ("interval_seconds", float("nan")),
        ("interval_seconds", float("inf")),
        ("interval_seconds", 10**1000),
        ("heartbeat_seconds", 0),
        ("heartbeat_seconds", 61),
        ("heartbeat_seconds", False),
        ("heartbeat_seconds", None),
        ("heartbeat_seconds", float("-inf")),
    ],
)
def test_invalid_policy(field: str, value: object) -> None:
    with pytest.raises(DiscoveryError, match="scheduler_policy_invalid"):
        SchedulerPolicy(**{field: value})  # type: ignore[arg-type]


def test_policy_bounds_and_relationship() -> None:
    assert SchedulerPolicy(1, 1).interval_seconds == 1
    assert SchedulerPolicy(86400, 60).heartbeat_seconds == 60
    with pytest.raises(DiscoveryError, match="scheduler_policy_invalid"):
        SchedulerPolicy(1, 2)


@pytest.mark.parametrize(
    "content,code",
    [
        ("scheduler: {}", None),
        ("scheduler: {interval_seconds: 4, heartbeat_seconds: 1}", None),
        ("{}", "scheduler_policy_invalid"),
        ("scheduler: []", "scheduler_policy_invalid"),
        ("scheduler: {enabled: true}", "scheduler_policy_invalid"),
        ("scheduler: {interval_seconds: 4, interval_seconds: 5}", "scheduler_policy_invalid"),
        ("scheduler: {1: 4}", "scheduler_policy_invalid"),
        ("scheduler: {interval_seconds: .nan}", "scheduler_policy_invalid"),
        ("scheduler: {interval_seconds: yes}", "scheduler_policy_invalid"),
        ("scheduler: [", "scheduler_policy_unreadable"),
    ],
)
def test_config(tmp_path: Path, content: str, code: str | None) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text(content)
    if code:
        with pytest.raises(DiscoveryError, match=code):
            load_scheduler_policy(path)
    else:
        assert isinstance(load_scheduler_policy(path), SchedulerPolicy)


def test_config_unreadable_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "missing"
    with pytest.raises(DiscoveryError, match="scheduler_policy_unreadable"):
        load_scheduler_policy(path)
    path.write_bytes(b"#" * 65537)
    with pytest.raises(DiscoveryError, match="scheduler_policy_too_large"):
        load_scheduler_policy(path)
    path.write_bytes(b"\xff")
    with pytest.raises(DiscoveryError, match="scheduler_policy_unreadable"):
        load_scheduler_policy(path)


def test_immediate_first_cycle_stopped_persisted(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite3"
    clock = Clock()
    result = execute(db, clock)
    assert clock.starts == [0]
    assert clock.waits == []
    assert result == state(db)
    assert result["status"] == "stopped"
    assert result["last_result"] == {"status": "success"}
    assert result["last_success_at"] is not None
    assert result["last_error"] is None
    assert result["next_run_at"] is None


def test_fixed_delay_after_slow_cycle_without_backlog(tmp_path: Path) -> None:
    clock = Clock(max_calls=3, duration=31)
    execute(tmp_path / "db", clock)
    assert clock.starts == [0, 41, 82]
    assert clock.waits == [2] * 10


def test_backward_wall_jump_uses_monotonic_deadline(tmp_path: Path) -> None:
    clock = Clock(max_calls=2, jumps=[-3600])
    execute(tmp_path / "db", clock)
    assert clock.starts == [0, 10]


def test_forward_wall_jump_one_cycle_then_rebase(tmp_path: Path) -> None:
    clock = Clock(max_calls=3, jumps=[86400])
    execute(tmp_path / "db", clock)
    assert clock.starts == [0, 2, 12]
    assert len(clock.waits) == 6


def test_long_suspension_does_not_replay_backlog(tmp_path: Path) -> None:
    clock = Clock(max_calls=2)
    runtime = clock.runtime()

    def suspended_wait(seconds: float) -> None:
        clock.wait(seconds + 86400)

    runtime = SchedulerRuntime(
        runtime.utc_now, runtime.monotonic, suspended_wait, runtime.cycle, runtime.should_stop
    )
    run_scheduler(tmp_path / "db", Path("s"), Path("p"), SchedulerPolicy(10, 2), runtime=runtime)
    assert clock.starts == [0, 86402]
    assert clock.calls == 2


def test_failure_busy_and_unknown_error_wait_normal_interval(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock(
        max_calls=3,
        results=[
            DiscoveryError("database_busy"),
            RuntimeError("https://user:secret@example.com"),
            {"status": "success"},
        ],
    )
    result = execute(tmp_path / "db", clock)
    assert clock.starts == [0, 10, 20]
    assert result["last_error"] is None
    assert result["consecutive_failures"] == 0
    assert "secret" not in caplog.text


def test_partial_failed_success_counter(tmp_path: Path) -> None:
    clock = Clock(max_calls=2, results=[{"status": "partial"}, {"status": "failed"}])
    result = execute(tmp_path / "db", clock)
    assert result["consecutive_failures"] == 2
    assert result["last_error"] == "collection_failed"
    assert result["last_success_at"] is None


def test_partial_has_own_error_code(tmp_path: Path) -> None:
    result = execute(tmp_path / "db", Clock(results=[{"status": "partial"}]))
    assert result["last_error"] == "collection_partial"


def test_invalid_exception_code_is_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    result = execute(tmp_path / "db", Clock(results=[DiscoveryError("unsafe token:secret")]))
    assert result["last_error"] == "scheduler_cycle_failed"
    assert "secret" not in caplog.text


def test_preserves_success_across_restart_with_failures(tmp_path: Path) -> None:
    db = tmp_path / "db"
    first = execute(db, Clock())
    later = Clock(wall=datetime(2026, 10, 19, tzinfo=UTC), results=[{"status": "failed"}])
    second = execute(db, later)
    assert second["last_success_at"] == first["last_success_at"]
    assert second["session_id"] != first["session_id"]
    assert second["started_at"] != first["started_at"]
    assert second["consecutive_failures"] == 1


def test_singleton_fails_before_cycle_and_canonicalizes_alias(tmp_path: Path) -> None:
    db = tmp_path / "db"
    link = tmp_path / "alias"
    link.symlink_to(db)
    lock = tmp_path / "db.scheduler.lock"
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        clock = Clock()
        with pytest.raises(DiscoveryError, match="scheduler_already_running"):
            execute(link, clock)
        assert clock.calls == 0
    execute(db, Clock())


def test_manual_lock_prevents_all_state_writes(tmp_path: Path) -> None:
    db = tmp_path / "db"
    first = execute(db, Clock())
    with database_lock(db):
        clock = Clock(results=[DiscoveryError("database_busy")])
        second = execute(db, clock)
        assert second["last_error"] == "database_busy"
        assert state(db) == first
    # No scheduler code reconciles or touches a manual collector's live run.
    assert state(db)["session_id"] == first["session_id"]


def test_keyboard_interrupt_during_collection_stops_once(tmp_path: Path) -> None:
    clock = Clock(max_calls=10, results=[KeyboardInterrupt()])
    result = execute(tmp_path / "db", clock)
    assert clock.calls == 1
    assert clock.waits == []
    assert result["last_error"] == "collection_interrupted"
    assert result["status"] == "stopped"
    assert state(tmp_path / "db") == result


def test_keyboard_interrupt_while_waiting(tmp_path: Path) -> None:
    db = tmp_path / "db"
    clock = Clock(max_calls=2)
    runtime = clock.runtime()

    def interrupt(seconds: float) -> None:
        assert state(db)["status"] == "waiting"
        raise KeyboardInterrupt

    runtime = SchedulerRuntime(
        runtime.utc_now, runtime.monotonic, interrupt, runtime.cycle, runtime.should_stop
    )
    result = run_scheduler(db, Path("s"), Path("p"), SchedulerPolicy(10, 2), runtime=runtime)
    assert result["status"] == "stopped"
    assert result["last_error"] is None
    assert clock.calls == 1


def test_waiting_heartbeats_are_persisted(tmp_path: Path) -> None:
    db = tmp_path / "db"
    clock = Clock(max_calls=2)
    runtime = clock.runtime()
    heartbeats = []

    def record_wait(seconds: float) -> None:
        previous = state(db)
        assert previous["status"] == "waiting"
        heartbeats.append(previous["heartbeat_at"])
        clock.wait(seconds)

    runtime = SchedulerRuntime(
        runtime.utc_now, runtime.monotonic, record_wait, runtime.cycle, runtime.should_stop
    )
    run_scheduler(db, Path("s"), Path("p"), SchedulerPolicy(10, 2), runtime=runtime)
    assert len(set(heartbeats)) == 5
