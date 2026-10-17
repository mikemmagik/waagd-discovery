"""Independent retention safety and immutable-audit probes; all inputs local."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from waagd_discovery import backups
from waagd_discovery.backups import (
    RetentionPolicy,
    create_managed_backup,
    load_retention_policy,
    rotate_backups,
)
from waagd_discovery.filtering import FilterPolicy, assess
from waagd_discovery.models import ArticleCandidate, DiscoveryError, FetchResult, Source
from waagd_discovery.storage import Repository
from waagd_discovery.storage.migrations import MIGRATIONS, SCHEMA_VERSION

REFERENCE = datetime(2026, 10, 17, 12, tzinfo=UTC)
SOURCE = Source("qa", "QA source", "https://feeds.example.org/rss")


def candidate():
    return ArticleCandidate("https://example.org/qa", title="Independent QA")


def ingest(repository, items):
    run = repository.start_run()
    repository.save_source(run, SOURCE, items, FetchResult(200))
    repository.finish_run(run, "success")


def managed(directory, age_days):
    stamp = REFERENCE - timedelta(days=age_days)
    return directory / f"waagd-backup-{stamp.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4()}.sqlite3"


def create_backup(path):
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [candidate()])
        repository.backup(path)


def regular_bytes(directory):
    return {
        p.name: p.read_bytes() for p in directory.iterdir() if p.is_file() and not p.is_symlink()
    }


def test_rotation_preserves_manual_corrupt_future_symlink_nonregular_and_hardlink(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    oldest, obsolete, survivor = [managed(directory, age) for age in (90, 70, 0)]
    for path in (oldest, obsolete, survivor):
        create_backup(path)
    manual = directory / "manual.sqlite3"
    create_backup(manual)
    pre_restore = directory / "pre-restore-precious.sqlite3"
    pre_restore.write_bytes(manual.read_bytes())
    corrupt = managed(directory, -1)
    corrupt.write_bytes(b"invalid sqlite but preserve these bytes")
    future = managed(directory, -2)
    with closing(sqlite3.connect(future)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    external = tmp_path / "external-precious.sqlite3"
    create_backup(external)
    link = managed(directory, 80)
    link.symlink_to(external)
    fifo = managed(directory, 81)
    os.mkfifo(fifo)
    nested = managed(directory, 82)
    nested.mkdir()
    hardlink = managed(directory, 83)
    hardlink.hardlink_to(manual)
    unsafe_name = directory / "private operator note\npassword=not-a-real-secret"
    unsafe_name.write_text("preserve")
    before = regular_bytes(directory)
    external_before = external.read_bytes()
    policy = RetentionPolicy(keep_last=1, max_age_days=1)
    preview = rotate_backups(directory, policy, as_of=REFERENCE)
    assert set(preview["would_delete"]) == {oldest.name, obsolete.name}
    assert preview["deleted"] == []
    assert regular_bytes(directory) == before
    report = rotate_backups(directory, policy, apply=True, as_of=REFERENCE)
    assert set(report["deleted"]) == {oldest.name, obsolete.name}
    assert report["valid"] == 3
    assert report["keep"] == [survivor.name]
    preserved = {entry["name"]: entry["reason"] for entry in report["preserved"]}
    assert preserved[corrupt.name] == "corrupt_database"
    assert preserved[future.name] == "schema_newer"
    assert preserved[link.name] == "symlink"
    assert preserved[fifo.name] == "nonregular_file"
    assert preserved[nested.name] == "nonregular_file"
    assert preserved[hardlink.name] == "hardlink"
    assert "[unrecognized-name]" in preserved
    assert str(tmp_path) not in json.dumps(report)
    assert "password=" not in json.dumps(report)
    assert external.read_bytes() == external_before
    assert link.is_symlink()
    assert fifo.exists()
    assert nested.is_dir()
    for name, body in before.items():
        if name not in {oldest.name, obsolete.name}:
            assert (directory / name).read_bytes() == body


@pytest.mark.parametrize("with_valid", [False, True])
def test_no_valid_or_sole_valid_survivor_never_deleted(tmp_path, with_valid):
    corrupt = managed(tmp_path, 1)
    corrupt.write_bytes(b"corrupt")
    manual = tmp_path / "manual.sqlite3"
    create_backup(manual)
    valid = managed(tmp_path, 200)
    if with_valid:
        create_backup(valid)
    before = regular_bytes(tmp_path)
    report = rotate_backups(
        tmp_path, RetentionPolicy(keep_last=1, max_age_days=1), apply=True, as_of=REFERENCE
    )
    assert report["deleted"] == []
    assert regular_bytes(tmp_path) == before
    if with_valid:
        assert report["keep"] == [valid.name]
    else:
        assert report["status"] == "blocked"
        assert report["reason"] == "no_valid_survivor"


def test_legacy_backup_is_verified_survivor_without_on_disk_migration(tmp_path):
    legacy = managed(tmp_path, 10)
    with closing(sqlite3.connect(legacy)) as connection, connection:
        for statements in MIGRATIONS[:2]:
            for statement in statements:
                connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
    before = regular_bytes(tmp_path)
    report = rotate_backups(
        tmp_path, RetentionPolicy(keep_last=1, max_age_days=1), apply=True, as_of=REFERENCE
    )
    assert report["valid"] == 1
    assert report["keep"] == [legacy.name]
    assert report["deleted"] == []
    assert regular_bytes(tmp_path) == before
    with closing(sqlite3.connect(legacy)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_survivor_corruption_during_rotation_preserves_old_verified_backup(tmp_path, monkeypatch):
    old, survivor = [managed(tmp_path, age) for age in (90, 0)]
    create_backup(old)
    create_backup(survivor)
    old_before = old.read_bytes()
    original = backups._verify

    def corrupt_before_recheck(descriptor, name, expected=None):
        if name == survivor.name and expected is not None:
            survivor.write_bytes(b"changed after initial verification")
        return original(descriptor, name, expected)

    monkeypatch.setattr(backups, "_verify", corrupt_before_recheck)
    report = rotate_backups(
        tmp_path, RetentionPolicy(keep_last=1, max_age_days=1), apply=True, as_of=REFERENCE
    )
    assert report["status"] == "blocked"
    assert report["deleted"] == []
    assert old.read_bytes() == old_before


def test_obsolete_file_replaced_by_symlink_is_preserved_without_following(tmp_path, monkeypatch):
    old, survivor = [managed(tmp_path, age) for age in (90, 0)]
    create_backup(old)
    create_backup(survivor)
    external = tmp_path / "manual.sqlite3"
    create_backup(external)
    before = external.read_bytes()
    original = backups._verify

    def substitute_before_recheck(descriptor, name, expected=None):
        if name == old.name and expected is not None:
            old.unlink()
            old.symlink_to(external)
        return original(descriptor, name, expected)

    monkeypatch.setattr(backups, "_verify", substitute_before_recheck)
    report = rotate_backups(
        tmp_path, RetentionPolicy(keep_last=1, max_age_days=1), apply=True, as_of=REFERENCE
    )
    assert report["deleted"] == []
    assert old.is_symlink()
    assert external.read_bytes() == before


def test_failed_new_backup_fsync_cannot_trigger_old_backup_deletion(tmp_path, monkeypatch):
    old = managed(tmp_path, 90)
    create_backup(old)
    before = old.read_bytes()

    def fail(*args):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(backups.os, "fsync", fail)
    with Repository(":memory:") as repository:
        with pytest.raises(DiscoveryError, match="^backup_creation_failed$"):
            create_managed_backup(repository, tmp_path, RetentionPolicy(1, 1))
    assert old.read_bytes() == before


def test_policy_recursion_is_safe_configuration_error(tmp_path):
    policy = tmp_path / "backup.yaml"
    policy.write_text("backup: " + "[" * 1500 + "0" + "]" * 1500)
    with pytest.raises(DiscoveryError):
        load_retention_policy(policy)


def test_filter_audit_cannot_be_rewritten_or_deleted():
    with Repository(":memory:") as repository:
        repository.sync_sources([SOURCE])
        ingest(repository, [candidate()])
        assess(repository, FilterPolicy(), REFERENCE)
        for statement in (
            "UPDATE filter_batches SET engine_version='tampered'",
            "DELETE FROM filter_batches",
            "UPDATE filter_decisions SET status='eligible'",
            "DELETE FROM filter_decisions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="filter_audit_is_immutable"):
                repository.connection.execute(statement)
        repository.verify()
