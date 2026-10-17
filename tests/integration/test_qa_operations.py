"""Independent operational regressions: preserve state under adversarial inputs."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waagd_discovery.cli import main
from waagd_discovery.filtering import FilterPolicy, assess
from waagd_discovery.logging import configure
from waagd_discovery.models import ArticleCandidate, DiscoveryError, FetchResult, Source
from waagd_discovery.operations import restore, snapshot
from waagd_discovery.pipeline import collect
from waagd_discovery.storage import Repository
from waagd_discovery.storage.migrations import MIGRATIONS, SCHEMA_VERSION

SOURCE = Source("qa", "QA source", "https://feeds.example.org/rss")
ARTICLE = ArticleCandidate(
    "https://example.org/retained",
    title="Retained",
    description="Sufficient metadata for a technically eligible discovery fixture.",
    published_at=datetime(2026, 10, 17, 10, tzinfo=UTC),
    date_status="valid",
)


def seed(path: Path) -> None:
    with Repository(path) as repository:
        repository.sync_sources([SOURCE])
        run_id = repository.start_run()
        repository.save_source(run_id, SOURCE, [ARTICLE], FetchResult(200, etag='"old"'))
        repository.finish_run(run_id, "success")
        assess(repository, FilterPolicy(), datetime(2026, 10, 17, 12, tzinfo=UTC))


def files_under(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


class NoNetworkClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def fetch(self, source):
        return FetchResult(304, etag='"new"')


def test_legacy_database_diagnostics_and_dry_run_do_not_migrate_disk(tmp_path, monkeypatch, capsys):
    """The snapshot may upgrade its memory copy; the original remains version 1."""
    db = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(db)) as connection, connection:
        for statement in MIGRATIONS[0]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "sources: [{id: qa, name: QA source, url: 'https://feeds.example.org/rss'}]"
    )
    monkeypatch.setattr("waagd_discovery.cli.HttpClient", NoNetworkClient)
    before = files_under(tmp_path)
    args = ["--db", str(db), "--sources", str(registry)]
    for command in (["stats"], ["verify"], ["collect", "--once", "--dry-run"]):
        assert main([*args, *command]) == 0
        assert files_under(tmp_path) == before
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["dry_run"] is True
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.parametrize("protected", ["database", "wal", "shm", "lock", "sources"])
@pytest.mark.parametrize("through_symlink", [False, True])
def test_cli_export_rejects_protected_paths_before_reading_database(
    tmp_path, protected, through_symlink, capsys
):
    db = tmp_path / "db.sqlite3"
    seed(db)
    registry = tmp_path / "sources.yaml"
    registry.write_text("sources: []")
    targets = {
        "database": db,
        "wal": Path(str(db) + "-wal"),
        "shm": Path(str(db) + "-shm"),
        "lock": Path(str(db) + ".lock"),
        "sources": registry,
    }
    target = targets[protected]
    if not target.exists():
        target.write_bytes(b"protected sentinel")
    output = target
    if through_symlink:
        output = tmp_path / "innocent-export.json"
        output.symlink_to(target)
    before = files_under(tmp_path)
    assert (
        main(["--db", str(db), "--sources", str(registry), "export", "--output", str(output)]) == 1
    )
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["code"] == (
        "export_target_protected"
    )
    assert files_under(tmp_path) == before


def test_export_replaces_hardlink_alias_without_touching_database(tmp_path, capsys):
    db = tmp_path / "db.sqlite3"
    seed(db)
    output = tmp_path / "export.json"
    output.hardlink_to(db)
    before = db.read_bytes()
    assert (
        main(["--db", str(db), "export", "--output", str(output), "--at", "2026-10-17T12:00:00Z"])
        == 0
    )
    assert json.loads(output.read_text())["candidates"][0]["title"] == "Retained"
    assert db.read_bytes() == before
    assert output.stat().st_ino != db.stat().st_ino
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["empty", "unrelated", "missing_table", "future"])
def test_restore_rejects_non_backups_and_keeps_original_database(tmp_path, kind):
    db = tmp_path / "live.sqlite3"
    seed(db)
    backup = tmp_path / "input.sqlite3"
    if kind == "missing_table":
        seed(backup)
        with closing(sqlite3.connect(backup)) as connection, connection:
            connection.execute("DROP TABLE source_runs")
    else:
        with closing(sqlite3.connect(backup)) as connection, connection:
            if kind == "empty":
                connection.execute("VACUUM")
            else:
                connection.execute("CREATE TABLE unrelated (value TEXT)")
                connection.execute("INSERT INTO unrelated VALUES ('preserve')")
                version = SCHEMA_VERSION + (kind == "future")
                connection.execute(f"PRAGMA user_version = {version}")
    before = db.read_bytes()
    original_backup = backup.read_bytes()
    with pytest.raises((DiscoveryError, sqlite3.DatabaseError)):
        restore(db, backup)
    assert db.read_bytes() == before
    assert backup.read_bytes() == original_backup
    assert not list(tmp_path.glob(".restore-*"))
    assert not list(tmp_path.glob("pre-restore-*"))
    with snapshot(db) as repository:
        assert repository.candidates()[0]["title"] == "Retained"


def test_pipeline_storage_failure_rolls_back_feed_and_continues_later_source(tmp_path, capsys):
    configure()
    sources = [
        Source(name, name, f"https://{name}.example.org/rss") for name in ("one", "two", "three")
    ]

    class Client:
        def fetch(self, source):
            items = (
                f"<item><link>https://example.org/{source.id}</link>"
                f"<title>{source.id}</title></item>"
            )
            if source.id == "two":
                items += (
                    "<item><link>https://example.org/rejected</link><title>reject</title></item>"
                )
            body = f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()
            return FetchResult(200, body, etag=f'"{source.id}"')

    with Repository(tmp_path / "db.sqlite3") as repository:
        repository.connection.execute(
            """CREATE TRIGGER qa_reject BEFORE INSERT ON observations
            WHEN NEW.title = 'reject'
            BEGIN SELECT RAISE(ABORT, 'injected_database_failure'); END"""
        )
        result = collect(repository, sources, Client())
        assert result["status"] == "partial"
        assert result["sources_checked"] == 3
        assert result["sources_failed"] == 1
        assert result["items_added"] == 2
        assert {item["title"] for item in repository.candidates()} == {"one", "three"}
        assert repository.get_source("two").etag is None
        assert repository.stats()["observations"] == 2
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM url_aliases WHERE url LIKE '%/two'"
            ).fetchone()[0]
            == 0
        )
        repository.connection.execute("DROP TRIGGER qa_reject")
        repository.verify()


def test_sigterm_finishes_interrupted_run_and_preserves_committed_source(tmp_path):
    """Deliver a real OS signal while the second configured source is blocked."""
    db = tmp_path / "db.sqlite3"
    registry = tmp_path / "sources.yaml"
    ready = tmp_path / "ready"
    registry.write_text(
        "sources:\n"
        "- {id: one, name: One, url: 'https://one.example.org/rss'}\n"
        "- {id: two, name: Two, url: 'https://two.example.org/rss'}\n"
    )
    program = """
import sys
import time
from pathlib import Path
from waagd_discovery import cli
from waagd_discovery.models import FetchResult

class Client:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def fetch(self, source):
        if source.id == "two":
            Path(sys.argv[3]).write_text("ready")
            time.sleep(30)
        return FetchResult(
            200,
            b'<rss version="2.0"><channel><item>'
            b'<link>https://example.org/retained</link>'
            b'<title>Retained</title></item></channel></rss>',
            etag='"saved"',
        )

cli.HttpClient = Client
sys.exit(cli.main([
    "--db", sys.argv[1], "--sources", sys.argv[2], "collect", "--once"
]))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(db), str(registry), str(ready)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child did not reach second source"
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=10)
        assert process.returncode == 130
        assert json.loads(output)["status"] == "interrupted"
        assert "Traceback" not in errors
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    with snapshot(db) as repository:
        assert repository.stats()["articles"] == 1
        assert repository.stats()["observations"] == 1
        assert repository.get_source("one").etag == '"saved"'
        assert repository.get_source("two").etag is None
        result = repository.connection.execute("SELECT status FROM runs").fetchone()
        assert result[0] == "interrupted"
