import json
from dataclasses import replace
from datetime import timedelta

import pytest

from waagd_discovery import filtering
from waagd_discovery.filtering import FilterPolicy, assess, filter_report, select_candidates
from waagd_discovery.models import FetchResult
from waagd_discovery.storage import Repository

from .helpers import REFERENCE, SECOND, SOURCE, article, seed


def test_unassessed_articles_never_export(repository: Repository) -> None:
    seed(repository, [article()])
    before = repository.connection.total_changes
    candidates, context = select_candidates(repository, FilterPolicy(), REFERENCE)
    assert candidates == []
    assert context["assessment_status"] == "unassessed"
    assert context["assessment_run_id"] is None
    assert context["assessed_at"] is None
    assert context["counts"] == {
        "eligible": 0,
        "deferred": 0,
        "excluded": 0,
        "unassessed": 1,
        "stale": 0,
    }
    assert repository.connection.total_changes == before
    assert filter_report(repository)["assessment"] is None


def test_selected_shape_and_complete_context_are_json_compatible(repository: Repository) -> None:
    seed(repository, [article()])
    summary = assess(repository, FilterPolicy(), REFERENCE)
    changes = repository.connection.total_changes
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE)
    assert set(context) == {
        "policy_hash",
        "policy",
        "engine_version",
        "reference_time",
        "assessment_run_id",
        "assessed_at",
        "assessment_status",
        "counts",
    }
    assert context["assessment_run_id"] == summary["run_id"]
    assert context["policy"] == {
        "max_age_hours": 72.0,
        "future_tolerance_minutes": 15.0,
        "min_description_chars": 40,
        "require_publisher": False,
    }
    assert selected[0]["selection"] == {
        "status": "eligible",
        "assessment_run_id": summary["run_id"],
        "policy_hash": summary["policy_hash"],
        "reasons": [],
        "checks": {
            "canonical_url": True,
            "title": True,
            "enabled_source": True,
            "description": True,
            "publisher": True,
            "publication_date": True,
            "future_tolerance": True,
            "freshness": True,
        },
    }
    assert json.loads(json.dumps((selected, context))) == [selected, context]
    assert repository.connection.total_changes == changes


def test_rank_is_publication_then_completeness_then_id_and_limit_is_last(
    repository: Repository,
) -> None:
    seed(
        repository,
        [
            article(
                "https://example.com/old",
                published_at=REFERENCE - timedelta(hours=1),
                publisher="A",
            ),
            article("https://example.com/less-complete"),
            article("https://example.com/tie-first", publisher="A"),
            article("https://example.com/tie-second", publisher="B"),
            article("https://example.com/newest", published_at=REFERENCE + timedelta(minutes=1)),
        ],
    )
    assess(repository, FilterPolicy(), REFERENCE)
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE, 3)
    assert [item["url"] for item in selected] == [
        "https://example.com/newest",
        "https://example.com/tie-first",
        "https://example.com/tie-second",
    ]
    assert context["counts"]["eligible"] == 5
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE, 0)
    assert selected == []
    assert context["counts"]["eligible"] == 5


def test_policy_change_requires_reassessment_and_latest_batch_wins(repository: Repository) -> None:
    seed(repository, [article()])
    original = assess(repository, FilterPolicy(), REFERENCE)
    strict = FilterPolicy(require_publisher=True)
    selected, context = select_candidates(repository, strict, REFERENCE)
    assert selected == []
    assert context["assessment_status"] == "policy_mismatch"
    assert context["counts"]["stale"] == 1
    latest = assess(repository, strict, REFERENCE)
    assert latest["run_id"] != original["run_id"]
    assert select_candidates(repository, strict, REFERENCE)[0] == []
    assert (
        select_candidates(repository, FilterPolicy(), REFERENCE)[1]["assessment_status"]
        == "policy_mismatch"
    )
    assert filter_report(repository)["assessment"] == latest


def test_changed_engine_requires_reassessment(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(repository, [article()])
    assess(repository, FilterPolicy(), REFERENCE)
    monkeypatch.setattr(filtering, "ENGINE_VERSION", "rules-v2")
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE)
    assert selected == []
    assert context["assessment_status"] == "engine_mismatch"
    assert context["counts"]["stale"] == 1
    assess(repository, FilterPolicy(), REFERENCE)
    assert len(select_candidates(repository, FilterPolicy(), REFERENCE)[0]) == 1


@pytest.mark.parametrize("field", ["title", "description", "publisher", "published_at"])
def test_changed_quality_metadata_cannot_export_until_reassessed(
    repository: Repository, field: str
) -> None:
    seed(repository, [article()])
    assess(repository, FilterPolicy(), REFERENCE)
    changes = {field: REFERENCE - timedelta(hours=1) if field == "published_at" else "x" * 50}
    seed(repository, [article(**changes)])
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE)
    assert selected == []
    assert context["counts"]["stale"] == 1
    assess(repository, FilterPolicy(), REFERENCE)
    assert len(select_candidates(repository, FilterPolicy(), REFERENCE)[0]) == 1


@pytest.mark.parametrize("change", ["name", "url", "enabled", "new_provenance"])
def test_changed_provenance_invalidates_persisted_input(
    repository: Repository, change: str
) -> None:
    seed(repository, [article()])
    assess(repository, FilterPolicy(), REFERENCE)
    if change == "new_provenance":
        seed(repository, [article()], SECOND)
    else:
        value = {"name": "Renamed", "url": "https://feeds.example.com/new.xml", "enabled": False}[
            change
        ]
        repository.sync_sources([replace(SOURCE, **{change: value}), SECOND])
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE)
    assert selected == []
    assert context["counts"]["stale"] == 1


def test_replay_timestamp_change_does_not_stale_quality_and_new_articles_are_unassessed(
    repository: Repository,
) -> None:
    seed(repository, [article()])
    assess(repository, FilterPolicy(), REFERENCE)
    seed(repository, [article(), article("https://example.com/new")])
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE)
    assert len(selected) == 1
    assert context["counts"]["stale"] == 0
    assert context["counts"]["unassessed"] == 1


def test_304_does_not_prevent_export_time_expiration(repository: Repository) -> None:
    seed(repository, [article(published_at=REFERENCE - timedelta(hours=71))])
    assess(repository, FilterPolicy(), REFERENCE)
    before = repository.candidates()
    run_id = repository.start_run()
    repository.save_source(run_id, SOURCE, [], FetchResult(304))
    repository.finish_run(run_id, "success")
    assert repository.candidates() == before
    selected, context = select_candidates(
        repository, FilterPolicy(), REFERENCE + timedelta(hours=2)
    )
    assert selected == []
    assert context["counts"]["excluded"] == 1
    assert context["counts"]["stale"] == 0


def test_future_deferred_requires_reassessment_even_when_time_catches_up(
    repository: Repository,
) -> None:
    seed(repository, [article(published_at=REFERENCE + timedelta(hours=1))])
    assess(repository, FilterPolicy(), REFERENCE)
    later = REFERENCE + timedelta(hours=1)
    assert select_candidates(repository, FilterPolicy(), later)[0] == []
    assess(repository, FilterPolicy(), later)
    assert len(select_candidates(repository, FilterPolicy(), later)[0]) == 1


def test_source_queries_are_constant_for_assessment_and_selection(repository: Repository) -> None:
    seed(repository, [article(f"https://example.com/{i}") for i in range(200)])
    statements: list[str] = []
    repository.connection.set_trace_callback(statements.append)
    assess(repository, FilterPolicy(), REFERENCE)
    assert sum(statement.lstrip().upper().startswith("SELECT") for statement in statements) <= 3
    statements.clear()
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE, 2)
    assert len(selected) == 2
    assert context["counts"]["eligible"] == 200
    assert sum(statement.lstrip().upper().startswith("SELECT") for statement in statements) <= 3
    repository.connection.set_trace_callback(None)
