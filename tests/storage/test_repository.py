import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from waagd_discovery.models import ArticleCandidate, DiscoveryError, FetchResult, Source
from waagd_discovery.storage import Repository

SOURCE = Source("one", "First source", "https://feeds.example.com/one.xml")
SECOND = Source("two", "Second source", "https://feeds.example.com/two.xml")
FETCHED = FetchResult(200, etag='"representation-1"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT")


def article(url: str = "https://example.com/item", **kwargs: object) -> ArticleCandidate:
    return ArticleCandidate(original_url=url, **kwargs)  # type: ignore[arg-type]


def ingest(
    repository: Repository,
    items: list[ArticleCandidate],
    source: Source = SOURCE,
    fetched: FetchResult = FETCHED,
    skipped: int = 0,
) -> tuple[tuple[int, int, int], dict[str, object]]:
    run_id = repository.start_run()
    counts = repository.save_source(run_id, source, items, fetched, skipped)
    return counts, repository.finish_run(run_id, "success")


def test_persistence_and_connection_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    with Repository(path) as repository:
        assert repository.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert repository.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert repository.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        repository.sync_sources([SOURCE])
        ingest(repository, [article(title="Original")])
        repository.verify()
    with Repository(path) as repository:
        assert repository.stats() == {
            "articles": 1,
            "sources": 1,
            "enabled_sources": 1,
            "runs": 1,
            "observations": 1,
        }
        assert repository.get_source(SOURCE.id).etag == FETCHED.etag
        assert repository.candidates()[0]["title"] == "Original"


def test_sources_sync_disables_removed_and_preserves_history() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE, SECOND])
        ingest(repository, [article()], source=SOURCE)
        repository.sync_sources([replace(SECOND, name="Renamed")])
        assert repository.get_source(SOURCE.id).enabled is False
        assert repository.get_source(SOURCE.id).etag == FETCHED.etag
        assert repository.get_source(SECOND.id).name == "Renamed"
        assert repository.stats()["sources"] == 2
        assert repository.candidates()[0]["sources"] == [
            {"id": SOURCE.id, "name": SOURCE.name, "url": SOURCE.url}
        ]
        repository.sync_sources([SOURCE])
        assert repository.get_source(SOURCE.id).etag == FETCHED.etag
        repository.sync_sources([replace(SOURCE, url="https://feeds.example.com/new.xml")])
        assert repository.get_source(SOURCE.id).etag is None
        assert repository.get_source(SOURCE.id).last_modified is None
        observation = repository.connection.execute("SELECT * FROM observations").fetchone()
        assert observation["source_url"] == SOURCE.url


def test_registry_validation_is_atomic() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        with pytest.raises(ValueError, match="duplicate_source_id"):
            repository.sync_sources([SECOND, SECOND])
        assert repository.get_source(SOURCE.id).enabled is True
        with pytest.raises(ValueError):
            repository.sync_sources([SECOND, replace(SOURCE, id="wrong id")])
        assert repository.stats()["sources"] == 1
        with pytest.raises(DiscoveryError, match="source_not_found"):
            repository.get_source("missing")


def test_global_url_dedup_preserves_each_source_and_original_observation() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE, SECOND])
        first = article("https://EXAMPLE.com/item?utm_source=one#fragment", title="First")
        assert ingest(repository, [first])[0] == (1, 0, 0)
        second = article("https://example.com/item?fbclid=two", title="Second")
        assert ingest(repository, [second, second], source=SECOND)[0] == (0, 2, 0)
        candidate = repository.candidates()[0]
        assert candidate["title"] == "Second"
        assert candidate["url"] == "https://example.com/item"
        assert candidate["sources"] == [
            {"id": s.id, "name": s.name, "url": s.url} for s in (SOURCE, SECOND)
        ]
        observations = repository.connection.execute(
            "SELECT * FROM observations ORDER BY id"
        ).fetchall()
        assert [row["title"] for row in observations] == ["First", "Second", "Second"]
        assert observations[0]["original_url"] == first.original_url
        assert repository.stats()["articles"] == 1
        assert repository.stats()["observations"] == 3


def test_guid_changed_url_creates_alias_and_keeps_canonical_identity() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE, SECOND])
        ingest(repository, [article(external_guid="guid-1", title="First")])
        before = repository.candidates()[0]
        changed = article("https://example.com/updated", external_guid="guid-1", title="Updated")
        assert ingest(repository, [changed])[0] == (0, 1, 0)
        assert ingest(repository, [article(changed.original_url)], source=SECOND)[0] == (0, 1, 0)
        after = repository.candidates()[0]
        assert after["id"] == before["id"]
        assert after["url"] == before["url"]
        assert after["first_seen_at"] == before["first_seen_at"]
        aliases = repository.connection.execute(
            "SELECT url FROM url_aliases ORDER BY url"
        ).fetchall()
        assert [row[0] for row in aliases] == ["https://example.com/item", changed.original_url]


def test_guid_is_scoped_to_source() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE, SECOND])
        ingest(repository, [article(external_guid="shared-guid")])
        assert ingest(
            repository,
            [article("https://example.com/other", external_guid="shared-guid")],
            source=SECOND,
        )[0] == (1, 0, 0)
        assert repository.stats()["articles"] == 2


def test_url_guid_conflict_fails_closed_without_mutating_either_article() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(
            repository,
            [
                article("https://example.com/a", external_guid="a", title="A"),
                article("https://example.com/b", external_guid="b", title="B"),
            ],
        )
        before = repository.candidates()
        conflict = article("https://example.com/a", external_guid="b", title="Overwrite")
        assert ingest(repository, [conflict])[0] == (0, 0, 1)
        assert repository.candidates() == before
        assert repository.stats()["observations"] == 2


@pytest.mark.parametrize(
    "invalid",
    [
        article("ftp://example.com/a"),
        article(title="x" * 1001),
        article(description="x" * 4001),
        article(publisher="x" * 301),
        article(external_guid="x" * 4097),
        article(published_at=datetime(2025, 1, 1), date_status="valid"),
        article(date_status="valid"),
        article(published_at=datetime(2025, 1, 1, tzinfo=UTC), date_status="missing"),
        article(date_status="bad"),
    ],
)
def test_invalid_items_fail_individually_and_count_skipped(invalid: ArticleCandidate) -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        counts, summary = ingest(repository, [invalid, article()], skipped=2)
        assert counts == (1, 0, 1)
        assert summary["items_received"] == 4
        assert summary["items_added"] == 1
        assert summary["items_failed"] == 1
        assert summary["items_skipped"] == 2
        assert repository.stats()["observations"] == 1


def test_aware_publication_date_is_utc_and_missing_metadata_is_json_null() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(
            repository,
            [
                article(
                    published_at=datetime(2025, 1, 1, 7, tzinfo=timezone(timedelta(hours=7))),
                    date_status="valid",
                )
            ],
        )
        candidate = repository.candidates()[0]
        assert candidate["published_at"] == "2025-01-01T00:00:00+00:00"
        assert candidate["title"] is None
        assert candidate["description"] is None
        assert candidate["publisher"] is None
        assert json.loads(json.dumps([candidate])) == [candidate]


def test_304_preserves_article_timestamps_and_observations_and_missing_validators() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [article()])
        before = repository.candidates()
        assert ingest(repository, [article(title="ignored")], fetched=FetchResult(304))[0] == (
            0,
            0,
            0,
        )
        assert repository.candidates() == before
        assert repository.stats()["observations"] == 1
        assert repository.get_source(SOURCE.id).etag == FETCHED.etag
        ingest(repository, [], fetched=FetchResult(200))
        assert repository.get_source(SOURCE.id).etag is None


def test_unexpected_error_rolls_back_entire_source_but_preserves_prior_source() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE, SECOND])
        run_id = repository.start_run()
        repository.save_source(run_id, SOURCE, [article(title="Before")], FETCHED)
        repository.connection.execute(
            """CREATE TRIGGER reject_second BEFORE INSERT ON observations
            WHEN NEW.source_id = 'two'
            BEGIN SELECT RAISE(ABORT, 'simulated_storage_failure'); END"""
        )
        before = repository.candidates()
        with pytest.raises(sqlite3.IntegrityError, match="simulated_storage_failure"):
            repository.save_source(
                run_id,
                SECOND,
                [article(title="Should roll back"), article("https://example.com/new")],
                FetchResult(200, etag='"new"'),
            )
        assert repository.candidates() == before
        assert repository.get_source(SECOND.id).etag is None
        assert repository.stats()["observations"] == 1
        assert repository.connection.in_transaction is False
        repository.fail_source(run_id, SECOND, "database_error")
        summary = repository.finish_run(run_id, "partial")
        assert summary["items_added"] == 1
        assert summary["sources_failed"] == 1
        assert summary["sources_checked"] == 2
        repository.connection.execute("DROP TRIGGER reject_second")
        repository.verify()


def test_source_transaction_rolls_back_earlier_items_of_the_same_feed() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        repository.connection.execute(
            """CREATE TRIGGER fail_later BEFORE INSERT ON observations
            WHEN NEW.title = 'fail'
            BEGIN SELECT RAISE(ABORT, 'simulated_failure'); END"""
        )
        run_id = repository.start_run()
        with pytest.raises(sqlite3.IntegrityError):
            repository.save_source(
                run_id,
                SOURCE,
                [article(), article("https://example.com/fail", title="fail")],
                FETCHED,
            )
        assert repository.stats()["articles"] == 0
        assert repository.stats()["observations"] == 0
        assert repository.get_source(SOURCE.id).etag is None
        assert repository.connection.execute("SELECT count(*) FROM source_runs").fetchone()[0] == 0


def test_observations_and_canonical_identity_are_immutable() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [article(title="Original")])
        for query in (
            "UPDATE observations SET title = 'altered'",
            "DELETE FROM observations",
            "UPDATE articles SET url = 'https://example.com/other'",
            "UPDATE articles SET first_seen_at = 'yesterday'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                repository.connection.execute(query)
        assert repository.candidates()[0]["title"] == "Original"


def test_run_status_validation_idempotence_and_safe_errors() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        run_id = repository.start_run()
        with pytest.raises(ValueError, match="invalid_error_code"):
            repository.fail_source(run_id, SOURCE, "https://user:secret@example.com/")
        repository.fail_source(run_id, SOURCE, "network_error")
        summary = repository.finish_run(run_id, "interrupted")
        assert summary["status"] == "interrupted"
        assert summary["sources_failed"] == 1
        assert repository.finish_run(run_id, "interrupted") == summary
        with pytest.raises(ValueError, match="run_already_finished"):
            repository.finish_run(run_id, "success")
        with pytest.raises(ValueError, match="run_not_running"):
            repository.save_source(run_id, SOURCE, [article()], FETCHED)
        with pytest.raises(ValueError, match="invalid_run_status"):
            repository.finish_run(run_id, "made_up_status")


def test_repeated_source_outcome_is_rejected_without_changing_counters_or_articles() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        run_id = repository.start_run()
        repository.save_source(run_id, SOURCE, [article(title="Kept")], FETCHED)
        before = repository.candidates()
        with pytest.raises(ValueError, match="source_already_processed"):
            repository.save_source(run_id, SOURCE, [article(title="Wrong")], FETCHED)
        with pytest.raises(ValueError, match="source_already_processed"):
            repository.fail_source(run_id, SOURCE, "network_error")
        assert repository.candidates() == before
        summary = repository.finish_run(run_id, "success")
        assert summary["items_added"] == 1
        assert summary["sources_failed"] == 0


def test_candidates_order_limit_and_slash_identity() -> None:
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(
            repository, [article("https://example.com/item"), article("https://example.com/item/")]
        )
        candidates = repository.candidates()
        # Items in one ingestion share discovery time, so the integer id breaks ties.
        assert [row["url"] for row in candidates] == [
            "https://example.com/item",
            "https://example.com/item/",
        ]
        ingest(repository, [article("https://example.com/recent")])
        assert repository.candidates(1)[0]["url"] == "https://example.com/recent"
        assert repository.candidates(0) == []
        with pytest.raises(ValueError):
            repository.candidates(-1)


def test_backup_contains_wal_data_and_refuses_existing_destination(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    with Repository(path) as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [article()])
        assert Path(f"{path}-wal").stat().st_size > 0
        repository.backup(backup)
        prior = backup.read_bytes()
        with pytest.raises(FileExistsError):
            repository.backup(backup)
        assert backup.read_bytes() == prior
        ingest(repository, [article("https://example.com/new")])
    with Repository(backup) as restored:
        assert restored.stats()["articles"] == 1
        restored.verify()


def test_backup_verification_failure_cleans_up_new_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(connection: sqlite3.Connection) -> None:
        raise DiscoveryError("database_integrity_failed")

    monkeypatch.setattr("waagd_discovery.storage._verify_connection", reject)
    destination = tmp_path / "bad.sqlite3"
    with Repository(":memory:") as repository:
        with pytest.raises(DiscoveryError, match="database_integrity_failed"):
            repository.backup(destination)
    assert not destination.exists()


def test_verify_detects_foreign_key_corruption(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    with Repository(path) as repository:
        repository.sync_sources([SOURCE])
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO source_guids (source_id, guid, article_id) VALUES ('one', 'dangling', 999)"
        )
    with Repository(path) as repository:
        with pytest.raises(DiscoveryError, match="database_foreign_key_failed"):
            repository.verify()


def test_verify_and_backup_reject_changed_schema(tmp_path: Path) -> None:
    with Repository(":memory:") as repository:
        repository.connection.execute("DROP TRIGGER observations_no_update")
        with pytest.raises(DiscoveryError, match="database_schema_invalid"):
            repository.verify()
        destination = tmp_path / "invalid.sqlite3"
        with pytest.raises(DiscoveryError, match="database_schema_invalid"):
            repository.backup(destination)
        assert not destination.exists()
