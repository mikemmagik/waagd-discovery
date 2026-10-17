"""Independent cross-module acceptance and crash-recovery tests for Sprint 2."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from waagd_discovery import filtering
from waagd_discovery.cli import main
from waagd_discovery.filtering import FilterPolicy, assess, filter_report, select_candidates
from waagd_discovery.models import ArticleCandidate, DiscoveryError, FetchResult, Source
from waagd_discovery.operations import restore, snapshot
from waagd_discovery.storage import Repository
from waagd_discovery.storage.migrations import MIGRATIONS, SCHEMA_VERSION

REFERENCE = datetime(2026, 10, 17, 12, tzinfo=UTC)
SOURCE = Source("qa", "QA source", "https://feeds.example.org/rss")
DESCRIPTION = "Independent deterministic discovery metadata suitable for assessment."
CHECKS = [
    "canonical_url",
    "title",
    "enabled_source",
    "description",
    "publisher",
    "publication_date",
    "future_tolerance",
    "freshness",
]


def candidate(identity=1, **changes):
    return replace(
        ArticleCandidate(
            f"https://example.org/qa/{identity}",
            title=f"QA article {identity}",
            description=DESCRIPTION,
            publisher="Independent QA",
            published_at=REFERENCE - timedelta(hours=1),
            date_status="valid",
        ),
        **changes,
    )


def ingest(repository, items, source=SOURCE):
    run = repository.start_run()
    repository.save_source(run, source, items, FetchResult(200))
    repository.finish_run(run, "success")


def seed(path, items=None):
    with Repository(path) as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, items or [candidate()])
        assess(repository, FilterPolicy(), REFERENCE)


@pytest.mark.parametrize(
    "publication,expected",
    [
        (REFERENCE - timedelta(hours=72, microseconds=1), "excluded"),
        (REFERENCE - timedelta(hours=72), "eligible"),
        (REFERENCE - timedelta(hours=72) + timedelta(microseconds=1), "eligible"),
        (REFERENCE + timedelta(minutes=15) - timedelta(microseconds=1), "eligible"),
        (REFERENCE + timedelta(minutes=15), "eligible"),
        (REFERENCE + timedelta(minutes=15, microseconds=1), "deferred"),
    ],
)
def test_exact_freshness_and_future_boundaries(publication, expected):
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [candidate(published_at=publication)])
        assessed = assess(repository, FilterPolicy(), REFERENCE)
        decision = filter_report(repository)["decisions"][0]
        assert decision["status"] == expected
        assert list(decision["checks"]) == CHECKS
        assert all(type(value) is bool for value in decision["checks"].values())
        assert assessed["counts"][expected] == 1
        selected, _ = select_candidates(repository, FilterPolicy(), REFERENCE)
        assert bool(selected) == (expected == "eligible")


def test_selected_limit_applies_after_full_filtering_and_deterministic_ranking():
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(
            repository,
            [candidate(1), candidate(2, publisher=None), candidate(3)],
        )
        ingest(repository, [candidate(i, title=None) for i in range(4, 80)])
        assess(repository, FilterPolicy(), REFERENCE)
        first, context = select_candidates(repository, FilterPolicy(), REFERENCE, 2)
        assert [row["id"] for row in first] == [1, 3]
        assert context["counts"]["eligible"] == 3
        assert context["counts"]["deferred"] == 76
        repeat = assess(repository, FilterPolicy(), REFERENCE)
        second, _ = select_candidates(repository, FilterPolicy(), REFERENCE, 2)
        assert [row["id"] for row in first] == [row["id"] for row in second]
        assert repeat["counts"]["eligible"] == 3
        assert (
            first[0]["selection"]["assessment_run_id"]
            != second[0]["selection"]["assessment_run_id"]
        )


def test_latest_batch_wins_and_deferred_items_require_reassessment():
    future = REFERENCE + timedelta(hours=1)
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [candidate(published_at=future)])
        assess(repository, FilterPolicy(), REFERENCE)
        assert select_candidates(repository, FilterPolicy(), future)[0] == []
        assess(repository, FilterPolicy(), future)
        assert len(select_candidates(repository, FilterPolicy(), future)[0]) == 1
        assess(repository, FilterPolicy(), future + timedelta(hours=100))
        assert select_candidates(repository, FilterPolicy(), future)[0] == []
        assert (
            repository.connection.execute("SELECT count(*) FROM filter_batches").fetchone()[0] == 3
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Edited but still eligible"},
        {"description": DESCRIPTION + " Updated."},
        {"publisher": "Renamed publisher"},
        {"published_at": REFERENCE},
    ],
)
def test_metadata_fingerprint_invalidates_eligible_decision_until_reassessed(changes):
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [candidate()])
        assess(repository, FilterPolicy(), REFERENCE)
        ingest(repository, [candidate(**changes)])
        selected, context = select_candidates(repository, FilterPolicy(), REFERENCE)
        assert selected == []
        assert context["counts"]["stale"] == 1
        assess(repository, FilterPolicy(), REFERENCE)
        assert len(select_candidates(repository, FilterPolicy(), REFERENCE)[0]) == 1


@pytest.mark.parametrize("change", ["disabled", "removed", "renamed", "new_url"])
def test_cli_export_rechecks_current_registry_in_snapshot_only(tmp_path, capsys, change):
    db = tmp_path / "db.sqlite3"
    seed(db)
    registry = tmp_path / "sources.yaml"
    policy = tmp_path / "filter.yaml"
    policy.write_text("filter: {}")
    if change == "removed":
        row = {"id": "other", "name": "Other", "url": "https://other.example.org/rss"}
    else:
        row = {"id": SOURCE.id, "name": SOURCE.name, "url": SOURCE.url}
        if change == "disabled":
            row["enabled"] = False
        elif change == "renamed":
            row["name"] = "Renamed source"
        else:
            row["url"] = "https://feeds.example.org/changed-rss"
    registry.write_text(json.dumps({"sources": [row]}))
    output = tmp_path / "selected.json"
    before = db.read_bytes()
    args = ["--db", str(db), "--sources", str(registry), "--policy", str(policy)]
    assert main([*args, "export", "--output", str(output), "--at", REFERENCE.isoformat()]) == 0
    payload = json.loads(output.read_text())
    assert payload["schema_version"] == 2
    assert payload["candidates"] == []
    assert payload["selection"]["counts"]["stale"] == 1
    assert db.read_bytes() == before
    with snapshot(db) as repository:
        assert repository.get_source("qa").enabled is True
        assert repository.get_source("qa").name == SOURCE.name
        assert repository.get_source("qa").url == SOURCE.url
    assert main([*args, "filter", "--at", REFERENCE.isoformat()]) == 0
    assert main([*args, "export", "--output", str(output), "--at", REFERENCE.isoformat()]) == 0
    assert len(json.loads(output.read_text())["candidates"]) == (change in {"renamed", "new_url"})
    assert "Traceback" not in capsys.readouterr().err


def test_cli_policy_change_invalidates_matching_quality_and_preserves_prior_audit(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    seed(db)
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        json.dumps({"sources": [{"id": SOURCE.id, "name": SOURCE.name, "url": SOURCE.url}]})
    )
    policy = tmp_path / "filter.yaml"
    policy.write_text("filter: {require_publisher: true}")
    output = tmp_path / "selected.json"
    args = ["--db", str(db), "--sources", str(registry), "--policy", str(policy)]
    assert main([*args, "export", "--output", str(output), "--at", REFERENCE.isoformat()]) == 0
    payload = json.loads(output.read_text())
    assert payload["candidates"] == []
    assert payload["selection"]["assessment_status"] == "policy_mismatch"
    assert main([*args, "filter", "--at", REFERENCE.isoformat()]) == 0
    assert main([*args, "export", "--output", str(output), "--at", REFERENCE.isoformat()]) == 0
    assert len(json.loads(output.read_text())["candidates"]) == 1
    with snapshot(db) as repository:
        assert (
            repository.connection.execute("SELECT count(*) FROM filter_batches").fetchone()[0] == 2
        )
    assert "Traceback" not in capsys.readouterr().err


def audit_rows(repository):
    return {
        table: [tuple(row) for row in repository.connection.execute(f"SELECT * FROM {table}")]
        for table in ("filter_batches", "filter_decisions")
    }


def test_all_failed_collect_preserves_previous_assessment(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db.sqlite3"
    seed(db)
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        json.dumps({"sources": [{"id": SOURCE.id, "name": SOURCE.name, "url": SOURCE.url}]})
    )
    policy = tmp_path / "filter.yaml"
    policy.write_text("filter: {}")
    with snapshot(db) as repository:
        before = audit_rows(repository)

    class Unavailable:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch(self, source):
            raise DiscoveryError("http_timeout")

    monkeypatch.setattr("waagd_discovery.cli.HttpClient", Unavailable)
    args = ["--db", str(db), "--sources", str(registry), "--policy", str(policy)]
    assert main([*args, "collect", "--once"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert "filter" not in result
    with snapshot(db) as repository:
        assert audit_rows(repository) == before
        assert repository.stats()["articles"] == 1
        assert repository.stats()["runs"] == 2


def test_filter_interruption_rolls_back_partial_audit_and_keeps_previous_batch(monkeypatch):
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [candidate(1), candidate(2)])
        assess(repository, FilterPolicy(), REFERENCE)
        before = audit_rows(repository)
        original = filtering.insert_decision
        inserted = []

        def interrupt_after_insert(*args, **kwargs):
            original(*args, **kwargs)
            inserted.append(True)
            if len(inserted) == 2:
                raise KeyboardInterrupt

        monkeypatch.setattr(filtering, "insert_decision", interrupt_after_insert)
        with pytest.raises(KeyboardInterrupt):
            assess(repository, FilterPolicy(), REFERENCE)
        assert audit_rows(repository) == before
        assert repository.connection.in_transaction is False
        assert len(select_candidates(repository, FilterPolicy(), REFERENCE)[0]) == 2
        repository.verify()


def test_sigkill_during_assessment_recovers_complete_previous_batch(tmp_path):
    db = tmp_path / "db.sqlite3"
    seed(db, [candidate(1), candidate(2)])
    with snapshot(db) as repository:
        before = audit_rows(repository)
        before_stats = repository.stats()
    ready = tmp_path / "ready"
    program = """
import sys
import time
from pathlib import Path
from datetime import datetime
from waagd_discovery import filtering
from waagd_discovery.storage import Repository

original = filtering.insert_decision
def pause_after_insert(*args, **kwargs):
    original(*args, **kwargs)
    Path(sys.argv[2]).write_text("uncommitted decision inserted")
    time.sleep(30)
filtering.insert_decision = pause_after_insert
with Repository(sys.argv[1]) as repository:
    filtering.assess(repository, filtering.FilterPolicy(), datetime.fromisoformat(sys.argv[3]))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(db), str(ready), REFERENCE.isoformat()],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child did not reach the uncommitted audit write"
        process.kill()
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    with Repository(db) as repository:
        repository.verify()
        assert audit_rows(repository) == before
        assert repository.stats() == before_stats
        assert len(select_candidates(repository, FilterPolicy(), REFERENCE)[0]) == 2


def make_legacy(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        for statements in MIGRATIONS[:2]:
            for statement in statements:
                connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
        connection.execute(
            "INSERT INTO sources VALUES (?, ?, ?, 'rss', 1, NULL, NULL)",
            (SOURCE.id, SOURCE.name, SOURCE.url),
        )
        item = candidate()
        connection.execute(
            """INSERT INTO articles VALUES
            (1, ?, ?, ?, ?, ?, 'valid', ?, ?)""",
            (
                item.original_url,
                item.title,
                item.description,
                item.publisher,
                item.published_at.isoformat(),
                REFERENCE.isoformat(),
                REFERENCE.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO url_aliases VALUES (?, 1, ?)",
            (item.original_url, REFERENCE.isoformat()),
        )
        connection.execute(
            "INSERT INTO article_sources VALUES (1, ?, ?, ?)",
            (SOURCE.id, REFERENCE.isoformat(), REFERENCE.isoformat()),
        )


def test_legacy_restore_and_snapshot_upgrade_only_isolated_copy(tmp_path):
    legacy = tmp_path / "legacy.sqlite3"
    make_legacy(legacy)
    original = legacy.read_bytes()
    with snapshot(legacy) as repository:
        assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert select_candidates(repository, FilterPolicy(), REFERENCE)[0] == []
        assess(repository, FilterPolicy(), REFERENCE)
        assert len(select_candidates(repository, FilterPolicy(), REFERENCE)[0]) == 1
    assert legacy.read_bytes() == original
    target = tmp_path / "restored.sqlite3"
    assert restore(target, legacy) is None
    assert legacy.read_bytes() == original
    with Repository(target) as repository:
        repository.verify()
        assert repository.candidates()[0]["title"] == candidate().title
        assert (
            repository.connection.execute("SELECT count(*) FROM filter_batches").fetchone()[0] == 0
        )
        assert (
            select_candidates(repository, FilterPolicy(), REFERENCE)[1]["assessment_status"]
            == "unassessed"
        )
