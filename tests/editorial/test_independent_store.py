"""Private-store boundary probes, using disposable SQLite files only."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from test_independent_contracts import NOW, inbox

from waagd_discovery.bridge.io import BridgeError
from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.store import EditorialStore


def store_path(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "editorial-private"
    root.mkdir(mode=0o700)
    return root / "editorial.sqlite3"


def test_completed_initialized_store_cannot_reset_after_database_loss(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with EditorialStore(path, simulation=True) as store:
        store.register(inbox(), NOW.isoformat())
    path.unlink()
    with pytest.raises(EditorialError, match="editorial_store_database_missing"):
        with EditorialStore(path, simulation=True):
            pytest.fail("silently reset missing database")
    assert not path.exists()


def test_existing_lock_prevents_history_reset_if_marker_and_database_lost(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with EditorialStore(path, simulation=True) as store:
        store.register(inbox(), NOW.isoformat())
    path.unlink()
    path.with_name(path.name + ".initialized.json").unlink()
    with pytest.raises(EditorialError):
        with EditorialStore(path, simulation=True):
            pytest.fail("silently reset detectable missing initialized state")
    assert not path.exists()


def test_lost_marker_preserves_existing_database_history(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with EditorialStore(path, simulation=True) as store:
        store.register(inbox(), NOW.isoformat())
    path.with_name(path.name + ".initialized.json").unlink()
    with EditorialStore(path, simulation=True) as store:
        assert store.status()["deliveries"] == 1
        assert store.status()["candidates"] == 1


@pytest.mark.parametrize("version", [4, 999])
def test_newer_or_discovery_schema_is_never_reset(tmp_path: Path, version: int) -> None:
    path = store_path(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE preserved(value TEXT)")
        connection.execute("INSERT INTO preserved VALUES ('canary')")
        connection.execute(f"PRAGMA user_version={version}")
    path.chmod(0o600)
    with pytest.raises(EditorialError, match="editorial_schema_newer"):
        with EditorialStore(path, simulation=True):
            pytest.fail("accepted future schema")
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute("SELECT * FROM preserved").fetchall() == [("canary",)]


def test_simulation_and_manual_results_never_share_store(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with EditorialStore(path, simulation=True) as store:
        store.register(inbox(), NOW.isoformat())
    with pytest.raises(EditorialError, match="editorial_simulation_mismatch"):
        with EditorialStore(path, simulation=False):
            pytest.fail("mixed simulation and real results")
    with EditorialStore(path, simulation=True) as store:
        assert store.status()["deliveries"] == 1


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_database_objects_refused_without_following(tmp_path: Path, kind: str) -> None:
    path = store_path(tmp_path)
    target = tmp_path.resolve() / "canary"
    target.write_bytes(b"untouched")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises((EditorialError, BridgeError, OSError)):
        with EditorialStore(path, simulation=True):
            pytest.fail("unsafe database opened")
    assert target.read_bytes() == b"untouched"


@pytest.mark.parametrize("root_name", ["fake-discovery", "fake-bridge", "fake-workspace"])
def test_protected_roots_cannot_contain_editorial_database(tmp_path: Path, root_name: str) -> None:
    root = tmp_path.resolve() / root_name
    root.mkdir(mode=0o700)
    with pytest.raises(EditorialError, match="editorial_store_root_protected"):
        EditorialStore(root / "forbidden.sqlite3", simulation=True, protected_roots=(root,))
    assert list(root.iterdir()) == []


def test_status_readonly_does_not_change_database_or_create_state(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with EditorialStore(path, simulation=True) as store:
        store.register(inbox(), NOW.isoformat())
    snapshots: dict[str, tuple[bytes, int]] = {
        item.name: (item.read_bytes(), item.stat().st_mtime_ns) for item in path.parent.iterdir()
    }
    with EditorialStore(path, simulation=True, readonly=True) as store:
        assert store.status()["deliveries"] == 1
    assert {item.name for item in path.parent.iterdir()} == set(snapshots)
    for item in path.parent.iterdir():
        assert (item.read_bytes(), item.stat().st_mtime_ns) == snapshots[item.name]


def test_store_transaction_rolls_back_partial_delivery_write(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with EditorialStore(path, simulation=True) as store:
        malformed: dict[str, Any] = inbox()
        malformed["candidates"][0]["url"] = "https://changed.example.test/identity-collision"
        store.register(inbox(), NOW.isoformat())
        malformed["delivery_id"] = "478d1f48-4aab-46db-bd3e-dd49be296e47"
        with pytest.raises(EditorialError, match="candidate_identity_collision"):
            store.register(malformed, NOW.isoformat())
        assert store.status()["deliveries"] == 1
        assert store.status()["candidates"] == 1
