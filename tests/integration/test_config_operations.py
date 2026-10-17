import json
import sqlite3

import pytest
from test_pipeline import SOURCES, Client, collect

from waagd_discovery.config import load_sources
from waagd_discovery.export import export_candidates
from waagd_discovery.locking import database_lock
from waagd_discovery.models import DiscoveryError
from waagd_discovery.operations import restore, snapshot
from waagd_discovery.storage import Repository


@pytest.mark.parametrize(
    "body",
    [
        "null",
        "[]",
        "sources: []",
        "sources: nope",
        "sources: [{id: X, name: X, url: 'https://example.com'}]",
        "sources: [{id: x, name: X, url: 'file:///tmp/feed'}]",
        "sources: [{id: x, name: X, url: 'https://user:password@example.com'}]",
        "sources: [{id: x, name: X, url: 'https://:password@example.com'}]",
        "sources: [{id: x, name: X, url: 'https://example.com', enabled: 'yes'}]",
        "sources: [{id: x, name: X, url: 'https://example.com', type: html}]",
        "sources: [{id: x, name: X, url: 'https://example.com', token: secret}]",
        "sources: [{id: x, name: X, url: 'https://example.com/#fragment'}]",
        "sources: [{id: x, name: X, url: 'https://example.com:bad'}]",
        "sources: [{id: x, name: X, url: 'https://example.com'}, {id: x, name: X, url: 'https://example.com'}]",
        "!!python/object/apply:os.system ['touch /tmp/should-not-exist']",
    ],
)
def test_registry_rejects_invalid_or_unsafe_data(tmp_path, body):
    registry = tmp_path / "sources.yaml"
    registry.write_text(body)
    with pytest.raises(DiscoveryError):
        load_sources(registry)


def test_registry_missing_and_size_bound(tmp_path):
    path = tmp_path / "missing"
    with pytest.raises(DiscoveryError, match="unreadable"):
        load_sources(path)
    path.write_text("x" * 262145)
    with pytest.raises(DiscoveryError, match="too_large"):
        load_sources(path)


def test_advisory_lock_releases_even_on_error(tmp_path):
    db = tmp_path / "db"
    with database_lock(db):
        with pytest.raises(DiscoveryError, match="busy"), database_lock(db):
            pass
    with database_lock(db):
        pass


def test_restore_rejects_bad_input_without_touching_target(tmp_path):
    db = tmp_path / "db"
    with Repository(db) as repository:
        collect(repository, SOURCES, Client())
    before = db.read_bytes()
    bad = tmp_path / "invalid"
    bad.write_text("this is not a database")
    with pytest.raises(sqlite3.DatabaseError):
        restore(db, bad)
    assert db.read_bytes() == before
    with pytest.raises(DiscoveryError):
        restore(db, db)
    assert not list(tmp_path.glob(".restore-*"))


def test_restore_keeps_previous_database(tmp_path):
    db, backup = tmp_path / "db", tmp_path / "backup"
    with Repository(db) as repository:
        repository.backup(backup)
        collect(repository, SOURCES, Client())
    with database_lock(db):
        previous = restore(db, backup)
    assert previous is not None
    with snapshot(previous) as repository:
        assert repository.stats()["articles"] == 1
    with snapshot(db) as repository:
        assert repository.stats()["articles"] == 0


def test_export_bounded_schema_and_atomic_failure(tmp_path, monkeypatch):
    output = tmp_path / "candidates.json"
    with Repository(":memory:") as repository:
        collect(repository, SOURCES, Client())
        assert export_candidates(repository, output, 1) == 1
        data = json.loads(output.read_text())
        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["description"] == "Untrusted data"
        before = output.read_bytes()
        monkeypatch.setattr(
            "waagd_discovery.export.os.replace", lambda *a: (_ for _ in ()).throw(OSError())
        )
        with pytest.raises(OSError):
            export_candidates(repository, output)
        assert output.read_bytes() == before
        assert not list(tmp_path.glob(".candidates-*"))
