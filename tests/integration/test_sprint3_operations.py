"""Operator diagnostics preserve data and explain technical filter results."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from waagd_discovery.cli import main
from waagd_discovery.diagnostics import service_status
from waagd_discovery.filtering import FilterPolicy, assess
from waagd_discovery.models import ArticleCandidate, FetchResult, Source
from waagd_discovery.storage import Repository

AT = datetime(2026, 10, 18, tzinfo=UTC)
SOURCE = Source("fixture", "Fixture", "https://example.org/feed")


def seed(db):
    with Repository(db) as repository:
        repository.sync_sources([SOURCE])
        run = repository.start_run()
        items = [
            ArticleCandidate(
                "https://example.org/good", "Good", "x" * 40, published_at=AT, date_status="valid"
            ),
            ArticleCandidate(
                "https://example.org/old",
                "Old",
                "x" * 40,
                published_at=AT - timedelta(hours=73),
                date_status="valid",
            ),
            ArticleCandidate("https://example.org/unknown", "Unknown", "short"),
        ]
        repository.save_source(run, SOURCE, items, FetchResult(200))
        repository.finish_run(run, "success")
        assess(repository, FilterPolicy(), AT)


def test_filter_statistics_are_complete_even_without_decision_listing(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    seed(db)
    before = db.read_bytes()
    assert main(["--db", str(db), "filter-stats"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"eligible": 1, "deferred": 1, "excluded": 1}
    assert result["reasons"]["excluded"] == {"publication_too_old": 1}
    assert result["reasons"]["deferred"] == {
        "description_too_short": 1,
        "publication_date_missing": 1,
    }
    assert result["scope"] == "latest_persisted_assessment_not_current_export_eligibility"
    assert db.read_bytes() == before


def test_status_reports_live_source_totals_and_preserves_database(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    seed(db)
    before = db.read_bytes()
    assert main(["--db", str(db), "status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sources_checked"] == 1
    assert result["sources_successful"] == 1
    assert result["sources_failed"] == 0
    assert result["latest_counts"]["items_added"] == 3
    assert result["operation_state"] == "idle"
    assert result["scheduler_alive"] is False
    assert main(["--db", str(db), "health"]) == 1
    capsys.readouterr()
    assert db.read_bytes() == before


def test_persistent_feed_problems_are_separate_from_service_liveness(tmp_path):
    db = tmp_path / "db.sqlite3"
    seed(db)
    with Repository(db) as repository:
        for _ in range(3):
            run = repository.start_run()
            repository.fail_source(run, SOURCE, "http_status_429")
            repository.finish_run(run, "failed")
    status = service_status(db)
    assert status["last_successful_run"]["status"] == "success"
    assert status["source_problems"][0]["consecutive_failures"] == 3
    assert status["source_problems"][0]["persistent_problem"] is True
    assert status["source_problems"][0]["last_error"] == "http_status_429"
    assert status["filtering"]["counts"]["eligible"] == 1


@pytest.mark.parametrize("suffix", [".scheduler.lock", ".lock"])
def test_export_cannot_replace_service_locks(tmp_path, capsys, suffix):
    db = tmp_path / "db.sqlite3"
    seed(db)
    output = tmp_path / ("db.sqlite3" + suffix)
    output.write_text("preserve")
    assert main(["--db", str(db), "export", "--output", str(output)]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "export_target_protected"
    assert output.read_text() == "preserve"
