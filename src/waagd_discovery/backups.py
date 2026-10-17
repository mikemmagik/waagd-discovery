"""Verified managed backups and conservative, opt-in local-directory retention."""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from waagd_discovery.models import DiscoveryError
from waagd_discovery.operations import snapshot
from waagd_discovery.storage import Repository
from waagd_discovery.storage.migrations import validate_schema

_MANAGED_NAME = re.compile(
    r"waagd-backup-([0-9]{8}T[0-9]{12}Z)-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.sqlite3"
)
_SIDECARS = ("-wal", "-shm", "-journal")
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
_Identity = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class RetentionPolicy:
    keep_last: int = 7
    max_age_days: int = 30

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1 for value in (self.keep_last, self.max_age_days)
        ):
            raise DiscoveryError("backup_policy_invalid")


class _PolicyLoader(yaml.SafeLoader):
    """Duplicate settings must not silently weaken retention."""


def _unique_mapping(loader: _PolicyLoader, node: yaml.MappingNode) -> dict[object, object]:
    keys: set[str] = set()
    for key, _ in node.value:
        if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
            raise DiscoveryError("backup_policy_invalid")
        if key.value in keys:
            raise DiscoveryError("backup_policy_invalid")
        keys.add(key.value)
    return dict(loader.construct_mapping(node))


_PolicyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_retention_policy(path: Path) -> RetentionPolicy:
    try:
        with path.open("rb") as source:
            content = source.read(65_537)
        if len(content) > 65_536:
            raise DiscoveryError("backup_policy_invalid")
        data = yaml.load(content.decode("utf-8"), Loader=_PolicyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, RecursionError, ValueError) as exc:
        raise DiscoveryError("backup_policy_unreadable") from exc
    if not isinstance(data, dict) or set(data) != {"backup"}:
        raise DiscoveryError("backup_policy_invalid")
    fields = data["backup"]
    if not isinstance(fields, dict) or set(fields) - {"keep_last", "max_age_days"}:
        raise DiscoveryError("backup_policy_invalid")
    return RetentionPolicy(**fields)


def _reference(as_of: datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(UTC)
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise DiscoveryError("backup_reference_time_invalid")
    return as_of.astimezone(UTC)


def _identity(info: os.stat_result) -> _Identity:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _display(name: str) -> str:
    # Unexpected filenames can contain controls or sensitive operator notes.
    return name if re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", name) else "[unrecognized-name]"


def _timestamp(name: str) -> datetime | None:
    if match := _MANAGED_NAME.fullmatch(name):
        try:
            return datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


@contextmanager
def _directory_lock(directory: Path, *, create: bool = False) -> Iterator[int]:
    descriptor: int | None = None
    try:
        if create:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DiscoveryError("backup_directory_busy") from exc
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise DiscoveryError("backup_directory_invalid") from exc
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    try:
        # Lock the directory inode rather than create a persistent lock file.
        # Preview therefore does not add even a lock file to the backup folder.
        yield descriptor
    finally:
        os.close(descriptor)


def _directory_unchanged(directory: Path, descriptor: int) -> bool:
    try:
        current = directory.stat(follow_symlinks=False)
        opened = os.fstat(descriptor)
        return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (
            opened.st_dev,
            opened.st_ino,
        )
    except OSError:
        return False


def _sidecars(descriptor: int, name: str) -> bool:
    for suffix in _SIDECARS:
        try:
            os.stat(name + suffix, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        return True
    return False


def _unchanged(descriptor: int, name: str, expected: _Identity) -> bool:
    try:
        return _identity(
            os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        ) == expected and not _sidecars(descriptor, name)
    except OSError:
        return False


@dataclass(frozen=True)
class _Backup:
    name: str
    timestamp: datetime
    identity: _Identity


def _verify(
    descriptor: int, name: str, expected: _Identity | None = None
) -> tuple[_Backup | None, str | None]:
    """Validate a pinned standalone backup without making a temporary disk copy.

    Immutable read-only SQLite access prevents WAL/SHM writes. Only managed files
    without sidecars qualify; the existing snapshot engine validates/migrates
    its in-memory copy. Neither the backup nor the container's small /tmp tmpfs
    is used as a writable database during verification.
    """
    timestamp = _timestamp(name)
    if timestamp is None:
        return None, "unmanaged_name"
    try:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return None, "symlink"
        if not stat.S_ISREG(info.st_mode):
            return None, "nonregular_file"
        if info.st_nlink != 1:
            return None, "hardlink"
        identity = _identity(info)
        if expected is not None and identity != expected:
            return None, "file_changed"
        if _sidecars(descriptor, name):
            return None, "sqlite_sidecars_present"
        source_fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(source_fd, "rb") as source:
            if _identity(os.fstat(source.fileno())) != identity:
                return None, "file_changed"
            handles = Path("/proc/self/fd" if sys.platform == "linux" else "/dev/fd")
            pinned = handles / str(source.fileno())
            if not _unchanged(descriptor, name, identity):
                return None, "file_changed"
            # An empty/unversioned SQLite file must not become a valid backup
            # merely because snapshot() initializes an empty database.
            with closing(
                sqlite3.connect(pinned.as_uri() + "?mode=ro&immutable=1", uri=True)
            ) as raw:
                validate_schema(raw)
            if not _unchanged(descriptor, name, identity):
                return None, "file_changed"
            with snapshot(pinned, immutable=True) as restored:
                restored.verify()
            if _identity(os.fstat(source.fileno())) != identity or not _unchanged(
                descriptor, name, identity
            ):
                return None, "file_changed"
        return _Backup(name, timestamp, identity), None
    except DiscoveryError as exc:
        reason = {
            "database_schema_newer": "schema_newer",
            "database_schema_invalid": "schema_invalid",
            "database_schema_unversioned": "schema_invalid",
            "database_integrity_failed": "integrity_failed",
            "database_foreign_key_failed": "integrity_failed",
        }.get(exc.code, "verification_failed")
        return None, reason
    except sqlite3.Error:
        return None, "corrupt_database"
    except OSError:
        return None, "unreadable_file"


def _durable(descriptor: int, backup: _Backup) -> None:
    source_fd = os.open(backup.name, os.O_RDONLY | _NOFOLLOW, dir_fd=descriptor)
    with os.fdopen(source_fd, "rb") as source:
        if _identity(os.fstat(source.fileno())) != backup.identity:
            raise DiscoveryError("backup_file_changed")
        os.fsync(source.fileno())
        if not _unchanged(descriptor, backup.name, backup.identity):
            raise DiscoveryError("backup_file_changed")
    os.fsync(descriptor)


def _rotate_locked(
    directory: Path, descriptor: int, policy: RetentionPolicy, apply: bool, reference: datetime
) -> dict[str, object]:
    valid: list[_Backup] = []
    preserved: list[dict[str, str]] = []
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise DiscoveryError("backup_directory_unreadable") from exc
    for name in names:
        candidate, reason = _verify(descriptor, name)
        if candidate is None:
            preserved.append({"name": _display(name), "reason": reason or "verification_failed"})
        else:
            valid.append(candidate)
    valid.sort(key=lambda backup: backup.name)
    valid.sort(key=lambda backup: backup.timestamp, reverse=True)
    keep = [
        backup
        for index, backup in enumerate(valid)
        if index < policy.keep_last
        or (reference - backup.timestamp).total_seconds() <= policy.max_age_days * 86_400
    ]
    keep_names = {backup.name for backup in keep}
    obsolete = [backup for backup in valid if backup.name not in keep_names]
    deleted: list[str] = []
    status = "rotated" if apply else "preview"
    blocked: str | None = None
    if not keep:
        status, blocked = "blocked", "no_valid_survivor"
    if apply:
        for backup in obsolete:
            if not _directory_unchanged(directory, descriptor):
                status, blocked = "blocked", "directory_changed"
                break
            survivor: _Backup | None = None
            for retained in keep:
                verified, _ = _verify(descriptor, retained.name, retained.identity)
                if verified is not None:
                    survivor = verified
                    break
            if survivor is None:
                status, blocked = "blocked", "survivor_verification_failed"
                break
            try:
                # This also makes an existing survivor durable when rotation is
                # invoked separately from managed backup creation.
                _durable(descriptor, survivor)
                verified, reason = _verify(descriptor, backup.name, backup.identity)
                if verified is None:
                    preserved.append({"name": backup.name, "reason": reason or "file_changed"})
                    continue
                if not _unchanged(descriptor, survivor.name, survivor.identity):
                    status, blocked = "blocked", "survivor_changed"
                    break
                if not _directory_unchanged(directory, descriptor):
                    status, blocked = "blocked", "directory_changed"
                    break
                if not _unchanged(descriptor, backup.name, backup.identity):
                    preserved.append({"name": backup.name, "reason": "file_changed"})
                    continue
                os.unlink(backup.name, dir_fd=descriptor)
                deleted.append(backup.name)
                os.fsync(descriptor)
            except (OSError, DiscoveryError):
                status, blocked = "blocked", "rotation_io_failed"
                break
    return {
        "status": status,
        "apply": apply,
        "reference_time": reference.isoformat(),
        "policy": asdict(policy),
        "valid": len(valid),
        "keep": [backup.name for backup in keep],
        "would_delete": [backup.name for backup in obsolete],
        "deleted": deleted,
        "preserved": preserved,
        "reason": blocked,
    }


def rotate_backups(
    directory: Path,
    policy: RetentionPolicy,
    *,
    apply: bool = False,
    as_of: datetime | None = None,
) -> dict[str, object]:
    if type(apply) is not bool:
        raise DiscoveryError("backup_apply_invalid")
    reference = _reference(as_of)
    with _directory_lock(directory) as descriptor:
        return _rotate_locked(directory, descriptor, policy, apply, reference)


def create_managed_backup(
    repository: Repository, directory: Path, policy: RetentionPolicy
) -> dict[str, object]:
    reference = _reference(None)
    name = f"waagd-backup-{reference.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4()}.sqlite3"
    with _directory_lock(directory, create=True) as descriptor:
        if not _directory_unchanged(directory, descriptor):
            raise DiscoveryError("backup_directory_changed")
        try:
            # Repository.backup owns exclusive creation and SQLite's online
            # backup API, including cleanup of its own incomplete target.
            repository.backup(directory / name)
            backup, _ = _verify(descriptor, name)
            if backup is None:
                raise DiscoveryError("backup_verification_failed")
            _durable(descriptor, backup)
        except OSError as exc:
            raise DiscoveryError("backup_creation_failed") from exc
        rotation = _rotate_locked(directory, descriptor, policy, True, reference)
    return {"status": "created", "created": name, "rotation": rotation}
