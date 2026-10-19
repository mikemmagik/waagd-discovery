"""Private, independently versioned editorial storage; never a Discovery database."""

import fcntl
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError, digest

SCHEMA_VERSION = 1
_SCHEMA = (
    "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "simulation INTEGER NOT NULL CHECK(simulation IN (0,1)))",
    "CREATE TABLE deliveries (delivery_id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL, "
    "schema_version INTEGER NOT NULL, received_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
    "status TEXT NOT NULL, processing_started_at TEXT, processing_finished_at TEXT, "
    "ack_status TEXT NOT NULL, error_code TEXT, payload TEXT NOT NULL)",
    "CREATE TABLE candidates (candidate_key TEXT PRIMARY KEY, source_url TEXT NOT NULL, "
    "status TEXT NOT NULL, verification_status TEXT, editorial_status TEXT, processed_at TEXT, "
    "current_revision INTEGER NOT NULL DEFAULT 0, requested_revision INTEGER NOT NULL DEFAULT 1, "
    "payload TEXT NOT NULL)",
    "CREATE TABLE delivery_candidates (delivery_id TEXT NOT NULL REFERENCES deliveries, "
    "candidate_key TEXT NOT NULL REFERENCES candidates, revision INTEGER NOT NULL, "
    "PRIMARY KEY(delivery_id,candidate_key))",
    "CREATE TABLE verifications (candidate_key TEXT NOT NULL REFERENCES candidates, "
    "revision INTEGER NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL, "
    "PRIMARY KEY(candidate_key,revision))",
    "CREATE TABLE decisions (candidate_key TEXT NOT NULL REFERENCES candidates, "
    "revision INTEGER NOT NULL, decision TEXT NOT NULL, request TEXT NOT NULL, "
    "result TEXT NOT NULL, created_at TEXT NOT NULL, "
    "PRIMARY KEY(candidate_key,revision))",
    "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, delivery_id TEXT REFERENCES deliveries, "
    "candidate_key TEXT REFERENCES candidates, operation TEXT NOT NULL, status TEXT NOT NULL, "
    "started_at TEXT NOT NULL, finished_at TEXT, error_code TEXT)",
    "CREATE TABLE ack_outbox (delivery_id TEXT PRIMARY KEY REFERENCES deliveries, "
    "payload_digest TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, "
    "acknowledged_at TEXT, error_code TEXT)",
)


def _schema(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type,name"
        )
    ]


def _expected_schema() -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in _SCHEMA:
            reference.execute(statement)
        return _schema(reference)


class EditorialStore:
    """A single-owner local store, serialized across Controller processes by flock.

    The containing directory is dedicated to this store, owner-only, and outside
    all supplied protected roots. Same-user malicious code is not an OS sandbox.
    SQLite uses FULL synchronous rollback-journal transactions and never resets
    missing, corrupt, future or simulation-incompatible initialized state.
    """

    def __init__(
        self,
        path: Path,
        *,
        simulation: bool,
        protected_roots: tuple[Path, ...] = (),
        readonly: bool = False,
    ):
        self.path = Path(path)
        self.simulation = simulation
        self.readonly = readonly
        io.path_parts(self.path)
        if not self.path.name or not self.path.suffix or self.path.parent == Path("/"):
            raise EditorialError("editorial_store_path_invalid")
        for root in protected_roots:
            io.path_parts(root)
            if self.path.parent == root or root in self.path.parent.parents:
                raise EditorialError("editorial_store_root_protected")
            if self.path.parent in root.parents:
                raise EditorialError("editorial_store_root_overlaps")
        self.directory = -1
        self.lock = -1
        self._connection: sqlite3.Connection | None = None
        self._inode: tuple[int, int] | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise EditorialError("editorial_store_not_open")
        return self._connection

    def _verify_paths(self) -> None:
        current = io.open_directory(self.path.parent, private=True)
        try:
            left, right = os.fstat(current), os.fstat(self.directory)
            if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
                raise EditorialError("editorial_store_directory_changed")
        finally:
            os.close(current)
        info = os.stat(self.path.name, dir_fd=self.directory, follow_symlinks=False)
        io.check_file(info, private=True)
        if self._inode != (info.st_dev, info.st_ino):
            raise EditorialError("editorial_store_file_changed")
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                io.check_file(
                    os.stat(self.path.name + suffix, dir_fd=self.directory, follow_symlinks=False),
                    private=True,
                )
            except FileNotFoundError:
                pass

    def __enter__(self) -> "EditorialStore":
        try:
            self.directory = io.open_directory(
                self.path.parent, create=not self.readonly, private=True
            )
            marker = self.path.name + ".initialized.json"
            initialized = False
            try:
                initialized = (
                    io.read_file(self.directory, marker, 128, private=True) == b'{"v":1}\n'
                )
                if not initialized:
                    raise EditorialError("editorial_store_marker_corrupt")
            except FileNotFoundError:
                pass
            flags = os.O_RDONLY if self.readonly else os.O_RDWR
            lock_created = False
            try:
                self.lock = os.open(
                    self.path.name + ".lock",
                    flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=self.directory,
                )
            except FileNotFoundError:
                if initialized or self.readonly:
                    raise EditorialError("editorial_store_lock_missing") from None
                self.lock = os.open(
                    self.path.name + ".lock",
                    flags | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.directory,
                )
                lock_created = True
                os.fsync(self.directory)
            io.check_file(os.fstat(self.lock), private=True)
            try:
                fcntl.flock(
                    self.lock, (fcntl.LOCK_SH if self.readonly else fcntl.LOCK_EX) | fcntl.LOCK_NB
                )
            except BlockingIOError:
                raise EditorialError("editorial_busy") from None
            named_lock = os.stat(
                self.path.name + ".lock", dir_fd=self.directory, follow_symlinks=False
            )
            if io.identity(named_lock) != io.identity(os.fstat(self.lock)):
                raise EditorialError("editorial_store_lock_changed")
            try:
                descriptor = os.open(
                    self.path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=self.directory,
                )
            except FileNotFoundError:
                if initialized or self.readonly or not lock_created:
                    raise EditorialError("editorial_store_database_missing") from None
                descriptor = os.open(
                    self.path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.directory,
                )
                os.fsync(descriptor)
                os.fsync(self.directory)
            try:
                info = os.fstat(descriptor)
                io.check_file(info, private=True)
                self._inode = (info.st_dev, info.st_ino)
                self._verify_paths()
                # Verify the pinned private directory and database identity before
                # and immediately after SQLite opens the trusted absolute path.
                self._connection = sqlite3.connect(
                    self.path.as_uri() + ("?mode=ro" if self.readonly else "?mode=rw"),
                    uri=True,
                    timeout=0,
                    isolation_level=None,
                )
                self._verify_paths()
            finally:
                os.close(descriptor)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA synchronous=FULL")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version < 0:
                raise EditorialError("editorial_schema_invalid")
            if version > SCHEMA_VERSION:
                raise EditorialError("editorial_schema_newer")
            if version == 0:
                if initialized or self.readonly or _schema(self.connection):
                    raise EditorialError("editorial_schema_invalid")
                with self.transaction():
                    for statement in _SCHEMA:
                        self.connection.execute(statement)
                    self.connection.execute("PRAGMA user_version=1")
                    self.connection.execute("INSERT INTO metadata VALUES (1,?)", (self.simulation,))
            if _schema(self.connection) != _expected_schema():
                raise EditorialError("editorial_schema_invalid")
            if self.connection.execute("PRAGMA quick_check").fetchall()[0][0] != "ok":
                raise EditorialError("editorial_store_corrupt")
            if self.connection.execute("PRAGMA foreign_key_check").fetchall():
                raise EditorialError("editorial_store_corrupt")
            metadata = self.connection.execute("SELECT * FROM metadata").fetchall()
            if len(metadata) != 1 or bool(metadata[0]["simulation"]) != self.simulation:
                raise EditorialError("editorial_simulation_mismatch")
            if not self.readonly:
                self.connection.execute("PRAGMA journal_mode=DELETE")
                self.connection.execute("PRAGMA synchronous=FULL")
                io.atomic_write(self.directory, marker, b'{"v":1}\n')
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        for descriptor in (self.lock, self.directory):
            if descriptor != -1:
                os.close(descriptor)
        self.lock = self.directory = -1

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.readonly:
            raise EditorialError("editorial_store_readonly")
        self._verify_paths()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._verify_paths()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def recover_attempts(self, clock: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE attempts SET status='interrupted',finished_at=?,"
                "error_code='process_interrupted' "
                "WHERE status='started'",
                (clock,),
            )

    def register(self, envelope: dict[str, Any], clock: str) -> None:
        delivery = envelope["delivery_id"]
        expected_digest = digest(envelope)
        with self.transaction():
            row = self.connection.execute(
                "SELECT payload_digest FROM deliveries WHERE delivery_id=?", (delivery,)
            ).fetchone()
            if row is not None:
                if row[0] != expected_digest:
                    raise EditorialError("delivery_payload_changed")
                return
            self.connection.execute(
                "INSERT INTO deliveries VALUES (?,?,?,?,?,'processing',?,NULL,'not_ready',NULL,?)",
                (
                    delivery,
                    expected_digest,
                    envelope["bridge_schema_version"],
                    clock,
                    envelope["expires_at"],
                    clock,
                    io.encode(envelope).decode(),
                ),
            )
            for candidate in envelope["candidates"]:
                key = candidate["candidate_key"]
                self.connection.execute(
                    "INSERT INTO candidates(candidate_key,source_url,status,payload) "
                    "VALUES (?,?,'pending',?) ON CONFLICT(candidate_key) DO NOTHING",
                    (key, candidate["url"], io.encode(candidate).decode()),
                )
                candidate_row = self.connection.execute(
                    "SELECT source_url,requested_revision FROM candidates WHERE candidate_key=?",
                    (key,),
                ).fetchone()
                if candidate_row[0] != candidate["url"]:
                    raise EditorialError("candidate_identity_collision")
                self.connection.execute(
                    "INSERT INTO delivery_candidates VALUES (?,?,?)",
                    (delivery, key, candidate_row[1]),
                )

    def start_attempt(
        self, delivery: str | None, key: str | None, operation: str, clock: str
    ) -> str:
        attempt = str(uuid.uuid4())
        with self.transaction():
            self.connection.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,'started',?,NULL,NULL)",
                (attempt, delivery, key, operation, clock),
            )
        return attempt

    def finish_attempt(self, attempt: str, clock: str, error: str | None = None) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE attempts SET status=?,finished_at=?,error_code=? WHERE attempt_id=?",
                ("failed" if error else "completed", clock, error, attempt),
            )

    def verification(self, key: str, revision: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result FROM verifications WHERE candidate_key=? AND revision=?", (key, revision)
        ).fetchone()
        return io.decode(row[0].encode()) if row is not None else None

    def save_verification(
        self, key: str, revision: int, result: dict[str, Any], clock: str
    ) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO verifications VALUES (?,?,?,?)",
                (key, revision, io.encode(result).decode(), clock),
            )
            self.connection.execute(
                "UPDATE candidates SET verification_status=? WHERE candidate_key=?",
                (result["verification_status"], key),
            )

    def has_decision(self, key: str, revision: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM decisions WHERE candidate_key=? AND revision=?", (key, revision)
            ).fetchone()
            is not None
        )

    def save_decision(
        self,
        key: str,
        revision: int,
        request: dict[str, Any],
        result: dict[str, Any],
        delivery: str | None,
        clock: str,
    ) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                (
                    key,
                    revision,
                    result["decision"],
                    io.encode(request).decode(),
                    io.encode(result).decode(),
                    clock,
                ),
            )
            self.connection.execute(
                "UPDATE candidates SET status='completed',editorial_status=?,processed_at=?,"
                "current_revision=? WHERE candidate_key=?",
                (result["decision"], clock, revision, key),
            )
            if delivery is not None:
                self._complete_delivery(delivery, clock)

    def _complete_delivery(self, delivery: str, clock: str) -> None:
        unfinished = self.connection.execute(
            "SELECT COUNT(*) FROM delivery_candidates dc LEFT JOIN decisions d ON "
            "d.candidate_key=dc.candidate_key AND d.revision=dc.revision "
            "WHERE dc.delivery_id=? AND d.candidate_key IS NULL",
            (delivery,),
        ).fetchone()[0]
        if unfinished:
            return
        ack_status = "disabled_simulation" if self.simulation else "pending"
        self.connection.execute(
            "UPDATE deliveries SET status='results_saved',processing_finished_at=?,ack_status=?,"
            "error_code=NULL WHERE delivery_id=? AND status!='acknowledged'",
            (clock, ack_status, delivery),
        )
        if not self.simulation:
            self.connection.execute(
                "INSERT INTO ack_outbox SELECT delivery_id,payload_digest,'pending',?,NULL,NULL "
                "FROM deliveries WHERE delivery_id=? ON CONFLICT(delivery_id) DO NOTHING",
                (clock, delivery),
            )

    def complete_reused(self, delivery: str, clock: str) -> None:
        with self.transaction():
            self._complete_delivery(delivery, clock)

    def required(self, delivery: str) -> list[tuple[str, int]]:
        return [
            tuple(row)
            for row in self.connection.execute(
                "SELECT candidate_key,revision FROM delivery_candidates WHERE delivery_id=? "
                "ORDER BY candidate_key",
                (delivery,),
            )
        ]

    def mark_delivery(self, delivery: str, status: str, error: str | None) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status=?,error_code=? WHERE delivery_id=?",
                (status, error, delivery),
            )

    def acknowledged(self, delivery: str, clock: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE ack_outbox SET status='acknowledged',acknowledged_at=?,error_code=NULL "
                "WHERE delivery_id=?",
                (clock, delivery),
            )
            self.connection.execute(
                "UPDATE deliveries SET status='acknowledged',ack_status='acknowledged',"
                "error_code=NULL "
                "WHERE delivery_id=?",
                (delivery,),
            )

    def reconsider(self, key: str, clock: str) -> dict[str, Any]:
        with self.transaction():
            row = self.connection.execute(
                "SELECT current_revision,requested_revision FROM candidates WHERE candidate_key=?",
                (key,),
            ).fetchone()
            if row is None or not row[0]:
                raise EditorialError("candidate_not_processed")
            pending = self.connection.execute(
                "SELECT 1 FROM delivery_candidates dc JOIN deliveries d ON "
                "d.delivery_id=dc.delivery_id WHERE dc.candidate_key=? "
                "AND d.ack_status NOT IN ('acknowledged','disabled_simulation')",
                (key,),
            ).fetchone()
            if pending is not None:
                raise EditorialError("reconsideration_delivery_unresolved")
            revision = row[1] if row[1] > row[0] else row[0] + 1
            self.connection.execute(
                "UPDATE candidates SET requested_revision=?,status='reconsideration_pending' "
                "WHERE candidate_key=?",
                (revision, key),
            )
            self.connection.execute(
                "INSERT INTO attempts VALUES (?,NULL,?,'reconsider','completed',?,?,NULL)",
                (str(uuid.uuid4()), key, clock, clock),
            )
            return {"candidate_key": key, "status": "reconsideration_pending", "revision": revision}

    def status(self) -> dict[str, Any]:
        counts = {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("deliveries", "candidates", "verifications", "decisions", "attempts")
        }
        pending = self.connection.execute(
            "SELECT COUNT(*) FROM ack_outbox WHERE status='pending'"
        ).fetchone()[0]
        return {
            "status": "initialized",
            "schema_version": SCHEMA_VERSION,
            "simulation": self.simulation,
            **counts,
            "pending_ack": pending,
        }
