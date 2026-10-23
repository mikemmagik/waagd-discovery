"""Offline composition checks for the autonomous one-shot cycle."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from test_sprint8_publication_progression import _policy, _store

from waagd_discovery.autonomous_cycle import AutonomousCycle, TrustedStageCommand
from waagd_discovery.editorial.delivery import PublicationDelivery
from waagd_discovery.editorial.site_import import SiteImportOutcome
from waagd_discovery.models import DiscoveryError


class FakeSite:
    def import_intent(self, _intent_file: Path) -> SiteImportOutcome:
        return SiteImportOutcome("created")


def _stage(root: Path, result: str) -> TrustedStageCommand:
    path = root / "stage.py"
    path.write_text(
        "import json, os\n"
        "assert 'SITE_TEST_SECRET' not in os.environ\n"
        f"print(json.dumps({{'status': {result!r}}}))\n"
    )
    path.chmod(0o700)
    return TrustedStageCommand(Path(sys.executable).resolve(), (str(path),))


def _delivery(root: Path) -> PublicationDelivery:
    return PublicationDelivery(
        _store(root / "store" / "editorial.sqlite3"),
        root / "export" / "publication-intent.json",
        _policy(),
        FakeSite(),
    )


def test_cycle_runs_sanitized_upstream_then_delivers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SITE_TEST_SECRET", "never-forwarded")
    cycle = AutonomousCycle((_stage(tmp_path, "synced"),), _delivery(tmp_path))
    assert cycle.run() == {"status": "completed", "code": None, "delivered": 2, "held": 0}


def test_cycle_does_not_deliver_after_upstream_operator_hold(tmp_path: Path) -> None:
    delivery = _delivery(tmp_path)
    cycle = AutonomousCycle((_stage(tmp_path, "operator_required"),), delivery)
    assert cycle.run() == {
        "status": "operator_required",
        "code": "upstream_stage_operator_required",
    }
    assert not (tmp_path / "export" / "publication-intent.json").exists()


def test_side_effecting_stage_timeout_or_exit_requires_operator(
    tmp_path: Path, monkeypatch
) -> None:
    stage = _stage(tmp_path, "synced")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("synthetic", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert stage.run() == "operator_required"
    assert (
        TrustedStageCommand(stage.executable, stage.arguments, side_effecting=False).run()
        == "retryable"
    )


def test_stage_timeout_budget_supports_five_story_gateway_batch(tmp_path: Path) -> None:
    stage = _stage(tmp_path, "synced")
    editorial = TrustedStageCommand(stage.executable, stage.arguments, timeout_seconds=10_800)
    cycle = AutonomousCycle((editorial,), _delivery(tmp_path), timeout_seconds=14_400)
    assert cycle.timeout_seconds == 14_400


def test_explicit_cycle_budget_includes_required_overhead(tmp_path: Path) -> None:
    stage = _stage(tmp_path, "synced")
    editorial = TrustedStageCommand(stage.executable, stage.arguments, timeout_seconds=10_800)
    with pytest.raises(DiscoveryError, match="autonomous_cycle_timeout_invalid"):
        AutonomousCycle((editorial,), _delivery(tmp_path), timeout_seconds=13_800)
