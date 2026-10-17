"""Descriptor-relative, bounded filesystem operations for the data-only bridge."""

import json
import math
import os
import stat
import uuid
from pathlib import Path
from typing import Any


class BridgeError(ValueError):
    """A closed failure: no unsafe input is accepted or delivery history reset."""


def path_parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or ".." in path.parts:
        raise BridgeError("paths_must_be_absolute_without_parent_traversal")
    return path.parts[1:]


def open_directory(path: Path, *, create: bool = False, private: bool = False) -> int:
    parts = path_parts(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            if create and index == len(parts) - 1:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise BridgeError("private_directory_requires_owner_and_mode_0700")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def check_file(info: os.stat_result, *, private: bool) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BridgeError("file_must_be_regular_without_hardlinks")
    if info.st_mode & 0o022:
        raise BridgeError("file_must_not_be_group_or_world_writable")
    if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise BridgeError("private_file_requires_owner_and_mode_0600")


def read_file(directory: int, name: str, maximum: int, *, private: bool = False) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        check_file(before, private=private)
        if before.st_size > maximum:
            raise BridgeError("file_too_large")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise BridgeError("file_too_large")
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if identity(before) != identity(after) or identity(after) != identity(named):
            raise BridgeError("file_changed_during_read")
        return data
    finally:
        os.close(descriptor)


def encode(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def decode(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise BridgeError("duplicate_json_key")
            result[key] = value
        return result

    def reject(value: str) -> None:
        raise BridgeError("nonfinite_json_number")

    def finite(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise BridgeError("nonfinite_json_number")
        return result

    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject, parse_float=finite
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise BridgeError("invalid_json") from error


def atomic_write(directory: int, name: str, data: bytes) -> None:
    """Reject unsafe existing targets and durably replace within one pinned directory."""
    before = None
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        check_file(before, private=True)
    except FileNotFoundError:
        pass
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if (before is None) != (current is None) or (
            before is not None and current is not None and identity(before) != identity(current)
        ):
            raise BridgeError("target_changed_during_write")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
