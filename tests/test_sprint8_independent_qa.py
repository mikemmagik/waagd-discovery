"""Independent adversarial coverage for Sprint 8 automation seams."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from waagd_discovery.autonomous import DailyCoordinator, DailyPolicy


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def test_operator_required_cycle_is_not_replayed_by_duplicate_timer(tmp_path: Path) -> None:
    """A durable unsafe result needs an operator, not another automatic invocation."""
    calls: list[str] = []

    def runner(cycle_id: str, _date: str) -> dict[str, object]:
        calls.append(cycle_id)
        return {"status": "operator_required", "code": "unsafe_site_outcome"}

    coordinator = DailyCoordinator(tmp_path / "private", DailyPolicy(), runner)
    now = _at("2026-10-23T01:00:00+00:00")
    coordinator.install(now)
    coordinator.enable()

    assert coordinator.run_due(now)["status"] == "operator_required"
    assert coordinator.run_due(now) == {"status": "not_due"}
    assert len(calls) == 1
