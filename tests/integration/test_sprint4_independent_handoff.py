"""Independent handoff boundary checks; no feeds or operator database are opened."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waagd_discovery.handoff import integration_status, record_attempt, validate_target
from waagd_discovery.models import DiscoveryError


def test_qa_auto_export_cannot_enter_any_private_database_subdirectory(tmp_path):
    db = tmp_path / "data" / "state.sqlite3"
    output = db.parent / "nested" / "deeper" / "candidates.json"
    with pytest.raises(DiscoveryError, match="handoff_target_protected"):
        validate_target(db, output, ())
    assert not db.parent.exists()


@pytest.mark.parametrize("kind", ["symlink_ancestor", "symlink", "hardlink", "fifo"])
def test_qa_auto_export_rejects_unsafe_target_objects(tmp_path, kind):
    root = tmp_path.resolve()
    exports = root / "exports"
    exports.mkdir()
    original = root / "safe.json"
    original.write_text("retained")
    output = exports / "candidates.json"
    if kind == "symlink_ancestor":
        redirect = root / "redirect"
        redirect.symlink_to(exports, target_is_directory=True)
        output = redirect / "candidates.json"
    elif kind == "symlink":
        output.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, output)
    else:
        os.mkfifo(output)
    with pytest.raises(DiscoveryError, match="unsafe"):
        validate_target(root / "data" / "db.sqlite3", output, ())
    assert original.read_text() == "retained"


def test_qa_missing_first_export_still_reports_saved_precise_failure(tmp_path):
    db = tmp_path / "private" / "db.sqlite3"
    config = tmp_path / "handoff.yaml"
    output = tmp_path / "exports" / "candidates.json"
    config.write_text(f'handoff: {{enabled: true, output: "{output}"}}')
    failure = {"status": "error", "code": "handoff_target_protected"}
    assert record_attempt(db, failure, {"run_id": "fake-run"})
    before = Path(str(db.resolve()) + ".handoff.json").read_bytes()
    result = integration_status(db, config)
    assert not result["healthy"]
    assert result["last_attempt"]["code"] == "handoff_target_protected"
    assert result["last_attempt"]["collection_run_id"] == "fake-run"
    assert Path(str(db.resolve()) + ".handoff.json").read_bytes() == before
    assert not db.exists() and not output.parent.exists()


def test_qa_diagnostics_missing_export_create_no_state_or_database(tmp_path):
    db = tmp_path / "missing-private" / "db.sqlite3"
    config = tmp_path / "handoff.yaml"
    output = tmp_path / "missing-exports" / "candidates.json"
    config.write_text(f'handoff: {{enabled: true, output: "{output}"}}')
    before = sorted(tmp_path.rglob("*"))
    result = integration_status(db, config)
    assert not result["healthy"]
    assert before == sorted(tmp_path.rglob("*"))


def test_qa_handoff_diagnostic_is_bounded_private_plain_data(tmp_path):
    db = tmp_path / "private" / "db.sqlite3"
    result = {"status": "exported", "candidates": 2}
    assert record_attempt(db, result, {"run_id": "synthetic-only"})
    path = Path(str(db.resolve()) + ".handoff.json")
    data = json.loads(path.read_text())
    assert data["status"] == "exported" and data["candidates"] == 2
    assert datetime.fromisoformat(data["finished_at"]).tzinfo is not None
    assert data["last_success_at"] == data["finished_at"]
    assert datetime.fromisoformat(data["finished_at"]) <= datetime.now(UTC)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not db.exists()
