"""Scheduler regressions authored independently from the scheduler implementation."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from waagd_discovery.models import DiscoveryError
from waagd_discovery.scheduler import SchedulerPolicy, SchedulerRuntime, run_scheduler
from waagd_discovery.storage import Repository

START = datetime(2026, 10, 18, 8, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = START
        self.elapsed = 0
        self.waits = []

    def utc_now(self):
        return self.now

    def monotonic(self):
        return self.elapsed

    def wait(self, seconds):
        self.waits.append(seconds)
        self.elapsed += seconds
        self.now += timedelta(seconds=seconds)


def test_interrupted_pipeline_result_stops_without_waiting_or_starting_another_cycle(tmp_path):
    clock = Clock()
    calls = []

    def cycle(*paths):
        calls.append(paths)
        return {"status": "interrupted", "sources_checked": 1}

    # Bound this test even when a regression incorrectly resumes after interruption.
    runtime = SchedulerRuntime(
        utc_now=clock.utc_now,
        monotonic=clock.monotonic,
        wait=clock.wait,
        cycle=cycle,
        should_stop=lambda: bool(clock.waits) or len(calls) > 1,
    )
    db = tmp_path / "state.sqlite3"
    state = run_scheduler(
        db, tmp_path / "sources", tmp_path / "filter", SchedulerPolicy(), runtime=runtime
    )
    assert len(calls) == 1
    assert clock.waits == []
    assert state["status"] == "stopped"
    assert state["last_error"] == "collection_interrupted"
    assert state["last_result"]["status"] == "interrupted"
    assert state["next_run_at"] is None
    assert state["last_success_at"] is None


@pytest.mark.parametrize("exception", [KeyboardInterrupt, RuntimeError])
def test_cycle_boundary_interrupt_and_failure_release_lifetime_lock(tmp_path, exception):
    clock = Clock()
    calls = []
    db = tmp_path / "state.sqlite3"

    def fail(*paths):
        calls.append(True)
        raise exception("secret string must not become an error code")

    runtime = SchedulerRuntime(
        utc_now=clock.utc_now,
        monotonic=clock.monotonic,
        wait=clock.wait,
        cycle=fail,
        should_stop=lambda: bool(clock.waits),
    )
    state = run_scheduler(
        db, tmp_path / "sources", tmp_path / "filter", SchedulerPolicy(), runtime=runtime
    )
    assert len(calls) == 1
    assert state["status"] == "stopped"
    assert "secret" not in json.dumps(state)
    assert bool(clock.waits) is (exception is RuntimeError)
    # No stale PID/heartbeat can retain a released kernel lock.
    runtime = SchedulerRuntime(should_stop=lambda: True)
    assert (
        run_scheduler(
            db, tmp_path / "sources", tmp_path / "filter", SchedulerPolicy(), runtime=runtime
        )["status"]
        == "stopped"
    )


def test_restart_preserves_last_full_success_but_partial_does_not_replace_it(tmp_path):
    db = tmp_path / "state.sqlite3"
    clock = Clock()
    first_calls = []

    def success(*paths):
        first_calls.append(True)
        return {"status": "success", "items_added": 0}

    runtime = SchedulerRuntime(
        utc_now=clock.utc_now,
        monotonic=clock.monotonic,
        wait=clock.wait,
        cycle=success,
        should_stop=lambda: bool(first_calls),
    )
    state = run_scheduler(
        db, tmp_path / "sources", tmp_path / "filter", SchedulerPolicy(), runtime=runtime
    )
    success_at = state["last_success_at"]
    assert success_at == START.isoformat(timespec="microseconds")
    clock.now += timedelta(days=30)
    second_calls = []

    def partial(*paths):
        second_calls.append(True)
        return {"status": "partial", "sources_failed": 1}

    runtime = SchedulerRuntime(
        utc_now=clock.utc_now,
        monotonic=clock.monotonic,
        wait=clock.wait,
        cycle=partial,
        should_stop=lambda: bool(second_calls),
    )
    state = run_scheduler(
        db, tmp_path / "sources", tmp_path / "filter", SchedulerPolicy(), runtime=runtime
    )
    assert state["last_success_at"] == success_at
    assert state["consecutive_failures"] == 1
    with Repository(db) as repository:
        persisted = json.loads(
            repository.connection.execute("SELECT state_json FROM service_state").fetchone()[0]
        )
    assert persisted["last_success_at"] == success_at


def test_second_scheduler_through_symlink_cannot_enter_cycle(tmp_path):
    db = tmp_path / "real.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    calls = []
    nested_errors = []
    with Repository(db):
        pass
    alias.symlink_to(db)

    def outer_cycle(*paths):
        calls.append(True)
        with pytest.raises(DiscoveryError, match="scheduler_already_running") as error:
            run_scheduler(
                alias,
                tmp_path / "sources",
                tmp_path / "filter",
                SchedulerPolicy(),
                runtime=SchedulerRuntime(should_stop=lambda: True),
            )
        nested_errors.append(error.value.code)
        return {"status": "success"}

    runtime = SchedulerRuntime(cycle=outer_cycle, should_stop=lambda: bool(calls))
    state = run_scheduler(
        db, tmp_path / "sources", tmp_path / "filter", SchedulerPolicy(), runtime=runtime
    )
    assert state["status"] == "stopped"
    assert nested_errors == ["scheduler_already_running"]
    assert not (tmp_path / "alias.sqlite3.scheduler.lock").exists()


@pytest.mark.parametrize("jump_days,second_elapsed", [(30, 5), (-30, 20)])
def test_wall_jump_rebases_after_one_cycle_and_uses_monotonic_for_backwards_jump(
    tmp_path, jump_days, second_elapsed
):
    clock = Clock()
    starts = []

    def cycle(*paths):
        starts.append((clock.elapsed, clock.now))
        return {"status": "success", "items_added": 0}

    def jump_wait(seconds):
        first_wait = not clock.waits
        clock.wait(seconds)
        if first_wait:
            clock.now += timedelta(days=jump_days)

    runtime = SchedulerRuntime(
        utc_now=clock.utc_now,
        monotonic=clock.monotonic,
        wait=jump_wait,
        cycle=cycle,
        should_stop=lambda: len(starts) == 3,
    )
    state = run_scheduler(
        tmp_path / "state",
        tmp_path / "sources",
        tmp_path / "policy",
        SchedulerPolicy(interval_seconds=20, heartbeat_seconds=5),
        runtime=runtime,
    )
    assert [start[0] for start in starts] == [0, second_elapsed, second_elapsed + 20]
    assert starts[2][1] - starts[1][1] == timedelta(seconds=20)
    assert len(starts) == 3
    assert state["status"] == "stopped"


def test_slow_cycle_uses_fixed_delay_after_completion_and_busy_errors_never_spin(tmp_path):
    clock = Clock()
    starts = []

    def cycle(*paths):
        starts.append(clock.elapsed)
        # A slow synchronous collector has no reentrant scheduling opportunity.
        clock.elapsed += 100
        clock.now += timedelta(seconds=100)
        raise DiscoveryError("database_busy")

    runtime = SchedulerRuntime(
        utc_now=clock.utc_now,
        monotonic=clock.monotonic,
        wait=clock.wait,
        cycle=cycle,
        should_stop=lambda: len(starts) == 3,
    )
    state = run_scheduler(
        tmp_path / "state",
        tmp_path / "sources",
        tmp_path / "policy",
        SchedulerPolicy(interval_seconds=20, heartbeat_seconds=5),
        runtime=runtime,
    )
    assert starts == [0, 120, 240]
    assert state["consecutive_failures"] == 3
    assert state["last_error"] == "database_busy"
    assert sum(clock.waits) == 40
