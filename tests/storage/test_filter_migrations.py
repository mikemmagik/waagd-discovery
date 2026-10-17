import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from waagd_discovery.filtering import FilterPolicy, assess, select_candidates
from waagd_discovery.models import DiscoveryError
from waagd_discovery.storage import Repository
from waagd_discovery.storage.migrations import MIGRATIONS, SCHEMA_VERSION

LEGACY_TABLES = (
    "sources",
    "articles",
    "runs",
    "url_aliases",
    "source_guids",
    "article_sources",
    "source_runs",
    "observations",
)
STAMP = "2026-10-17T12:00:00+00:00"


def legacy_v2(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as connection, connection:
        for migration in MIGRATIONS[:2]:
            for statement in migration:
                connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
        connection.execute(
            """INSERT INTO sources VALUES
            ('legacy', 'Legacy feed', 'https://example.com/feed', 'rss', 1, 'validator', NULL)"""
        )
        connection.execute(
            """INSERT INTO articles VALUES
            (42, 'https://example.com/old', 'A legacy article', ?, 'Legacy publisher',
             ?, 'valid', ?, ?)""",
            ("A" * 40, STAMP, STAMP, STAMP),
        )
        connection.execute("INSERT INTO runs VALUES ('old-run', ?, ?, 'success')", (STAMP, STAMP))
        connection.executemany(
            "INSERT INTO url_aliases VALUES (?, 42, ?)",
            [("https://example.com/old", STAMP), ("https://example.com/older", STAMP)],
        )
        connection.execute("INSERT INTO source_guids VALUES ('legacy', 'old-guid', 42)")
        connection.execute(
            "INSERT INTO article_sources VALUES (42, 'legacy', ?, ?)", (STAMP, STAMP)
        )
        connection.execute(
            """INSERT INTO source_runs
            VALUES ('old-run', 'legacy', 'success', 1, 1, 0, 0, 0, NULL, ?)""",
            (STAMP,),
        )
        connection.execute(
            """INSERT INTO observations VALUES
            (7, 42, 'legacy', 'old-run', ?, 'Legacy feed', 'https://example.com/feed',
             'https://example.com/old?utm_source=legacy', 'https://example.com/old', 'old-guid',
             'A legacy article', ?, 'Legacy publisher', ?, 'valid')""",
            (STAMP, "A" * 40, STAMP),
        )
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in LEGACY_TABLES
        }


def test_v01_upgrade_preserves_every_row_and_adds_unassessed_state(tmp_path: Path) -> None:
    from datetime import datetime

    path = tmp_path / "legacy.sqlite3"
    original = legacy_v2(path)
    with Repository(path) as repository:
        assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        for table in LEGACY_TABLES:
            rows = repository.connection.execute(f"SELECT * FROM {table}").fetchall()
            assert [tuple(row) for row in rows] == original[table]
        reference = datetime.fromisoformat(STAMP)
        selected, context = select_candidates(repository, FilterPolicy(), reference)
        assert selected == []
        assert context["counts"]["unassessed"] == 1
        summary = assess(repository, FilterPolicy(), reference)
        assert summary["counts"]["eligible"] == 1
        selected, _ = select_candidates(repository, FilterPolicy(), reference)
        assert selected[0]["id"] == 42
        assert selected[0]["url"] == "https://example.com/old"
        assert repository.stats()["observations"] == 1
        repository.verify()


def test_filter_migration_failure_is_atomic_and_leaves_v01_database_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.sqlite3"
    original = legacy_v2(path)
    monkeypatch.setattr(
        "waagd_discovery.storage.migrations.MIGRATIONS",
        (*MIGRATIONS[:2], (*MIGRATIONS[2], "NOT SQL")),
    )
    with pytest.raises(sqlite3.OperationalError):
        with Repository(path):
            pass
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        for table in LEGACY_TABLES:
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == original[table]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'filter_%'"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize(
    "tamper", ["DROP TRIGGER filter_batches_no_update", "DROP INDEX filter_decisions_status"]
)
def test_filter_schema_objects_are_checked_exactly(tmp_path: Path, tamper: str) -> None:
    path = tmp_path / "tampered.sqlite3"
    with Repository(path) as repository:
        repository.connection.execute(tamper)
    with pytest.raises(DiscoveryError, match="database_schema_invalid"):
        with Repository(path):
            pass
