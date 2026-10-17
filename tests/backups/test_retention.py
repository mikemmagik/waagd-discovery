import fcntl
import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from waagd_discovery import backups
from waagd_discovery.backups import (
    RetentionPolicy,
    create_managed_backup,
    load_retention_policy,
    rotate_backups,
)
from waagd_discovery.models import DiscoveryError
from waagd_discovery.storage import Repository
from waagd_discovery.storage.migrations import MIGRATIONS, SCHEMA_VERSION

REFERENCE = datetime(2026, 10, 17, 12, tzinfo=UTC)


def managed_name(when: datetime, number: int = 1) -> str:
    return f"waagd-backup-{when.strftime('%Y%m%dT%H%M%S%fZ')}-{UUID(int=number)}.sqlite3"


def fixture(directory: Path, days: int, *, number: int = 1) -> Path:
    path = directory / managed_name(REFERENCE - timedelta(days=days), number)
    with Repository(":memory:") as repository:
        repository.backup(path)
    return path


def contents(directory: Path) -> dict[str, bytes | str]:
    return {
        path.name: os.readlink(path) if path.is_symlink() else path.read_bytes()
        for path in directory.iterdir()
        if path.is_file() or path.is_symlink()
    }


def test_policy_defaults_are_frozen_and_empty_config_uses_them(tmp_path: Path) -> None:
    policy = RetentionPolicy()
    assert policy.keep_last == 7 and policy.max_age_days == 30
    with pytest.raises(FrozenInstanceError):
        policy.keep_last = 1  # type: ignore[misc]
    path = tmp_path / "policy.yaml"
    path.write_text("backup: {}")
    assert load_retention_policy(path) == policy
    path.write_text("backup: {keep_last: 2, max_age_days: 3}")
    assert load_retention_policy(path) == RetentionPolicy(2, 3)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "7", None, float("inf"), float("nan")])
@pytest.mark.parametrize("field", ["keep_last", "max_age_days"])
def test_policy_rejects_nonpositive_or_noninteger_values(value: object, field: str) -> None:
    with pytest.raises(DiscoveryError, match="backup_policy_invalid"):
        RetentionPolicy(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[]",
        "backup: null",
        "backup: []",
        "backup: {keep_last: true}",
        "backup: {max_age_days: 1.0}",
        "backup: {max_age_days: .inf}",
        "backup: {keep_last: 0}",
        "backup: {unknown: 1}",
        "backup: {}\nunknown: true",
        "backup: {keep_last: 7, keep_last: 1}",
        "backup: {}\nbackup: {keep_last: 1}",
        "backup: {7: 7}",
        "backup: &recursive {keep_last: *recursive}",
        "backup: {keep_last: &recursive [*recursive]}",
        "backup: !!python/object:subprocess.Popen {}",
        "backup: [",
        "backup: " + "[" * 1500 + "0" + "]" * 1500,
        "#" + "x" * 65_536,
    ],
)
def test_config_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(content)
    with pytest.raises(DiscoveryError, match="backup_policy_"):
        load_retention_policy(path)


def test_missing_or_nonunicode_config_has_fixed_error(tmp_path: Path) -> None:
    path = tmp_path / "private-operator-path.yaml"
    with pytest.raises(DiscoveryError, match="^backup_policy_unreadable$"):
        load_retention_policy(path)
    path.write_bytes(b"\xff")
    with pytest.raises(DiscoveryError, match="^backup_policy_unreadable$"):
        load_retention_policy(path)


@pytest.mark.parametrize(
    ("policy", "expected_days"),
    [(RetentionPolicy(2, 3), [0, 1, 3]), (RetentionPolicy(4, 1), [0, 1, 3, 5])],
)
def test_retention_keeps_union_of_newest_and_recent(
    tmp_path: Path, policy: RetentionPolicy, expected_days: list[int]
) -> None:
    paths = {days: fixture(tmp_path, days) for days in [0, 1, 3, 5, 30, 60]}
    result = rotate_backups(tmp_path, policy, as_of=REFERENCE)
    assert result["status"] == "preview"
    assert result["keep"] == [paths[days].name for days in expected_days]
    assert set(result["would_delete"]) == {
        path.name for days, path in paths.items() if days not in expected_days
    }
    assert result["deleted"] == []
    assert all(path.exists() for path in paths.values())


def test_age_boundary_is_inclusive_and_future_backup_is_retained(tmp_path: Path) -> None:
    future = fixture(tmp_path, -1)
    boundary = fixture(tmp_path, 1)
    expired = tmp_path / managed_name(REFERENCE - timedelta(days=1, microseconds=1))
    with Repository(":memory:") as repository:
        repository.backup(expired)
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), as_of=REFERENCE, apply=True)
    assert result["keep"] == [future.name, boundary.name]
    assert result["deleted"] == [expired.name]


def test_preview_preserves_all_bytes_and_directory_entries(tmp_path: Path) -> None:
    for days in [0, 20, 30]:
        fixture(tmp_path, days)
    (tmp_path / "manual.sqlite3").write_bytes(b"precious manual data")
    (tmp_path / managed_name(REFERENCE, 99)).write_bytes(b"corrupt")
    before = contents(tmp_path)
    entries = sorted(os.listdir(tmp_path))
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), as_of=REFERENCE)
    assert result["would_delete"]
    assert contents(tmp_path) == before
    assert sorted(os.listdir(tmp_path)) == entries
    assert not any(name.endswith(("-wal", "-shm")) for name in entries)


def test_invalid_schemas_and_corrupt_files_are_preserved_not_counted(tmp_path: Path) -> None:
    survivor = fixture(tmp_path, 30)
    obsolete = fixture(tmp_path, 60)
    corrupt = tmp_path / managed_name(REFERENCE, 10)
    corrupt.write_bytes(b"not sqlite")
    future = tmp_path / managed_name(REFERENCE, 11)
    empty = tmp_path / managed_name(REFERENCE, 12)
    invalid = tmp_path / managed_name(REFERENCE, 13)
    with closing(sqlite3.connect(future)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.execute("CREATE TABLE future (precious TEXT)")
    with closing(sqlite3.connect(empty)) as connection:
        connection.execute("PRAGMA user_version = 0")
    with Repository(invalid) as repository:
        repository.connection.execute("DROP TRIGGER observations_no_update")
    before = {path.name: path.read_bytes() for path in [corrupt, future, empty, invalid]}
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert result["valid"] == 2
    assert result["keep"] == [survivor.name]
    assert result["deleted"] == [obsolete.name]
    reasons = {row["name"]: row["reason"] for row in result["preserved"]}
    assert reasons[corrupt.name] == "corrupt_database"
    assert reasons[future.name] == "schema_newer"
    assert reasons[empty.name] == "schema_invalid"
    assert reasons[invalid.name] == "schema_invalid"
    assert {name: (tmp_path / name).read_bytes() for name in before} == before


def test_recoverable_legacy_schema_validates_in_memory_only(tmp_path: Path) -> None:
    path = tmp_path / managed_name(REFERENCE - timedelta(days=40))
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in MIGRATIONS[0]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
    original = path.read_bytes()
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert result["valid"] == 1 and result["keep"] == [path.name]
    assert path.read_bytes() == original
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_no_valid_survivor_blocks_all_deletion(tmp_path: Path) -> None:
    path = tmp_path / managed_name(REFERENCE - timedelta(days=50))
    path.write_bytes(b"corrupt but preserved")
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert result["status"] == "blocked" and result["reason"] == "no_valid_survivor"
    assert result["deleted"] == [] and result["would_delete"] == []
    assert path.read_bytes() == b"corrupt but preserved"


def test_only_verified_survivor_is_never_pruned(tmp_path: Path) -> None:
    survivor = fixture(tmp_path, 500)
    for number in range(2, 12):
        (tmp_path / managed_name(REFERENCE, number)).write_bytes(b"invalid")
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert result["valid"] == 1
    assert result["keep"] == [survivor.name]
    assert result["deleted"] == []
    assert survivor.exists()


def test_symlinks_hardlinks_nonregular_unknown_and_sidecars_are_preserved(tmp_path: Path) -> None:
    directory = tmp_path / "managed"
    directory.mkdir()
    outside = fixture(tmp_path, 10)
    before = outside.read_bytes()
    symbolic = directory / managed_name(REFERENCE, 1)
    symbolic.symlink_to(outside)
    hardlink = directory / managed_name(REFERENCE, 2)
    os.link(outside, hardlink)
    folder = directory / managed_name(REFERENCE, 3)
    folder.mkdir()
    fifo = directory / managed_name(REFERENCE, 4)
    os.mkfifo(fifo)
    sidecar = fixture(directory, 20, number=5)
    Path(str(sidecar) + "-wal").write_bytes(b"preserve sidecar")
    (directory / "pre-restore-manual.sqlite3").write_bytes(b"manual")
    (directory / "private\noperator-note.sqlite3").write_bytes(b"unrecognized")
    invalid_date = managed_name(REFERENCE, 7).replace("20261017", "20260230")
    (directory / invalid_date).write_bytes(b"invalid filename")
    survivor = fixture(directory, 500, number=6)
    result = rotate_backups(directory, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    reasons = {row["name"]: row["reason"] for row in result["preserved"]}
    assert reasons[symbolic.name] == "symlink"
    assert reasons[hardlink.name] == "hardlink"
    assert reasons[folder.name] == reasons[fifo.name] == "nonregular_file"
    assert reasons[sidecar.name] == "sqlite_sidecars_present"
    assert reasons[invalid_date] == "unmanaged_name"
    assert reasons["[unrecognized-name]"] == "unmanaged_name"
    assert result["keep"] == [survivor.name] and result["deleted"] == []
    assert outside.read_bytes() == before
    assert symbolic.is_symlink() and hardlink.is_file() and folder.is_dir()
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    assert str(tmp_path) not in json.dumps(result)
    assert "operator-note" not in json.dumps(result)


def test_directory_symlink_missing_directory_and_busy_directory_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(DiscoveryError, match="backup_directory_invalid"):
        rotate_backups(missing, RetentionPolicy())
    assert not missing.exists()
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DiscoveryError, match="backup_directory_invalid"):
        rotate_backups(linked, RetentionPolicy())
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DiscoveryError, match="backup_directory_busy"):
            rotate_backups(tmp_path, RetentionPolicy())
    finally:
        os.close(descriptor)


def test_reference_and_apply_require_explicit_valid_types(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="backup_reference_time_invalid"):
        rotate_backups(tmp_path, RetentionPolicy(), as_of=datetime(2026, 10, 17))
    with pytest.raises(DiscoveryError, match="backup_apply_invalid"):
        rotate_backups(tmp_path, RetentionPolicy(), apply=1)  # type: ignore[arg-type]


def test_managed_creation_uses_existing_backup_and_syncs_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = fixture(tmp_path, 200)
    events: list[str] = []
    original_sync = os.fsync
    original_unlink = os.unlink

    def sync(descriptor: int) -> None:
        events.append("sync")
        original_sync(descriptor)

    def unlink(path: str | Path, *, dir_fd: int | None = None) -> None:
        if str(path) == old.name and dir_fd is not None:
            events.append("delete")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(backups.os, "fsync", sync)
    monkeypatch.setattr(backups.os, "unlink", unlink)
    with Repository(":memory:") as repository:
        original_backup = repository.backup
        called: list[Path] = []

        def backup(path: Path) -> None:
            called.append(path)
            original_backup(path)

        monkeypatch.setattr(repository, "backup", backup)
        result = create_managed_backup(repository, tmp_path, RetentionPolicy(1, 1))
    assert result["status"] == "created"
    assert called == [tmp_path / result["created"]]
    assert (tmp_path / result["created"]).is_file()
    assert result["rotation"]["deleted"] == [old.name]
    assert events.index("delete") >= 4  # New file+directory and survivor file+directory.
    assert result["created"] in result["rotation"]["keep"]


def test_creation_sync_failure_preserves_every_old_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = fixture(tmp_path, 200)
    before = old.read_bytes()

    def fail(descriptor: int) -> None:
        raise OSError("private path must never appear in metadata")

    monkeypatch.setattr(backups.os, "fsync", fail)
    with Repository(":memory:") as repository:
        with pytest.raises(DiscoveryError, match="^backup_creation_failed$"):
            create_managed_backup(repository, tmp_path, RetentionPolicy(1, 1))
    assert old.read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 2  # Extra backup is safer than early deletion.


def test_interrupted_creation_cannot_start_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    old = fixture(tmp_path, 200)
    before = old.read_bytes()
    with Repository(":memory:") as repository:
        original = repository.backup

        def interrupt(path: Path) -> None:
            original(path)
            raise KeyboardInterrupt

        monkeypatch.setattr(repository, "backup", interrupt)
        with pytest.raises(KeyboardInterrupt):
            create_managed_backup(repository, tmp_path, RetentionPolicy(1, 1))
    assert old.read_bytes() == before


def test_failed_new_backup_verification_cannot_delete_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = fixture(tmp_path, 200)
    before = old.read_bytes()
    with Repository(":memory:") as repository:

        def corrupt(path: Path) -> None:
            path.write_bytes(b"corrupt new backup")

        monkeypatch.setattr(repository, "backup", corrupt)
        with pytest.raises(DiscoveryError, match="^backup_verification_failed$"):
            create_managed_backup(repository, tmp_path, RetentionPolicy(1, 1))
    assert old.read_bytes() == before


def test_changed_survivor_blocks_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    survivor = fixture(tmp_path, 0)
    old = fixture(tmp_path, 60)
    original = backups._verify

    def verify(descriptor: int, name: str, expected=None):
        if expected is not None and name == survivor.name:
            survivor.write_bytes(b"corrupted between planning and applying")
        return original(descriptor, name, expected)

    monkeypatch.setattr(backups, "_verify", verify)
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert result["reason"] == "survivor_verification_failed"
    assert result["deleted"] == [] and old.exists()


def test_changed_survivor_after_target_validation_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    survivor = fixture(tmp_path, 0)
    old = fixture(tmp_path, 60)
    original = backups._verify

    def verify(descriptor: int, name: str, expected=None):
        result = original(descriptor, name, expected)
        if expected is not None and name == old.name:
            survivor.write_bytes(b"corrupted immediately before unlink")
        return result

    monkeypatch.setattr(backups, "_verify", verify)
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert result["reason"] == "survivor_changed" and result["deleted"] == []
    assert old.exists()


def test_target_replaced_by_symlink_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = tmp_path / "managed"
    directory.mkdir()
    fixture(directory, 0)
    old = fixture(directory, 60)
    outside = tmp_path / "manual.sqlite3"
    outside.write_bytes(b"preserve outside")
    original = backups._verify

    def verify(descriptor: int, name: str, expected=None):
        if expected is not None and name == old.name:
            old.unlink()
            old.symlink_to(outside)
        return original(descriptor, name, expected)

    monkeypatch.setattr(backups, "_verify", verify)
    result = rotate_backups(directory, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert result["deleted"] == []
    assert {"name": old.name, "reason": "symlink"} in result["preserved"]
    assert old.is_symlink() and outside.read_bytes() == b"preserve outside"


def test_delete_failure_stops_rotation_and_keeps_valid_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    survivor = fixture(tmp_path, 0)
    old = fixture(tmp_path, 60)
    oldest = fixture(tmp_path, 90)
    original = os.unlink

    def unlink(path: str | Path, *, dir_fd: int | None = None) -> None:
        if str(path) == old.name and dir_fd is not None:
            raise PermissionError("must not expose path")
        original(path, dir_fd=dir_fd)

    monkeypatch.setattr(backups.os, "unlink", unlink)
    result = rotate_backups(tmp_path, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert result["status"] == "blocked" and result["reason"] == "rotation_io_failed"
    assert result["deleted"] == []
    assert all(path.exists() for path in [survivor, old, oldest])


def test_interrupted_rotation_leaves_verified_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    survivor = fixture(tmp_path, 0)
    old = fixture(tmp_path, 60)
    original = os.unlink

    def unlink(path: str | Path, *, dir_fd: int | None = None) -> None:
        if str(path) == old.name and dir_fd is not None:
            raise KeyboardInterrupt
        original(path, dir_fd=dir_fd)

    monkeypatch.setattr(backups.os, "unlink", unlink)
    with pytest.raises(KeyboardInterrupt):
        rotate_backups(tmp_path, RetentionPolicy(1, 1), apply=True, as_of=REFERENCE)
    assert survivor.exists() and old.exists()
    # The directory lock is released on exceptional exit.
    monkeypatch.setattr(backups.os, "unlink", original)
    assert rotate_backups(tmp_path, RetentionPolicy(1, 1), as_of=REFERENCE)["status"] == "preview"
