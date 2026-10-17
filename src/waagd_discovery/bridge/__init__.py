"""Data-only, offline host bridge. No Discovery database or service access."""

import fcntl
import hashlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from waagd_discovery.bridge import io
from waagd_discovery.bridge.io import BridgeError
from waagd_discovery.bridge.validation import (
    envelope,
    timestamp,
    validate_envelope,
    validate_export,
)

__all__ = ["Bridge", "BridgeError"]


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise BridgeError("clock_requires_timezone")
    return value.astimezone(UTC)


def _uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _checksum(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        io.encode({key: item for key, item in value.items() if key != "checksum"})
    ).hexdigest()


def _validate_state(value: Any) -> dict[str, Any]:
    try:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "version",
                "records",
                "pending",
                "snapshot",
                "acknowledged_ids",
                "dismissed_ids",
                "checksum",
            }
            or value["version"] != 1
            or value["checksum"] != _checksum(value)
        ):
            raise BridgeError("ledger_corrupt")
        records = value["records"]
        acknowledged = value["acknowledged_ids"]
        dismissed = value["dismissed_ids"]
        if (
            not isinstance(dismissed, list)
            or len(dismissed) > 10000
            or not all(_uuid(item) for item in dismissed)
            or len(set(dismissed)) != len(dismissed)
            or set(dismissed) & set(acknowledged)
        ):
            raise BridgeError("ledger_corrupt")
        if (
            not isinstance(records, dict)
            or len(records) > 10000
            or not isinstance(acknowledged, list)
        ):
            raise BridgeError("ledger_corrupt")
        if (
            len(acknowledged) > 10000
            or len(set(acknowledged)) != len(acknowledged)
            or not all(_uuid(item) for item in acknowledged)
        ):
            raise BridgeError("ledger_corrupt")
        for key, record in records.items():
            if (
                len(key) != 64
                or any(c not in "0123456789abcdef" for c in key)
                or not isinstance(record, dict)
                or set(record) != {"status", "delivery_id"}
                or record["status"] not in {"prepared", "delivered", "processed", "dismissed"}
                or not _uuid(record["delivery_id"])
            ):
                raise BridgeError("ledger_corrupt")
        validate_envelope(value["snapshot"])
        pending = value["pending"]
        if pending is not None:
            if (
                not isinstance(pending, dict)
                or set(pending) != {"phase", "envelope"}
                or pending["phase"] not in {"prepared", "delivered"}
                or pending["envelope"] != value["snapshot"]
            ):
                raise BridgeError("ledger_corrupt")
            delivery = pending["envelope"]["delivery_id"]
            if not delivery or delivery in acknowledged or delivery in dismissed:
                raise BridgeError("ledger_corrupt")
            keys = {item["candidate_key"] for item in pending["envelope"]["candidates"]}
            if len(keys) != len(pending["envelope"]["candidates"]):
                raise BridgeError("ledger_corrupt")
            for key in keys:
                if records.get(key) != {"status": pending["phase"], "delivery_id": delivery}:
                    raise BridgeError("ledger_corrupt")
            if {
                key
                for key, record in records.items()
                if record["status"] not in {"processed", "dismissed"}
            } != keys:
                raise BridgeError("ledger_corrupt")
        elif value["snapshot"]["candidates"] or any(
            record["status"] not in {"processed", "dismissed"} for record in records.values()
        ):
            raise BridgeError("ledger_corrupt")
        if {
            record["delivery_id"] for record in records.values() if record["status"] == "processed"
        } != set(acknowledged):
            raise BridgeError("ledger_corrupt")
        if {
            record["delivery_id"] for record in records.values() if record["status"] == "dismissed"
        } != set(dismissed):
            raise BridgeError("ledger_corrupt")
        return value
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise BridgeError("ledger_corrupt") from error


class Bridge:
    """One pending, durable delivery; explicit acknowledgement preserves URL history."""

    def __init__(self, export: Path, workspace_root: Path, state_dir: Path, limit: int = 10):
        self.export = Path(export)
        self.workspace_root = Path(workspace_root)
        self.state_dir = Path(state_dir)
        self.inbox_dir = self.workspace_root / "waagd-discovery"
        for path in (self.export, self.workspace_root, self.state_dir):
            io.path_parts(path)
        roots = (self.export.parent, self.workspace_root, self.state_dir)
        for index, root in enumerate(roots):
            for other in roots[index + 1 :]:
                if root == other or root in other.parents or other in root.parents:
                    raise BridgeError("roots_must_not_overlap")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BridgeError("limit_must_be_1_to_50")
        self.limit = limit

    def _verify(self, descriptor: int, path: Path, private: bool = False) -> None:
        current = io.open_directory(path, private=private)
        try:
            first, second = os.fstat(descriptor), os.fstat(current)
            if (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino):
                raise BridgeError("directory_identity_changed")
        finally:
            os.close(current)

    def _lock(self, descriptor: int, *, exclusive: bool) -> None:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BridgeError("bridge_busy") from error

    @contextmanager
    def _access(self, *, mutate: bool) -> Iterator[tuple[int, int]]:
        descriptors: list[int] = []
        try:
            workspace = io.open_directory(self.workspace_root)
            descriptors.append(workspace)
            if os.fstat(workspace).st_uid != os.geteuid() or os.fstat(workspace).st_mode & 0o022:
                raise BridgeError("workspace_requires_owner_without_group_world_write")
            self._verify(workspace, self.workspace_root)
            inbox = io.open_directory(self.inbox_dir, create=mutate, private=True)
            descriptors.append(inbox)
            state = io.open_directory(self.state_dir, create=mutate, private=True)
            descriptors.append(state)
            flags = os.O_RDWR if mutate else os.O_RDONLY
            try:
                lock = os.open("bridge.lock", flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=state)
            except FileNotFoundError:
                if not mutate:
                    raise
                for name in ("ledger.json", "initialized.json"):
                    try:
                        os.stat(name, dir_fd=state, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise BridgeError("initialized_lock_missing") from None
                try:
                    lock = os.open(
                        "bridge.lock",
                        flags | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=state,
                    )
                    os.fsync(state)
                except FileExistsError:
                    lock = os.open(
                        "bridge.lock", flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=state
                    )
            descriptors.append(lock)
            io.check_file(os.fstat(lock), private=True)
            self._lock(lock, exclusive=mutate)
            if io.identity(os.fstat(lock)) != io.identity(
                os.stat("bridge.lock", dir_fd=state, follow_symlinks=False)
            ):
                raise BridgeError("lock_identity_changed")
            self._verify(state, self.state_dir, True)
            self._verify(inbox, self.inbox_dir, True)
            yield state, inbox
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _load(self, state: int, inbox: int | None) -> dict[str, Any] | None:
        initialized = False
        try:
            marker = io.read_file(state, "initialized.json", 64, private=True)
            if marker != b'{"version":1}\n':
                raise BridgeError("initialization_marker_corrupt")
            initialized = True
        except FileNotFoundError:
            pass
        try:
            return _validate_state(
                io.decode(io.read_file(state, "ledger.json", 8 * 1024 * 1024, private=True))
            )
        except FileNotFoundError:
            if initialized:
                raise BridgeError("initialized_ledger_missing") from None
            if inbox is None:
                return None
            try:
                os.stat("inbox.json", dir_fd=inbox, follow_symlinks=False)
            except FileNotFoundError:
                return None
            raise BridgeError("untracked_existing_inbox") from None

    def _save(self, state: int, ledger: dict[str, Any]) -> None:
        self._verify(state, self.state_dir, True)
        ledger["checksum"] = _checksum(ledger)
        _validate_state(ledger)
        io.atomic_write(state, "ledger.json", io.encode(ledger))
        self._mark_initialized(state)

    def _mark_initialized(self, state: int) -> None:
        self._verify(state, self.state_dir, True)
        # This durable marker distinguishes a lost ledger from a fresh install.
        # Write it after the first ledger and before any publication.
        try:
            marker = io.read_file(state, "initialized.json", 64, private=True)
            if marker != b'{"version":1}\n':
                raise BridgeError("initialization_marker_corrupt")
        except FileNotFoundError:
            io.atomic_write(state, "initialized.json", b'{"version":1}\n')

    def _publish(self, inbox: int, snapshot: dict[str, Any]) -> None:
        self._verify(inbox, self.inbox_dir, True)
        data = io.encode(snapshot)
        try:
            if io.read_file(inbox, "inbox.json", 128 * 1024, private=True) == data:
                return
        except FileNotFoundError:
            pass
        io.atomic_write(inbox, "inbox.json", data)

    def _read_export(self, now: datetime) -> tuple[list[dict[str, Any]], datetime]:
        descriptor = io.open_directory(self.export.parent)
        try:
            data = io.read_file(descriptor, self.export.name, 8 * 1024 * 1024)
            self._verify(descriptor, self.export.parent)
            return validate_export(io.decode(data), now)
        finally:
            os.close(descriptor)

    def _summary(self, ledger: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
        if ledger is None:
            return {
                "status": "uninitialized",
                "pending_delivery_id": None,
                "prepared": 0,
                "delivered": 0,
                "processed": 0,
                "dismissed": 0,
                "known": 0,
            }
        records = ledger["records"]
        pending = ledger["pending"]
        status = "idle"
        if pending is not None:
            status = (
                "pending_expired"
                if timestamp(pending["envelope"]["expires_at"]) <= now
                else pending["phase"]
            )
        return {
            "status": status,
            "pending_delivery_id": pending["envelope"]["delivery_id"] if pending else None,
            "prepared": sum(item["status"] == "prepared" for item in records.values()),
            "delivered": sum(item["status"] == "delivered" for item in records.values()),
            "processed": sum(item["status"] == "processed" for item in records.values()),
            "dismissed": sum(item["status"] == "dismissed" for item in records.values()),
            "known": len(records),
        }

    def sync(self, now: datetime | None = None) -> dict[str, Any]:
        clock = _utc(now)
        with self._access(mutate=True) as (state, inbox):
            ledger = self._load(state, inbox)
            if ledger is not None:
                self._mark_initialized(state)
            if ledger is not None and ledger["pending"] is not None:
                self._publish(inbox, ledger["snapshot"])
                if ledger["pending"]["phase"] == "prepared":
                    ledger["pending"]["phase"] = "delivered"
                    for record in ledger["records"].values():
                        if record["status"] == "prepared":
                            record["status"] = "delivered"
                    self._save(state, ledger)
                return self._summary(ledger, clock)
            # An ACK is durable before clearing the inbox. Repair its publication
            # even when the next export is missing or invalid.
            if ledger is not None:
                self._publish(inbox, ledger["snapshot"])
            candidates, expiry = self._read_export(clock)
            if ledger is None:
                ledger = {
                    "version": 1,
                    "records": {},
                    "pending": None,
                    "snapshot": envelope(None, [], clock, clock + timedelta(hours=2)),
                    "acknowledged_ids": [],
                    "dismissed_ids": [],
                    "checksum": "",
                }
            selected = [
                item for item in candidates if item["candidate_key"] not in ledger["records"]
            ][: self.limit]
            if len(ledger["records"]) + len(selected) > 10000:
                raise BridgeError("ledger_capacity_reached")
            if selected:
                delivery = str(uuid.uuid4())
                # Trim projection batch until its encoded envelope fits. No
                # unbounded backlog or automatic pagination is created.
                while True:
                    try:
                        snapshot = envelope(delivery, selected, clock, expiry)
                        break
                    except BridgeError as error:
                        if str(error) != "envelope_too_large" or len(selected) == 1:
                            raise
                        selected.pop()
                ledger["snapshot"] = snapshot
                ledger["pending"] = {"phase": "prepared", "envelope": snapshot}
                for item in selected:
                    ledger["records"][item["candidate_key"]] = {
                        "status": "prepared",
                        "delivery_id": delivery,
                    }
                self._save(state, ledger)
                self._publish(inbox, snapshot)
                ledger["pending"]["phase"] = "delivered"
                for item in selected:
                    ledger["records"][item["candidate_key"]]["status"] = "delivered"
                self._save(state, ledger)
            else:
                # Keep an identical empty snapshot stable across repeated sync.
                self._save(state, ledger)
                self._publish(inbox, ledger["snapshot"])
            return self._summary(ledger, clock)

    def ack(self, delivery_id: str, now: datetime | None = None) -> dict[str, Any]:
        return self._complete(delivery_id, "processed", now)

    def dismiss(self, delivery_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Explicitly decline a batch without claiming editorial processing."""
        return self._complete(delivery_id, "dismissed", now)

    def _complete(self, delivery_id: str, terminal: str, now: datetime | None) -> dict[str, Any]:
        clock = _utc(now)
        if not _uuid(delivery_id):
            raise BridgeError("invalid_delivery_id")
        with self._access(mutate=True) as (state, inbox):
            ledger = self._load(state, inbox)
            if ledger is None:
                raise BridgeError("unknown_delivery_id")
            own_history = "acknowledged_ids" if terminal == "processed" else "dismissed_ids"
            other_history = "dismissed_ids" if terminal == "processed" else "acknowledged_ids"
            if delivery_id in ledger[other_history]:
                raise BridgeError("delivery_already_completed_differently")
            if delivery_id in ledger[own_history]:
                self._publish(inbox, ledger["snapshot"])
                return self._summary(ledger, clock)
            pending = ledger["pending"]
            if pending is None or pending["envelope"]["delivery_id"] != delivery_id:
                raise BridgeError("unknown_delivery_id")
            for record in ledger["records"].values():
                if record["delivery_id"] == delivery_id:
                    record["status"] = terminal
            ledger[own_history].append(delivery_id)
            ledger["pending"] = None
            ledger["snapshot"] = envelope(None, [], clock, clock + timedelta(hours=2))
            self._save(state, ledger)
            self._publish(inbox, ledger["snapshot"])
            return self._summary(ledger, clock)

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        clock = _utc(now)
        descriptors: list[int] = []
        ledger = None
        result: dict[str, Any]
        try:
            workspace = io.open_directory(self.workspace_root)
            descriptors.append(workspace)
            if os.fstat(workspace).st_uid != os.geteuid() or os.fstat(workspace).st_mode & 0o022:
                raise BridgeError("workspace_requires_owner_without_group_world_write")
            try:
                state = io.open_directory(self.state_dir, private=True)
            except FileNotFoundError:
                state = None
            if state is not None:
                descriptors.append(state)
                try:
                    lock = os.open(
                        "bridge.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=state
                    )
                except FileNotFoundError:
                    for name in ("ledger.json", "initialized.json"):
                        try:
                            os.stat(name, dir_fd=state, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        raise BridgeError("initialized_lock_missing") from None
                else:
                    descriptors.append(lock)
                    io.check_file(os.fstat(lock), private=True)
                    self._lock(lock, exclusive=False)
                    if io.identity(os.fstat(lock)) != io.identity(
                        os.stat("bridge.lock", dir_fd=state, follow_symlinks=False)
                    ):
                        raise BridgeError("lock_identity_changed")
                    self._verify(state, self.state_dir, True)
                    ledger = self._load(state, None)
            result = self._summary(ledger, clock)
            result["inbox_matches_ledger"] = False
            try:
                inbox = io.open_directory(self.inbox_dir, private=True)
            except FileNotFoundError:
                inbox = None
            if inbox is not None:
                descriptors.append(inbox)
                try:
                    current = io.read_file(inbox, "inbox.json", 128 * 1024, private=True)
                except FileNotFoundError:
                    pass
                else:
                    if ledger is None:
                        raise BridgeError("untracked_existing_inbox")
                    result["inbox_matches_ledger"] = current == io.encode(ledger["snapshot"])
                self._verify(inbox, self.inbox_dir, True)
            self._verify(workspace, self.workspace_root)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        try:
            candidates, _ = self._read_export(clock)
            result["export_status"] = "valid"
            result["export_candidates"] = len(candidates)
        except (BridgeError, OSError):
            result["export_status"] = "invalid_or_missing"
        return result
