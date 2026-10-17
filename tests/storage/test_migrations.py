import sqlite3
from pathlib import Path

import pytest

from waagd_discovery.models import DiscoveryError
from waagd_discovery.storage import Repository
from waagd_discovery.storage.migrations import MIGRATIONS, SCHEMA_VERSION, migrate


def version_one(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("BEGIN")
    for statement in MIGRATIONS[0]:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        """INSERT INTO articles (url, title, date_status, first_seen_at, last_seen_at)
        VALUES ('https://example.com/existing', 'Existing', 'missing',
                '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')"""
    )
    connection.commit()
    connection.close()


def test_upgrade_backfills_aliases_and_retains_canonical_data(tmp_path: Path) -> None:
    path = tmp_path / "version_one.sqlite3"
    version_one(path)
    with Repository(path) as repository:
        assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert repository.candidates()[0]["title"] == "Existing"
        alias = repository.connection.execute("SELECT * FROM url_aliases").fetchone()
        assert alias["url"] == "https://example.com/existing"
        assert alias["article_id"] == repository.candidates()[0]["id"]
        repository.verify()


def test_failed_upgrade_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "version_one.sqlite3"
    version_one(path)
    monkeypatch.setattr(
        "waagd_discovery.storage.migrations.MIGRATIONS",
        (MIGRATIONS[0], (*MIGRATIONS[1], "NOT SQL")),
    )
    with pytest.raises(sqlite3.OperationalError):
        with Repository(path):
            pass
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT title FROM articles").fetchone()[0] == "Existing"
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'url_aliases'"
            ).fetchone()
            is None
        )


def test_newer_schema_fails_closed_without_journal_or_content_change(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.execute("CREATE TABLE future (value TEXT)")
        connection.execute("INSERT INTO future VALUES ('precious')")
    before = path.read_bytes()
    with pytest.raises(DiscoveryError, match="database_schema_newer"):
        with Repository(path):
            pass
    assert path.read_bytes() == before
    assert not Path(f"{path}-wal").exists()


def test_unversioned_nonempty_schema_is_never_reset(tmp_path: Path) -> None:
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE precious_data (value TEXT)")
        connection.execute("INSERT INTO precious_data VALUES ('keep')")
    with pytest.raises(DiscoveryError, match="database_schema_unversioned"):
        with Repository(path):
            pass
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM precious_data").fetchone()[0] == "keep"


def test_migrate_is_idempotent_and_usable_on_snapshot_connection() -> None:
    with Repository(":memory:") as repository:
        migrate(repository.connection)
        migrate(repository.connection)
        repository.verify()
