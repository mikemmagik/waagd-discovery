"""Offline calendar, recovery and singleton tests for daily editorial coordination."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waagd_discovery.autonomous import DailyCoordinator, DailyPolicy, subprocess_runner


def at(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def coordinator(root: Path, calls: list[tuple[str, str]]) -> DailyCoordinator:
    def runner(cycle_id: str, date: str) -> dict[str, object]:
        calls.append((cycle_id, date))
        return {"status": "completed"}

    return DailyCoordinator(root, DailyPolicy("Asia/Bangkok", "08:00"), runner)


def test_install_is_disabled_and_uninstall_retains_journal(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    instance = coordinator(tmp_path / "private", calls)
    assert instance.install(at("2026-10-23T00:00:00+00:00"))["status"] == "installed"
    assert instance.run_due(at("2026-10-23T01:00:00+00:00")) == {"status": "disabled"}
    assert instance.enable()["enabled"] is True
    assert instance.uninstall() == {
        "status": "updated",
        "enabled": False,
        "paused": True,
        "state_retained": True,
    }
    assert (tmp_path / "private" / "autonomous.sqlite3").is_file()
    assert (tmp_path / "private" / "autonomous.sqlite3").stat().st_mode & 0o777 == 0o600


def test_one_local_day_runs_once_even_with_duplicate_timer(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    instance = coordinator(tmp_path / "private", calls)
    instance.install()
    instance.enable()
    first = instance.run_due(at("2026-10-23T01:05:00+00:00"))
    assert first["status"] == "completed"
    assert first["date"] == "2026-10-23"
    assert instance.run_due(at("2026-10-23T02:05:00+00:00")) == {"status": "not_due"}
    assert len(calls) == 1


def test_five_publications_cannot_be_followed_by_another_daily_cycle(tmp_path: Path) -> None:
    """The durable local-date journal preserves the delivery cap after restart."""

    calls: list[tuple[str, str]] = []

    def five_items(cycle_id: str, date: str) -> dict[str, object]:
        calls.append((cycle_id, date))
        return {"status": "completed", "delivered": 5}

    root = tmp_path / "private"
    first = DailyCoordinator(root, DailyPolicy(), five_items)
    first.install()
    first.enable()
    now = at("2026-10-23T01:00:00+00:00")
    assert first.run_due(now)["status"] == "completed"
    # Reopening mimics launchd restart or another wake on the same Bangkok day.
    second = DailyCoordinator(root, DailyPolicy(), five_items)
    assert second.run_due(now) == {"status": "not_due"}
    assert len(calls) == 1


def test_two_simulated_daily_cycles_have_distinct_local_dates(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    instance = coordinator(tmp_path / "private", calls)
    instance.install()
    instance.enable()

    first = instance.run_due(at("2026-10-23T01:05:00+00:00"))
    second = instance.run_due(at("2026-10-24T01:05:00+00:00"))

    assert first["status"] == second["status"] == "completed"
    assert [date for _, date in calls] == ["2026-10-23", "2026-10-24"]


def test_pre_slot_wake_catches_only_yesterday_once(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    instance = coordinator(tmp_path / "private", calls)
    instance.install()
    instance.enable()
    # 06:30 Bangkok on the 24th: yesterday is the only possible catch-up cycle.
    result = instance.run_due(at("2026-10-23T23:30:00+00:00"))
    assert result["status"] == "completed"
    assert result["date"] == "2026-10-23"
    assert instance.run_due(at("2026-10-23T23:45:00+00:00")) == {"status": "not_due"}
    assert calls[0][1] == "2026-10-23"


def test_interrupted_cycle_recovers_with_new_identity_and_retry_limit(tmp_path: Path) -> None:
    attempts: list[str] = []

    def runner(cycle_id: str, _date: str) -> dict[str, object]:
        attempts.append(cycle_id)
        return {"status": "retryable", "code": "temporary_outage"}

    instance = DailyCoordinator(
        tmp_path / "private", DailyPolicy("Asia/Bangkok", "08:00", maximum_attempts=2), runner
    )
    instance.install()
    instance.enable()
    now = at("2026-10-23T01:00:00+00:00")
    assert instance.run_due(now)["status"] == "retryable"
    assert instance.run_due(now)["status"] == "retryable"
    assert instance.run_due(now) == {
        "status": "operator_required",
        "reason": "retry_limit",
        "date": "2026-10-23",
    }
    assert len(set(attempts)) == 2


def test_pause_persists_for_a_new_coordinator(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    root = tmp_path / "private"
    first = coordinator(root, calls)
    first.install()
    first.enable()
    assert first.pause()["paused"] is True
    second = coordinator(root, calls)
    assert second.run_due(at("2026-10-23T01:00:00+00:00")) == {"status": "paused"}


def test_runner_timeout_and_crash_are_operator_required_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = (str(Path("/bin/true")),)
    runner = subprocess_runner(command)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("synthetic", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert runner("cycle", "2026-10-23") == {
        "status": "operator_required",
        "code": "cycle_runner_timeout",
    }

    def crashed(_cycle_id: str, _date: str) -> dict[str, object]:
        raise RuntimeError("synthetic")

    instance = DailyCoordinator(tmp_path / "private", DailyPolicy(), crashed)
    instance.install()
    instance.enable()
    now = at("2026-10-23T01:00:00+00:00")
    assert instance.run_due(now)["status"] == "operator_required"
    assert instance.run_due(now) == {"status": "not_due"}


def test_restart_never_replays_a_cycle_interrupted_after_dispatch(tmp_path: Path) -> None:
    root = tmp_path / "private"

    def interrupted(_cycle_id: str, _date: str) -> dict[str, object]:
        raise KeyboardInterrupt

    first = DailyCoordinator(root, DailyPolicy(), interrupted)
    first.install()
    first.enable()
    now = at("2026-10-23T01:00:00+00:00")
    with pytest.raises(KeyboardInterrupt):
        first.run_due(now)

    second = coordinator(root, [])
    assert second.run_due(now) == {"status": "not_due"}
    assert second.status()["latest_cycle"]["status"] == "operator_required"
