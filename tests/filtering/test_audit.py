import sqlite3
from pathlib import Path
from typing import Any

import pytest

from waagd_discovery import filtering
from waagd_discovery.filtering import FilterPolicy, assess, filter_report, select_candidates
from waagd_discovery.storage import Repository

from .helpers import REFERENCE, article, seed


def test_repeat_has_identical_decisions_and_retains_immutable_older_batch(
    repository: Repository,
) -> None:
    seed(repository, [article(), article("https://example.com/missing", title=None)])
    first = assess(repository, FilterPolicy(), REFERENCE)
    first_decisions = filter_report(repository)["decisions"]
    second = assess(repository, FilterPolicy(), REFERENCE)
    assert first["run_id"] != second["run_id"]
    assert first["policy_hash"] == second["policy_hash"]
    assert first["counts"] == second["counts"]
    assert filter_report(repository)["decisions"] == first_decisions
    assert repository.connection.execute("SELECT count(*) FROM filter_batches").fetchone()[0] == 2
    assert repository.connection.execute("SELECT count(*) FROM filter_decisions").fetchone()[0] == 4
    assert repository.stats()["articles"] == 2
    assert repository.stats()["observations"] == 2
    for table in ("filter_batches", "filter_decisions"):
        for statement in (
            f"DELETE FROM {table}",
            f"UPDATE {table} SET {'id = id' if table == 'filter_batches' else 'status = status'}",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="filter_audit_is_immutable"):
                repository.connection.execute(statement)
    repository.verify()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, sqlite3.OperationalError])
def test_interruption_rolls_back_entire_audit_batch(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException]
) -> None:
    seed(repository, [article(), article("https://example.com/second")])
    original = assess(repository, FilterPolicy(), REFERENCE)
    original_report = filter_report(repository)
    insert = filtering.insert_decision
    calls = 0

    def interrupted(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        insert(*args, **kwargs)
        if calls == 2:
            raise interruption("simulated_interruption")

    monkeypatch.setattr(filtering, "insert_decision", interrupted)
    with pytest.raises(interruption):
        assess(repository, FilterPolicy(), REFERENCE)
    assert repository.connection.in_transaction is False
    assert filter_report(repository) == original_report
    assert repository.connection.execute("SELECT count(*) FROM filter_batches").fetchone()[0] == 1
    assert repository.connection.execute("SELECT count(*) FROM filter_decisions").fetchone()[0] == 2
    selected, context = select_candidates(repository, FilterPolicy(), REFERENCE)
    assert len(selected) == 2
    assert context["assessment_run_id"] == original["run_id"]
    repository.verify()


def test_failure_before_final_summary_leaves_no_orphaned_decisions(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(repository, [article()])

    def fail(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("summary_failed")

    monkeypatch.setattr(filtering, "insert_batch", fail)
    with pytest.raises(sqlite3.OperationalError, match="summary_failed"):
        assess(repository, FilterPolicy(), REFERENCE)
    assert filter_report(repository)["assessment"] is None
    assert repository.connection.execute("SELECT count(*) FROM filter_decisions").fetchone()[0] == 0
    repository.verify()


def test_empty_batch_and_bounded_report(repository: Repository) -> None:
    empty = assess(repository, FilterPolicy(), REFERENCE)
    assert empty["articles_evaluated"] == 0
    assert filter_report(repository)["decisions"] == []
    seed(repository, [article(), article("https://example.com/second")])
    assess(repository, FilterPolicy(), REFERENCE)
    report = filter_report(repository, 1)
    assert len(report["decisions"]) == 1
    assert report["truncated"] is True
    assert filter_report(repository, 0)["decisions"] == []


def test_backup_preserves_assessments_and_selected_state(
    repository: Repository, tmp_path: Path
) -> None:
    seed(repository, [article()])
    summary = assess(repository, FilterPolicy(), REFERENCE)
    destination = tmp_path / "backup.sqlite3"
    repository.backup(destination)
    with Repository(destination) as restored:
        restored.verify()
        assert filter_report(restored)["assessment"] == summary
        assert select_candidates(restored, FilterPolicy(), REFERENCE) == select_candidates(
            repository, FilterPolicy(), REFERENCE
        )
