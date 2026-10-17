"""Read-only snapshots and explicit, backed-up restoration."""

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from waagd_discovery.models import DiscoveryError
from waagd_discovery.storage import Repository


def verify_connection(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise DiscoveryError("database_integrity_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DiscoveryError("database_foreign_key_failed")


@contextmanager
def snapshot(path: Path, *, immutable: bool = False) -> Iterator[Repository]:
    """Copy a consistent DB into memory; immutable is for guarded standalone backups only."""
    if not path.is_file():
        raise DiscoveryError("database_missing")
    with Repository(":memory:") as repository:
        suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
        source_path = path.absolute() if immutable else path.resolve()
        with closing(sqlite3.connect(source_path.as_uri() + suffix, uri=True)) as source:
            source.backup(repository.connection)
        # Re-run schema validation after replacing the in-memory initial schema.
        from waagd_discovery.storage.migrations import migrate

        migrate(repository.connection)
        yield repository


def restore(path: Path, backup: Path) -> Path | None:
    if not backup.is_file() or backup.resolve() == path.resolve():
        raise DiscoveryError("restore_input_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".restore-", suffix=".sqlite3", dir=path.parent)
    os.close(descriptor)
    staged = Path(name)
    previous: Path | None = None
    try:
        with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            verify_connection(source)
            if source.execute("PRAGMA user_version").fetchone()[0] < 1:
                raise DiscoveryError("restore_not_application_backup")
            with closing(sqlite3.connect(staged)) as target:
                source.backup(target)
        with Repository(staged) as restored:
            restored.verify()
            restored.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if path.exists():
            previous = path.with_name(
                "pre-restore-"
                + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + uuid4().hex[:8]
                + ".sqlite3"
            )
            with closing(sqlite3.connect(path)) as current:
                verify_connection(current)
                with closing(sqlite3.connect(previous)) as saved:
                    current.backup(saved)
                    verify_connection(saved)
                if current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise DiscoveryError("restore_database_busy")
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise DiscoveryError("restore_database_busy")
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(staged, path)
        return previous
    finally:
        staged.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(staged) + suffix).unlink(missing_ok=True)
