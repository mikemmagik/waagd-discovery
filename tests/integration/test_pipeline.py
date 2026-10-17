import json
import sqlite3
from contextlib import closing

import pytest

from waagd_discovery.cli import main
from waagd_discovery.models import DiscoveryError, FetchResult, Source
from waagd_discovery.operations import snapshot
from waagd_discovery.pipeline import collect
from waagd_discovery.storage import Repository

RSS = b"""<rss version="2.0"><channel><title>Fixture</title><item>
<guid>one</guid><title>Actual discovery</title>
<link>https://example.com/story?utm_source=test</link>
<description>&lt;b&gt;Untrusted data&lt;/b&gt;</description>
<pubDate>Sat, 17 Oct 2026 10:00:00 GMT</pubDate>
</item></channel></rss>"""
SOURCES = [
    Source("one", "One", "https://example.com/feed"),
    Source("two", "Two", "https://example.org/feed"),
]


class Client:
    def __init__(self, failure=False, interrupted=False, not_modified=False):
        self.failure = failure
        self.interrupted = interrupted
        self.not_modified = not_modified

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def fetch(self, source):
        if self.interrupted and source.id == "two":
            raise KeyboardInterrupt
        if self.failure and source.id == "two":
            raise DiscoveryError("http_status_500")
        return FetchResult(304 if self.not_modified else 200, RSS, etag='"version1"')


def test_pipeline_repeat_cross_source_and_failure(tmp_path):
    path = tmp_path / "discovery.sqlite3"
    with Repository(path) as repository:
        first = collect(repository, SOURCES, Client())
        assert first["items_added"] == 1
        assert first["items_existing"] == 1
        assert len(repository.candidates()[0]["sources"]) == 2
        second = collect(repository, SOURCES, Client(failure=True))
        assert second["status"] == "partial"
        assert second["sources_failed"] == 1
        assert repository.stats()["articles"] == 1
    with Repository(path) as reopened:
        assert reopened.stats()["articles"] == 1
        assert reopened.stats()["runs"] == 2
        assert reopened.stats()["observations"] == 3


def test_interrupt_retains_completed_source(tmp_path):
    with Repository(tmp_path / "db") as repository:
        result = collect(repository, SOURCES, Client(interrupted=True))
        assert result["status"] == "interrupted"
        assert repository.stats()["articles"] == 1


def test_304_does_not_add_observations(tmp_path):
    with Repository(tmp_path / "db") as repository:
        collect(repository, SOURCES, Client())
        result = collect(repository, SOURCES, Client(not_modified=True))
        assert result["status"] == "success"
        assert result["items_received"] == 0
        assert repository.stats()["observations"] == 2


def registry(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n  - id: one\n    name: One\n    url: https://example.com/rss\n")
    return path


def test_cli_dry_run_never_creates_or_changes_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("waagd_discovery.cli.HttpClient", Client)
    db = tmp_path / "state" / "db"
    args = ["--db", str(db), "--sources", str(registry(tmp_path))]
    assert main([*args, "collect", "--once", "--dry-run"]) == 0
    assert not db.parent.exists()
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert main([*args, "collect", "--once"]) == 0
    before = db.read_bytes()
    with snapshot(db) as repository:
        stats = repository.stats()
    assert main([*args, "collect", "--once", "--dry-run"]) == 0
    assert db.read_bytes() == before
    with snapshot(db) as repository:
        assert repository.stats() == stats


def test_cli_diagnostics_and_export_backup_restore(tmp_path, capsys):
    db = tmp_path / "db"
    args = ["--db", str(db), "--sources", str(registry(tmp_path))]
    assert main([*args, "stats"]) == 1
    assert not db.exists()
    assert main([*args, "init-db"]) == 0
    assert main([*args, "verify"]) == 0
    assert main([*args, "stats"]) == 0
    output = tmp_path / "exports" / "candidates.json"
    assert main([*args, "export", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["schema_version"] == 2
    backup = tmp_path / "backup"
    assert main([*args, "backup", "--output", str(backup)]) == 0
    assert main([*args, "restore", "--input", str(backup), "--confirm"]) == 0
    assert list(tmp_path.glob("pre-restore-*.sqlite3"))
    assert main([*args, "export", "--output", str(output), "--limit", "501"]) == 1
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("command", ["stats", "verify", "export"])
def test_diagnostics_refuse_future_schema(tmp_path, command):
    db = tmp_path / "db"
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("PRAGMA user_version=999")
    args = ["--db", str(db), command]
    if command == "export":
        args += ["--output", str(tmp_path / "out.json")]
    assert main(args) == 1
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999


def test_bad_source_selection_is_not_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr("waagd_discovery.cli.HttpClient", Client)
    db = tmp_path / "db"
    args = [
        "--db",
        str(db),
        "--sources",
        str(registry(tmp_path)),
        "collect",
        "--once",
        "--source",
        "not_configured",
    ]
    assert main(args) == 2
    assert not db.exists()
