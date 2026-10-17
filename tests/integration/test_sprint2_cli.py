"""Selected export and operator flow use the existing CLI data-safety boundary."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from waagd_discovery.cli import main
from waagd_discovery.models import ArticleCandidate, FetchResult, Source
from waagd_discovery.operations import snapshot
from waagd_discovery.storage import Repository

AT = datetime(2026, 10, 17, 12, tzinfo=UTC)
SOURCE = Source("fixture", "Fixture", "https://example.org/feed")


def prepare(tmp_path):
    db = tmp_path / "db.sqlite3"
    registry = tmp_path / "sources.yaml"
    registry.write_text("sources: [{id: fixture, name: Fixture, url: 'https://example.org/feed'}]")
    policy = tmp_path / "filter.yaml"
    policy.write_text("filter: {min_description_chars: 40}")
    with Repository(db) as repository:
        repository.sync_sources([SOURCE])
        run = repository.start_run()
        items = [
            ArticleCandidate(
                "https://example.org/fresh",
                "Fresh unusual event",
                "A sufficiently detailed piece of synthetic metadata for filtering.",
                published_at=AT - timedelta(hours=1),
                date_status="valid",
            ),
            ArticleCandidate(
                "https://example.org/stale",
                "Old event",
                "Description " * 10,
                published_at=AT - timedelta(hours=80),
                date_status="valid",
            ),
            ArticleCandidate(
                "https://example.org/undated", "Unknown publication date", "Description " * 10
            ),
        ]
        repository.save_source(run, SOURCE, items, FetchResult(200))
        repository.finish_run(run, "success")
    return db, registry, policy


def arguments(db, registry, policy):
    return ["--db", str(db), "--sources", str(registry), "--policy", str(policy)]


def test_cli_persists_decisions_and_exports_explained_fresh_records(tmp_path, capsys):
    db, registry, policy = prepare(tmp_path)
    args = arguments(db, registry, policy)
    assert main([*args, "filter", "--at", AT.isoformat()]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"eligible": 1, "excluded": 1, "deferred": 1}
    assert main([*args, "filter-report", "--limit", "2"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["decisions"]) == 2 and report["truncated"]
    before = db.read_bytes()
    output = tmp_path / "selected.json"
    assert main([*args, "export", "--output", str(output), "--at", AT.isoformat()]) == 0
    data = json.loads(output.read_text())
    assert data["schema_version"] == 2
    assert [row["url"] for row in data["candidates"]] == ["https://example.org/fresh"]
    assert all(data["candidates"][0]["selection"]["checks"].values())
    assert data["selection"]["assessment_run_id"] == result["run_id"]
    assert db.read_bytes() == before
    with snapshot(db) as repository:
        assert repository.stats()["articles"] == 3
        assert repository.stats()["observations"] == 3


def test_unassessed_policy_mismatch_and_expired_decisions_do_not_leak(tmp_path):
    db, registry, policy = prepare(tmp_path)
    args = arguments(db, registry, policy)
    output = tmp_path / "selected.json"
    export = [*args, "export", "--output", str(output)]
    assert main([*export, "--at", AT.isoformat()]) == 0
    assert json.loads(output.read_text())["candidates"] == []
    assert main([*args, "filter", "--at", AT.isoformat()]) == 0
    assert main([*export, "--at", (AT + timedelta(hours=73)).isoformat()]) == 0
    assert json.loads(output.read_text())["candidates"] == []
    policy.write_text("filter: {max_age_hours: 168, min_description_chars: 40}")
    assert main([*export, "--at", AT.isoformat()]) == 0
    assert json.loads(output.read_text())["selection"]["assessment_status"] == "policy_mismatch"
    assert json.loads(output.read_text())["candidates"] == []
    assert main([*args, "filter", "--at", AT.isoformat()]) == 0
    assert main([*export, "--at", AT.isoformat()]) == 0
    assert len(json.loads(output.read_text())["candidates"]) == 2


@pytest.mark.parametrize("target", ["filter", "backup", "database_suffix"])
def test_export_protects_policy_and_sqlite_paths(tmp_path, target):
    db, registry, policy = prepare(tmp_path)
    backup_policy = tmp_path / "backup.yaml"
    backup_policy.write_text("backup: {keep_last: 2}")
    output = {
        "filter": policy,
        "backup": backup_policy,
        "database_suffix": tmp_path / "important.sqlite3",
    }[target]
    if not output.exists():
        output.write_bytes(b"preserved")
    before = output.read_bytes()
    args = [*arguments(db, registry, policy), "--backup-policy", str(backup_policy)]
    assert main([*args, "export", "--output", str(output)]) == 1
    assert output.read_bytes() == before


def test_bad_policy_fails_before_mutating_database(tmp_path, capsys):
    db, registry, policy = prepare(tmp_path)
    before = db.read_bytes()
    policy.write_text("filter: {max_age_hours: .nan}")
    assert main([*arguments(db, registry, policy), "filter"]) == 1
    assert db.read_bytes() == before
    assert "Traceback" not in capsys.readouterr().err


def test_managed_backup_rotation_preview_and_restore(tmp_path, capsys):
    db, registry, policy = prepare(tmp_path)
    args = arguments(db, registry, policy)
    directory = tmp_path / "managed"
    assert main([*args, "backup-managed", "--directory", str(directory)]) == 0
    result = json.loads(capsys.readouterr().out)
    files = list(directory.glob("waagd-backup-*.sqlite3"))
    assert len(files) == 1 and result["created"]
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    assert main([*args, "backup-rotate", "--directory", str(directory)]) == 0
    assert {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    } == before
    assert main([*args, "restore", "--input", str(files[0]), "--confirm"]) == 0
    assert main([*args, "verify"]) == 0
    assert main([*args, "backup-rotate", "--directory", str(db.parent), "--apply"]) == 1


@pytest.mark.parametrize("value", ["2026-10-17", "not-a-date", "2026-10-17T12:00:00"])
def test_reference_time_requires_explicit_timezone(value):
    with pytest.raises(SystemExit) as exc:
        main(["filter", "--at", value])
    assert exc.value.code == 2


def test_deep_registry_error_is_safe(tmp_path, capsys):
    registry = tmp_path / "sources.yaml"
    registry.write_text("sources: " + "[" * 1500 + "]" * 1500)
    assert (
        main(["--db", str(tmp_path / "db"), "--sources", str(registry), "collect", "--once"]) == 2
    )
    assert not (tmp_path / "db").exists()
    assert "Traceback" not in capsys.readouterr().err
