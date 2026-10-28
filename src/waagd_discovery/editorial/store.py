"""Private, independently versioned editorial storage; never a Discovery database."""

import fcntl
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from typing import Any

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError, digest, validate_decision

SCHEMA_VERSION = 6
_SELECTION_RUNS = (
    "CREATE TABLE selection_runs (run_id TEXT PRIMARY KEY, delivery_id TEXT UNIQUE NOT NULL "
    "REFERENCES deliveries, utc_day TEXT NOT NULL, shortlist TEXT NOT NULL, selected TEXT, "
    "status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, error_code TEXT)"
)
_SELECTION_ITEMS = (
    "CREATE TABLE selection_items (run_id TEXT NOT NULL REFERENCES selection_runs, "
    "candidate_key TEXT NOT NULL REFERENCES candidates, candidate_hash TEXT NOT NULL, "
    "status TEXT NOT NULL, reason_code TEXT, editorial_status TEXT, export_status TEXT NOT NULL, "
    "PRIMARY KEY(run_id,candidate_key))"
)
_PUBLICATION_JOBS_V4 = (
    "CREATE TABLE publication_jobs (intent_id TEXT PRIMARY KEY, "
    "candidate_key TEXT NOT NULL REFERENCES candidates, revision INTEGER NOT NULL, "
    "content_hash TEXT NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL, "
    "site_outcome TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "error_code TEXT, UNIQUE(candidate_key,revision))"
)
_PUBLICATION_JOBS = (
    "CREATE TABLE publication_jobs (intent_id TEXT PRIMARY KEY, "
    "candidate_key TEXT NOT NULL REFERENCES candidates, revision INTEGER NOT NULL, "
    "delivery_id TEXT REFERENCES deliveries, content_hash TEXT NOT NULL, intent TEXT NOT NULL, "
    "status TEXT NOT NULL, site_outcome TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "error_code TEXT, UNIQUE(candidate_key,revision))"
)
_ATTEMPT_DIAGNOSTICS = (
    "CREATE TABLE attempt_diagnostics (attempt_id TEXT PRIMARY KEY REFERENCES attempts, "
    "delivery_id TEXT REFERENCES deliveries, candidate_key TEXT REFERENCES candidates, "
    "operation TEXT NOT NULL, failure_category TEXT NOT NULL, child_exit_status INTEGER, "
    "elapsed_ms INTEGER, terminal_receipt INTEGER NOT NULL CHECK(terminal_receipt IN (0,1)), "
    "route_attestation TEXT NOT NULL, cost_attestation TEXT NOT NULL, "
    "tool_attestation TEXT NOT NULL, terminal_disposition TEXT)"
)
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
    _ATTEMPT_DIAGNOSTICS,
    _SELECTION_RUNS,
    _SELECTION_ITEMS,
    _PUBLICATION_JOBS,
    "CREATE TABLE ack_outbox (delivery_id TEXT PRIMARY KEY REFERENCES deliveries, "
    "payload_digest TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, "
    "acknowledged_at TEXT, error_code TEXT)",
)
_SCHEMA_V5 = tuple(statement for statement in _SCHEMA if statement != _ATTEMPT_DIAGNOSTICS)
_SCHEMA_V4 = tuple(
    _PUBLICATION_JOBS_V4 if statement == _PUBLICATION_JOBS else statement
    for statement in _SCHEMA_V5
)


def _schema(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type,name"
        )
    ]


def _expected_schema(statements: tuple[str, ...] = _SCHEMA) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in statements:
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
        allow_legacy_readonly: bool = False,
    ):
        if type(allow_legacy_readonly) is not bool or (allow_legacy_readonly and not readonly):
            raise EditorialError("editorial_store_mode_invalid")
        self.path = Path(path)
        self.simulation = simulation
        self.readonly = readonly
        self.allow_legacy_readonly = allow_legacy_readonly
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
                    self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    self.connection.execute("INSERT INTO metadata VALUES (1,?)", (self.simulation,))
            if version == 1:
                if self.readonly:
                    raise EditorialError("editorial_schema_migration_required")
                with self.transaction():
                    self.connection.execute(_SELECTION_RUNS)
                    self.connection.execute(_SELECTION_ITEMS)
                    self.connection.execute("PRAGMA user_version=3")
                version = 3
            elif version == 2:
                if self.readonly:
                    raise EditorialError("editorial_schema_migration_required")
                with self.transaction():
                    self.connection.execute(_SELECTION_ITEMS)
                    self.connection.execute("PRAGMA user_version=3")
                version = 3
            if version == 3:
                if self.readonly:
                    raise EditorialError("editorial_schema_migration_required")
                try:
                    with self.transaction():
                        self.connection.execute(_PUBLICATION_JOBS_V4)
                        self.connection.execute("PRAGMA user_version=4")
                except sqlite3.Error:
                    raise EditorialError("editorial_schema_invalid") from None
                version = 4
            if version == 4:
                if self.readonly:
                    raise EditorialError("editorial_schema_migration_required")
                if _schema(self.connection) != _expected_schema(_SCHEMA_V4):
                    raise EditorialError("editorial_schema_invalid")
                try:
                    with self.transaction():
                        # v4 jobs did not record which delivery authorized an
                        # immutable Site identity.  Retain those jobs, but do
                        # not infer a binding from a potentially different
                        # delivery: an unbound historical job stays held until
                        # explicit operator reconciliation.
                        self.connection.execute(
                            "ALTER TABLE publication_jobs RENAME TO publication_jobs_v4"
                        )
                        self.connection.execute(_PUBLICATION_JOBS)
                        self.connection.execute(
                            "INSERT INTO publication_jobs "
                            "(intent_id,candidate_key,revision,delivery_id,content_hash,intent,"
                            "status,site_outcome,created_at,updated_at,error_code) "
                            "SELECT intent_id,candidate_key,revision,NULL,content_hash,intent,"
                            "status,site_outcome,created_at,updated_at,error_code "
                            "FROM publication_jobs_v4"
                        )
                        self.connection.execute("DROP TABLE publication_jobs_v4")
                        self.connection.execute("PRAGMA user_version=5")
                except sqlite3.Error:
                    raise EditorialError("editorial_schema_invalid") from None
                version = 5
            legacy_readonly = False
            if version == 5:
                if self.readonly and not self.allow_legacy_readonly:
                    raise EditorialError("editorial_schema_migration_required")
                if _schema(self.connection) != _expected_schema(_SCHEMA_V5):
                    raise EditorialError("editorial_schema_invalid")
                if self.readonly:
                    legacy_readonly = True
                else:
                    try:
                        with self.transaction():
                            self.connection.execute(_ATTEMPT_DIAGNOSTICS)
                            self.connection.execute("PRAGMA user_version=6")
                    except sqlite3.Error:
                        raise EditorialError("editorial_schema_invalid") from None
                    version = 6
            if not legacy_readonly and _schema(self.connection) != _expected_schema():
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

    def selection(self, delivery: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM selection_runs WHERE delivery_id=?", (delivery,)
        ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def start_selection(
        self,
        delivery: str,
        day: str,
        shortlist: list[dict[str, str]],
        rejected: dict[str, str],
        clock: str,
    ) -> dict[str, Any]:
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM selection_runs WHERE delivery_id=?", (delivery,)
            ).fetchone()
            if row is not None:
                return {key: row[key] for key in row.keys()}
            run = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO selection_runs VALUES (?,?,?,?,?,'started',?,NULL,NULL)",
                (run, delivery, day, io.encode(shortlist).decode(), None, clock),
            )
            for item in shortlist:
                key = item["candidate_key"]
                self.connection.execute(
                    "INSERT INTO selection_items VALUES "
                    "(?,?,?,'shortlisted',NULL,NULL,'not_exported')",
                    (run, key, digest(item)),
                )
            for key, reason in rejected.items():
                if key in {item["candidate_key"] for item in shortlist}:
                    raise EditorialError("selection_state_invalid")
                self.connection.execute(
                    "INSERT INTO selection_items VALUES (?,?,?,'rejected',?,NULL,'not_exported')",
                    (run, key, digest({"candidate_key": key}), reason),
                )
            return {
                "run_id": run,
                "delivery_id": delivery,
                "shortlist": io.encode(shortlist).decode(),
                "selected": None,
                "status": "started",
            }

    def finish_selection(
        self,
        delivery: str,
        selected: list[str],
        clock: str,
        error: str | None = None,
        reason_code: str = "not_selected",
    ) -> None:
        if not reason_code or not isinstance(reason_code, str):
            raise EditorialError("selection_state_invalid")
        with self.transaction():
            self.connection.execute(
                "UPDATE selection_runs SET selected=?,status=?,finished_at=?,error_code=? "
                "WHERE delivery_id=?",
                (
                    io.encode(selected).decode(),
                    "failed" if error else "completed",
                    clock,
                    error,
                    delivery,
                ),
            )
            run = self.connection.execute(
                "SELECT run_id FROM selection_runs WHERE delivery_id=?", (delivery,)
            ).fetchone()
            if run is None:
                raise EditorialError("selection_state_invalid")
            if error is not None:
                # A Gateway failure is not an editorial rejection. Preserve a
                # distinct durable state so operators can tell an empty valid
                # selection from a call that never produced one.
                self.connection.execute(
                    "UPDATE selection_items SET status='selection_failed',reason_code=? "
                    "WHERE run_id=? AND status='shortlisted'",
                    (error, run[0]),
                )
                return
            chosen = set(selected)
            if chosen:
                placeholders = ",".join("?" for _ in chosen)
                self.connection.execute(
                    "UPDATE selection_items SET status='selected',reason_code=NULL "
                    f"WHERE run_id=? AND candidate_key IN ({placeholders})",
                    (run[0], *sorted(chosen)),
                )
                self.connection.execute(
                    "UPDATE selection_items SET status='not_selected',reason_code=? "
                    "WHERE run_id=? AND status='shortlisted' "
                    f"AND candidate_key NOT IN ({placeholders})",
                    (reason_code, run[0], *sorted(chosen)),
                )
            else:
                self.connection.execute(
                    "UPDATE selection_items SET status='not_selected',reason_code=? "
                    "WHERE run_id=? AND status='shortlisted'",
                    (reason_code, run[0]),
                )

    def begin_selection_call(self, delivery: str) -> None:
        """Durably claim one non-idempotent Gateway turn before invoking it.

        A process death after the model receives a request leaves ``in_flight``.
        That ambiguous outcome is deliberately operator-required: replaying a
        creative selection call could create a different choice for the same
        run, so automatic recovery must not invoke it again.
        """

        with self.transaction():
            updated = self.connection.execute(
                "UPDATE selection_runs SET status='in_flight' "
                "WHERE delivery_id=? AND status='started'",
                (delivery,),
            ).rowcount
            if updated != 1:
                raise EditorialError("selection_state_invalid")

    def reopen_failed_selection(self, delivery: str) -> None:
        """Require an explicit operator action before retrying a known failure.

        ``in_flight`` calls are intentionally excluded: their outcome is
        ambiguous and must never be replayed from this store.
        """

        with self.transaction():
            run = self.connection.execute(
                "SELECT run_id,status FROM selection_runs WHERE delivery_id=?", (delivery,)
            ).fetchone()
            if run is None or run["status"] != "failed":
                raise EditorialError("selection_retry_not_allowed")
            if self.connection.execute(
                "SELECT 1 FROM decisions d JOIN delivery_candidates c "
                "ON c.candidate_key=d.candidate_key AND c.revision=d.revision "
                "WHERE c.delivery_id=?",
                (delivery,),
            ).fetchone():
                raise EditorialError("selection_retry_not_allowed")
            self.connection.execute(
                "UPDATE selection_runs SET selected=NULL,status='started',finished_at=NULL,"
                "error_code=NULL WHERE delivery_id=?",
                (delivery,),
            )
            self.connection.execute(
                "UPDATE selection_items SET status='shortlisted',reason_code=NULL,"
                "editorial_status=NULL,export_status='not_exported' "
                "WHERE run_id=? AND status='selection_failed'",
                (run["run_id"],),
            )

    def selected_today(self, day: str, *, excluding_delivery: str | None = None) -> int:
        query = (
            "SELECT COUNT(*) FROM selection_items i JOIN selection_runs r ON r.run_id=i.run_id "
            "WHERE r.utc_day=? AND r.status='completed' AND i.status='selected'"
        )
        params: tuple[str, ...] = (day,)
        if excluding_delivery is not None:
            query += " AND r.delivery_id!=?"
            params += (excluding_delivery,)
        return int(self.connection.execute(query, params).fetchone()[0])

    def mark_selection_editorial(
        self,
        delivery: str,
        key: str,
        status: str,
        export_status: str = "not_exported",
    ) -> None:
        if status not in {"completed", "failed", "rejected"} or export_status != "not_exported":
            raise EditorialError("selection_state_invalid")
        with self.transaction():
            self.connection.execute(
                "UPDATE selection_items SET editorial_status=?,export_status=? "
                "WHERE run_id=(SELECT run_id FROM selection_runs WHERE delivery_id=?) "
                "AND candidate_key=? AND status='selected'",
                (status, export_status, delivery, key),
            )

    def mark_selection_exported(self, key: str) -> None:
        """Record a completed data-only export for the selected candidate."""

        with self.transaction():
            self.connection.execute(
                "UPDATE selection_items SET export_status='exported' "
                "WHERE candidate_key=? AND status='selected'",
                (key,),
            )

    def publication_job(self) -> dict[str, Any] | None:
        """Return the oldest unresolved intent without changing its state.

        ``uncertain`` is deliberately included.  A caller must reconcile that
        stable identity with the Site before it may submit anything else.
        """

        row = self.connection.execute(
            "SELECT * FROM publication_jobs "
            "WHERE status IN ('pending','in_flight','uncertain','held') "
            "ORDER BY created_at,intent_id LIMIT 1"
        ).fetchone()
        return {key: row[key] for key in row.keys()} if row is not None else None

    def save_publication_job(
        self,
        intent_id: str,
        key: str,
        revision: int,
        delivery: str,
        content_hash: str,
        intent: dict[str, Any],
        clock: str,
    ) -> dict[str, Any]:
        """Durably prepare one immutable Site submission before it is exposed.

        Identity and bytes are bound together.  A revision can never acquire a
        second publication identity, including after a local crash.
        """

        try:
            # The Site digest intentionally includes the established nested
            # object order for reference records.  The general Bridge encoder
            # sorts keys, which would mutate those bytes on recovery.
            encoded = json.dumps(intent, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise EditorialError("publication_state_invalid") from None
        with self.transaction():
            existing = self.connection.execute(
                "SELECT * FROM publication_jobs WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["candidate_key"] != key
                    or existing["revision"] != revision
                    or existing["delivery_id"] != delivery
                    or existing["content_hash"] != content_hash
                    or existing["intent"] != encoded
                ):
                    raise EditorialError("publication_identity_changed")
                return {name: existing[name] for name in existing.keys()}
            try:
                self.connection.execute(
                    "INSERT INTO publication_jobs "
                    "(intent_id,candidate_key,revision,delivery_id,content_hash,intent,status,"
                    "site_outcome,created_at,updated_at,error_code) "
                    "VALUES (?,?,?,?,?,?,'pending',NULL,?,?,NULL)",
                    (intent_id, key, revision, delivery, content_hash, encoded, clock, clock),
                )
            except sqlite3.IntegrityError:
                raise EditorialError("publication_identity_changed") from None
            row = self.connection.execute(
                "SELECT * FROM publication_jobs WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                raise EditorialError("publication_state_invalid")
            return {name: row[name] for name in row.keys()}

    def begin_publication_submission(self, intent_id: str, clock: str) -> dict[str, Any]:
        """Claim a prepared submission before a non-idempotent Site invocation."""

        with self.transaction():
            updated = self.connection.execute(
                "UPDATE publication_jobs SET status='in_flight',updated_at=?,error_code=NULL "
                "WHERE intent_id=? AND status IN ('pending','held')",
                (clock, intent_id),
            ).rowcount
            if updated != 1:
                raise EditorialError("publication_submission_not_ready")
            row = self.connection.execute(
                "SELECT * FROM publication_jobs WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                raise EditorialError("publication_state_invalid")
            return {name: row[name] for name in row.keys()}

    def recover_publication_submissions(self, clock: str) -> int:
        """Turn interrupted Site calls into an explicit reconciliation requirement."""

        with self.transaction():
            return self.connection.execute(
                "UPDATE publication_jobs SET status='uncertain',updated_at=?,"
                "error_code='site_outcome_ambiguous' WHERE status='in_flight'",
                (clock,),
            ).rowcount

    def record_publication_outcome(self, intent_id: str, outcome: str, clock: str) -> None:
        """Persist only bounded Site outcomes for an already prepared identity."""

        states = {
            "created": "delivered",
            "duplicate": "delivered",
            "held": "held",
            "failed": "failed",
            "uncertain": "uncertain",
        }
        status = states.get(outcome)
        if status is None:
            raise EditorialError("publication_outcome_invalid")
        with self.transaction():
            row = self.connection.execute(
                "SELECT candidate_key,status FROM publication_jobs WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None or row["status"] not in {"in_flight", "uncertain"}:
                raise EditorialError("publication_outcome_invalid")
            self.connection.execute(
                "UPDATE publication_jobs SET status=?,site_outcome=?,updated_at=?,error_code=? "
                "WHERE intent_id=?",
                (
                    status,
                    outcome,
                    clock,
                    None if status == "delivered" else "site_" + outcome,
                    intent_id,
                ),
            )
            if status == "delivered":
                self.connection.execute(
                    "UPDATE selection_items SET export_status='delivered' "
                    "WHERE candidate_key=? AND status='selected'",
                    (row["candidate_key"],),
                )

    def retry_failed_publication(self, intent_id: str, clock: str) -> None:
        """Make an explicit, known Site failure retryable with the same identity."""

        with self.transaction():
            updated = self.connection.execute(
                "UPDATE publication_jobs SET status='pending',updated_at=?,error_code=NULL "
                "WHERE intent_id=? AND status='failed'",
                (clock, intent_id),
            ).rowcount
            if updated != 1:
                raise EditorialError("publication_retry_not_allowed")

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

    @staticmethod
    def _gateway_diagnostic_values(error: str, diagnostic: dict[str, Any]) -> tuple[Any, ...]:
        allowed_status = {"passed", "failed", "not_checked", "unknown"}
        category = diagnostic.get("failure_category")
        route = diagnostic.get("route_attestation")
        cost = diagnostic.get("cost_attestation")
        tools = diagnostic.get("tool_attestation")
        exit_status = diagnostic.get("child_exit_status")
        elapsed = diagnostic.get("elapsed_ms")
        terminal = diagnostic.get("terminal_receipt")
        disposition = diagnostic.get("terminal_disposition")
        if (
            not isinstance(error, str)
            or not error
            or not isinstance(category, str)
            or not category
            or route not in allowed_status
            or cost not in allowed_status
            or tools not in allowed_status
            or (exit_status is not None and (type(exit_status) is not int))
            or (elapsed is not None and (type(elapsed) is not int or elapsed < 0))
            or type(terminal) is not bool
            or disposition not in {None, "visible", "not-visible"}
        ):
            raise EditorialError("editorial_diagnostic_invalid")
        return category, exit_status, elapsed, int(terminal), route, cost, tools, disposition

    def finish_gateway_attempt(
        self, attempt: str, clock: str, error: str, diagnostic: dict[str, Any]
    ) -> None:
        """Finish one failed Gateway turn with bounded, non-content telemetry."""

        values = self._gateway_diagnostic_values(error, diagnostic)
        with self.transaction():
            row = self.connection.execute(
                "SELECT delivery_id,candidate_key,operation FROM attempts WHERE attempt_id=? "
                "AND status='started'",
                (attempt,),
            ).fetchone()
            if row is None:
                raise EditorialError("editorial_attempt_state_invalid")
            updated = self.connection.execute(
                "UPDATE attempts SET status='failed',finished_at=?,error_code=? WHERE attempt_id=? "
                "AND status='started'",
                (clock, error, attempt),
            ).rowcount
            if updated != 1:
                raise EditorialError("editorial_attempt_state_invalid")
            self.connection.execute(
                "INSERT INTO attempt_diagnostics VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt,
                    row["delivery_id"],
                    row["candidate_key"],
                    row["operation"],
                    *values,
                ),
            )

    def finish_fidelity_gateway_failure(
        self,
        editorial_attempt: str,
        fidelity_attempt: str,
        clock: str,
        error: str,
        diagnostic: dict[str, Any],
    ) -> None:
        """Atomically retain a completed draft and failed Gateway fidelity telemetry."""

        values = self._gateway_diagnostic_values(error, diagnostic)
        with self.transaction():
            completed = self.connection.execute(
                "UPDATE attempts SET status='completed',finished_at=?,error_code=NULL "
                "WHERE attempt_id=? AND status='started'",
                (clock, editorial_attempt),
            ).rowcount
            if completed != 1:
                raise EditorialError("editorial_attempt_state_invalid")
            row = self.connection.execute(
                "SELECT delivery_id,candidate_key,operation FROM attempts WHERE attempt_id=? "
                "AND status='started'",
                (fidelity_attempt,),
            ).fetchone()
            if row is None:
                raise EditorialError("editorial_attempt_state_invalid")
            failed = self.connection.execute(
                "UPDATE attempts SET status='failed',finished_at=?,error_code=? WHERE attempt_id=? "
                "AND status='started'",
                (clock, error, fidelity_attempt),
            ).rowcount
            if failed != 1:
                raise EditorialError("editorial_attempt_state_invalid")
            self.connection.execute(
                "INSERT INTO attempt_diagnostics VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fidelity_attempt,
                    row["delivery_id"],
                    row["candidate_key"],
                    row["operation"],
                    *values,
                ),
            )

    def finish_attempts(self, attempts: tuple[tuple[str, str | None], ...], clock: str) -> None:
        """Atomically finish related attempts without inventing a partial outcome."""

        with self.transaction():
            for attempt, error in attempts:
                updated = self.connection.execute(
                    "UPDATE attempts SET status=?,finished_at=?,error_code=? "
                    "WHERE attempt_id=? AND status='started'",
                    ("failed" if error else "completed", clock, error, attempt),
                ).rowcount
                if updated != 1:
                    raise EditorialError("editorial_attempt_state_invalid")

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

    def save_terminal_rejection(
        self,
        key: str,
        revision: int,
        request: dict[str, Any],
        result: dict[str, Any],
        delivery: str,
        attempts: tuple[tuple[str, str | None], ...],
        clock: str,
    ) -> None:
        """Atomically retain a host rejection and its completed-turn evidence.

        A deterministic invalid result is final only after its REJECTED
        decision, safe attempt codes, selection state, and delivery completion
        are durable together.  This method is never used for uncertain or
        unattested Gateway outcomes.
        """

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
            for attempt, error in attempts:
                updated = self.connection.execute(
                    "UPDATE attempts SET status=?,finished_at=?,error_code=? "
                    "WHERE attempt_id=? AND status='started'",
                    ("failed" if error else "completed", clock, error, attempt),
                ).rowcount
                if updated != 1:
                    raise EditorialError("editorial_attempt_state_invalid")
            self.connection.execute(
                "UPDATE selection_items SET editorial_status='rejected',"
                "export_status='not_exported' "
                "WHERE run_id=(SELECT run_id FROM selection_runs WHERE delivery_id=?) "
                "AND candidate_key=? AND status='selected'",
                (delivery, key),
            )
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

    def persisted_delivery(self, delivery: str) -> tuple[str, str] | None:
        """Return the private durable envelope bytes and digest without mutation."""

        row = self.connection.execute(
            "SELECT payload_digest,payload FROM deliveries WHERE delivery_id=?", (delivery,)
        ).fetchone()
        return None if row is None else (row["payload_digest"], row["payload"])

    def mark_delivery(self, delivery: str, status: str, error: str | None) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status=?,error_code=? WHERE delivery_id=?",
                (status, error, delivery),
            )

    def begin_expired_quarantine(self, envelope: dict[str, Any], clock: str) -> bool:
        """Durably reserve a proven-unused expired delivery for Bridge quarantine.

        Any evidence that a Gateway call, ACK, publication intent, or Site
        result may exist is deliberately an operator case.  This method does
        not delete candidate history and never creates an ACK outbox entry.
        """

        delivery = envelope["delivery_id"]
        expected = digest(envelope)
        with self.transaction():
            row = self.connection.execute(
                "SELECT payload_digest,status,ack_status FROM deliveries WHERE delivery_id=?",
                (delivery,),
            ).fetchone()
            if row is not None:
                if row["payload_digest"] != expected:
                    raise EditorialError("delivery_payload_changed")
                if row["status"] == "quarantine_pending":
                    return True
                if row["status"] == "quarantined" or row["ack_status"] != "not_ready":
                    return False
                if row["status"] == "operator_required":
                    return False
                started = self.connection.execute(
                    "SELECT 1 FROM attempts WHERE delivery_id=? LIMIT 1", (delivery,)
                ).fetchone()
                decisions = self.connection.execute(
                    "SELECT 1 FROM decisions d JOIN delivery_candidates dc ON "
                    "dc.candidate_key=d.candidate_key AND dc.revision=d.revision "
                    "WHERE dc.delivery_id=? LIMIT 1",
                    (delivery,),
                ).fetchone()
                if started is not None or decisions is not None:
                    return False
            else:
                self.connection.execute(
                    "INSERT INTO deliveries VALUES "
                    "(?,?,?,?,?,'processing',?,NULL,'not_ready',NULL,?)",
                    (
                        delivery,
                        expected,
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
                        "SELECT source_url,requested_revision FROM candidates "
                        "WHERE candidate_key=?",
                        (key,),
                    ).fetchone()
                    if candidate_row["source_url"] != candidate["url"]:
                        raise EditorialError("candidate_identity_collision")
                    self.connection.execute(
                        "INSERT INTO delivery_candidates VALUES (?,?,?)",
                        (delivery, key, candidate_row["requested_revision"]),
                    )
            if (
                self.connection.execute(
                    "SELECT 1 FROM ack_outbox WHERE delivery_id=?", (delivery,)
                ).fetchone()
                is not None
            ):
                return False
            if (
                self.connection.execute(
                    "SELECT 1 FROM publication_jobs WHERE delivery_id=? LIMIT 1", (delivery,)
                ).fetchone()
                is not None
            ):
                return False
            self.connection.execute(
                "UPDATE deliveries SET status='quarantine_pending',"
                "error_code='delivery_expired_quarantine_pending' WHERE delivery_id=?",
                (delivery,),
            )
            self.connection.execute(
                "UPDATE candidates SET status='quarantined',editorial_status='quarantined',"
                "processed_at=? WHERE candidate_key IN "
                "(SELECT candidate_key FROM delivery_candidates WHERE delivery_id=?)",
                (clock, delivery),
            )
            return True

    def finish_expired_quarantine(self, delivery: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status='quarantined',"
                "processing_finished_at=COALESCE(processing_finished_at,received_at),"
                "error_code='delivery_expired_quarantined' WHERE delivery_id=? "
                "AND status='quarantine_pending'",
                (delivery,),
            )

    def fail_expired_quarantine(self, delivery: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status='operator_required',"
                "error_code='delivery_expired_quarantine_uncertain' WHERE delivery_id=? "
                "AND status='quarantine_pending'",
                (delivery,),
            )

    def _begin_selection_abandon(
        self,
        envelope: dict[str, Any],
        clock: str,
        *,
        selection_error: str,
        pending_status: str,
        pending_error: str,
        completed_status: str,
    ) -> bool:
        """Reserve exactly one expired terminal selection failure for abandon.

        The selection record and its failed attempt remain untouched so this
        cannot be mistaken for a valid model choice.  Only an operator-required
        delivery with the one expected ambiguous selection attempt may proceed;
        any possible ACK, decision, PublicationIntent, Site job, or other
        Gateway operation remains fail-closed.
        """

        delivery = envelope["delivery_id"]
        expected = digest(envelope)
        with self.transaction():
            row = self.connection.execute(
                "SELECT payload_digest,payload,status,ack_status,error_code FROM deliveries "
                "WHERE delivery_id=?",
                (delivery,),
            ).fetchone()
            if row is None or row["payload_digest"] != expected:
                raise EditorialError("delivery_payload_changed")
            try:
                persisted = io.decode(row["payload"].encode())
            except (AttributeError, io.BridgeError, UnicodeError):
                raise EditorialError("persisted_delivery_invalid") from None
            if not isinstance(persisted, dict) or digest(persisted) != expected:
                raise EditorialError("persisted_delivery_invalid")
            if row["status"] == pending_status:
                return True
            if row["status"] == completed_status:
                return False
            if (
                row["status"] != "operator_required"
                or row["ack_status"] != "not_ready"
                or row["error_code"] != selection_error
            ):
                return False
            run = self.connection.execute(
                "SELECT status,selected,finished_at,error_code FROM selection_runs "
                "WHERE delivery_id=?",
                (delivery,),
            ).fetchone()
            if run is None:
                return False
            if selection_error == "selection_outcome_unknown":
                if (
                    run["status"] != "in_flight"
                    or run["selected"] is not None
                    or run["finished_at"] is not None
                    or run["error_code"] is not None
                ):
                    return False
            else:
                try:
                    selected = io.decode(run["selected"].encode())
                except (AttributeError, io.BridgeError, UnicodeError):
                    return False
                if (
                    run["status"] != "failed"
                    or selected != []
                    or run["finished_at"] is None
                    or run["error_code"] != selection_error
                ):
                    return False
            candidate_keys = {
                entry[0]
                for entry in self.connection.execute(
                    "SELECT candidate_key FROM delivery_candidates WHERE delivery_id=?", (delivery,)
                ).fetchall()
            }
            attempts = self.connection.execute(
                "SELECT operation,status,error_code,candidate_key,finished_at FROM attempts "
                "WHERE delivery_id=?",
                (delivery,),
            ).fetchall()
            selections = []
            for attempt in attempts:
                operation, status, error, candidate_key, finished = tuple(attempt)
                if operation == "selection":
                    selections.append((status, error, candidate_key, finished))
                    continue
                if operation != "verification" or candidate_key not in candidate_keys:
                    return False
                if finished is None:
                    return False
                if (status == "completed" and error is None) or (
                    status == "failed" and error == "source_verification_failed"
                ):
                    continue
                return False
            if len(selections) != 1:
                return False
            status, error, candidate_key, finished = selections[0]
            if (status, error, candidate_key) != ("failed", selection_error, None) or (
                finished is None
            ):
                return False
            if (
                self.connection.execute(
                    "SELECT 1 FROM ack_outbox WHERE delivery_id=?", (delivery,)
                ).fetchone()
                is not None
                or self.connection.execute(
                    "SELECT 1 FROM decisions d JOIN delivery_candidates dc ON "
                    "dc.candidate_key=d.candidate_key AND dc.revision=d.revision "
                    "WHERE dc.delivery_id=? LIMIT 1",
                    (delivery,),
                ).fetchone()
                is not None
                or self.connection.execute(
                    "SELECT 1 FROM publication_jobs WHERE delivery_id=? LIMIT 1", (delivery,)
                ).fetchone()
                is not None
                or self.connection.execute(
                    "SELECT 1 FROM publication_jobs p JOIN delivery_candidates dc ON "
                    "dc.candidate_key=p.candidate_key AND dc.revision=p.revision "
                    "WHERE dc.delivery_id=? LIMIT 1",
                    (delivery,),
                ).fetchone()
                is not None
                or self.connection.execute(
                    "SELECT 1 FROM selection_items WHERE run_id=(SELECT run_id FROM "
                    "selection_runs WHERE delivery_id=?) AND "
                    "(editorial_status IS NOT NULL OR export_status!='not_exported') LIMIT 1",
                    (delivery,),
                ).fetchone()
                is not None
            ):
                return False
            self.connection.execute(
                "UPDATE deliveries SET status=?,error_code=? WHERE delivery_id=?",
                (pending_status, pending_error, delivery),
            )
            self.connection.execute(
                "UPDATE candidates SET status='selection_abandoned',"
                "editorial_status='selection_abandoned',processed_at=? WHERE candidate_key IN "
                "(SELECT candidate_key FROM delivery_candidates WHERE delivery_id=?)",
                (clock, delivery),
            )
            return True

    def begin_selection_abandon(self, envelope: dict[str, Any], clock: str) -> bool:
        return self._begin_selection_abandon(
            envelope,
            clock,
            selection_error="selection_outcome_unknown",
            pending_status="selection_abandon_pending",
            pending_error="selection_outcome_unknown_abandon_pending",
            completed_status="selection_abandoned",
        )

    def begin_selection_attestation_abandon(self, envelope: dict[str, Any], clock: str) -> bool:
        return self._begin_selection_abandon(
            envelope,
            clock,
            selection_error="selection_attestation_missing",
            pending_status="selection_attestation_abandon_pending",
            pending_error="selection_attestation_missing_abandon_pending",
            completed_status="selection_attestation_abandoned",
        )

    def _finish_selection_abandon(
        self, delivery: str, *, pending_status: str, completed_status: str, completed_error: str
    ) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status=?,processing_finished_at="
                "COALESCE(processing_finished_at,received_at),error_code=? WHERE delivery_id=? "
                "AND status=?",
                (completed_status, completed_error, delivery, pending_status),
            )

    def _fail_selection_abandon(
        self, delivery: str, *, pending_status: str, uncertain_error: str
    ) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status='operator_required',"
                "error_code=? WHERE delivery_id=? AND status=?",
                (uncertain_error, delivery, pending_status),
            )

    def finish_selection_abandon(self, delivery: str) -> None:
        self._finish_selection_abandon(
            delivery,
            pending_status="selection_abandon_pending",
            completed_status="selection_abandoned",
            completed_error="selection_outcome_unknown_abandoned",
        )

    def fail_selection_abandon(self, delivery: str) -> None:
        self._fail_selection_abandon(
            delivery,
            pending_status="selection_abandon_pending",
            uncertain_error="selection_outcome_unknown_abandon_uncertain",
        )

    def finish_selection_attestation_abandon(self, delivery: str) -> None:
        self._finish_selection_abandon(
            delivery,
            pending_status="selection_attestation_abandon_pending",
            completed_status="selection_attestation_abandoned",
            completed_error="selection_attestation_missing_abandoned",
        )

    def fail_selection_attestation_abandon(self, delivery: str) -> None:
        self._fail_selection_abandon(
            delivery,
            pending_status="selection_attestation_abandon_pending",
            uncertain_error="selection_attestation_missing_abandon_uncertain",
        )

    def preflight_partial_editorial_abandon(self, envelope: dict[str, Any], clock: str) -> bool:
        """Read-only proof for a partial-batch abandon, including schema v5."""

        return self.begin_partial_editorial_abandon(envelope, clock, preflight=True)

    def begin_partial_editorial_abandon(
        self, envelope: dict[str, Any], clock: str, *, preflight: bool = False
    ) -> bool:
        """Reserve an expired, mixed-result Gateway batch for terminal abandonment.

        This is intentionally narrower than ordinary recovery.  It preserves
        completed Fidelity-passed decisions but never exposes them for export,
        ACK or publication; one ambiguous failed Gateway turn means the whole
        delivery can only be dismissed after this proof succeeds.
        """

        if type(preflight) is not bool:
            raise EditorialError("editorial_recovery_mode_invalid")
        delivery = envelope["delivery_id"]
        expected = digest(envelope)
        transaction = nullcontext() if preflight else self.transaction()
        with transaction:
            row = self.connection.execute(
                "SELECT payload_digest,payload,status,ack_status,error_code FROM deliveries "
                "WHERE delivery_id=?",
                (delivery,),
            ).fetchone()
            if row is None or row["payload_digest"] != expected:
                raise EditorialError("delivery_payload_changed")
            try:
                persisted = io.decode(row["payload"].encode())
            except (AttributeError, io.BridgeError, UnicodeError):
                raise EditorialError("persisted_delivery_invalid") from None
            if not isinstance(persisted, dict) or digest(persisted) != expected:
                raise EditorialError("persisted_delivery_invalid")
            if row["status"] == "partial_editorial_abandon_pending":
                return True
            if row["status"] == "partial_editorial_batch_abandoned":
                return False
            if (
                row["status"] != "operator_required"
                or row["ack_status"] != "not_ready"
                or row["error_code"] != "editorial_gateway_attestation_invalid"
            ):
                return False
            run = self.connection.execute(
                "SELECT run_id,status,selected,finished_at,error_code FROM selection_runs "
                "WHERE delivery_id=?",
                (delivery,),
            ).fetchone()
            if run is None or run["status"] != "completed" or run["finished_at"] is None:
                return False
            try:
                selected = io.decode(run["selected"].encode())
            except (AttributeError, io.BridgeError, UnicodeError):
                return False
            if (
                not isinstance(selected, list)
                or len(selected) < 2
                or len(set(selected)) != len(selected)
                or not all(isinstance(key, str) for key in selected)
            ):
                return False
            selected_keys = set(selected)
            candidate_keys = {
                entry[0]
                for entry in self.connection.execute(
                    "SELECT candidate_key FROM delivery_candidates WHERE delivery_id=?", (delivery,)
                ).fetchall()
            }
            if selected_keys - candidate_keys:
                return False
            attempts = self.connection.execute(
                "SELECT operation,status,error_code,candidate_key,finished_at FROM attempts "
                "WHERE delivery_id=?",
                (delivery,),
            ).fetchall()
            verifications: set[str] = set()
            verification_counts: dict[str, int] = {}
            selections = 0
            failed_gateway: set[str] = set()
            completed_gateway: set[str] = set()
            completed_fidelity: set[str] = set()
            gateway_counts: dict[str, int] = {}
            fidelity_counts: dict[str, int] = {}
            deterministic_nonselected: set[str] = set()
            for attempt in attempts:
                operation, status, error, key, finished = tuple(attempt)
                if finished is None or status not in {"completed", "failed"}:
                    return False
                if operation == "verification":
                    if key not in candidate_keys or status != "completed" or error is not None:
                        return False
                    verification_counts[key] = verification_counts.get(key, 0) + 1
                    verifications.add(key)
                elif operation == "selection":
                    if key is not None or status != "completed" or error is not None:
                        return False
                    selections += 1
                elif operation == "editorial_gateway":
                    if key not in candidate_keys:
                        return False
                    gateway_counts[key] = gateway_counts.get(key, 0) + 1
                    if key not in selected_keys:
                        # A PARTIAL verification is deterministically converted
                        # into NEEDS_VERIFICATION by GatewayEditor without an LLM
                        # turn.  Preserve that completed policy outcome only;
                        # any error or uncertain attempt remains fail-closed.
                        if status != "completed" or error is not None:
                            return False
                        deterministic_nonselected.add(key)
                    elif status == "completed" and error is None:
                        completed_gateway.add(key)
                    elif status == "failed" and error == "editorial_gateway_attestation_invalid":
                        failed_gateway.add(key)
                    else:
                        return False
                elif operation == "fidelity":
                    if key not in selected_keys or status != "completed" or error is not None:
                        return False
                    fidelity_counts[key] = fidelity_counts.get(key, 0) + 1
                    completed_fidelity.add(key)
                else:
                    return False
            if (
                verifications != candidate_keys
                or any(count != 1 for count in verification_counts.values())
                or selections != 1
                or not failed_gateway
                or any(count != 1 for count in gateway_counts.values())
                or any(count != 1 for count in fidelity_counts.values())
            ):
                return False
            retained = selected_keys - failed_gateway
            if not retained or retained != completed_gateway or retained != completed_fidelity:
                return False
            for key in retained:
                decision = self.connection.execute(
                    "SELECT decision FROM decisions WHERE candidate_key=? AND revision="
                    "(SELECT revision FROM delivery_candidates WHERE delivery_id=? "
                    "AND candidate_key=?)",
                    (key, delivery, key),
                ).fetchone()
                if decision is None or decision["decision"] != "READY_FOR_REVIEW":
                    return False
            for key in failed_gateway:
                if self.connection.execute(
                    "SELECT 1 FROM decisions WHERE candidate_key=? AND revision="
                    "(SELECT revision FROM delivery_candidates WHERE delivery_id=? "
                    "AND candidate_key=?)",
                    (key, delivery, key),
                ).fetchone():
                    return False
            for key in candidate_keys - selected_keys:
                decision = self.connection.execute(
                    "SELECT d.decision,d.request,d.result,c.source_url FROM decisions d "
                    "JOIN delivery_candidates dc ON dc.candidate_key=d.candidate_key "
                    "AND dc.revision=d.revision JOIN candidates c "
                    "ON c.candidate_key=dc.candidate_key "
                    "WHERE dc.delivery_id=? AND dc.candidate_key=?",
                    (delivery, key),
                ).fetchone()
                if decision is None:
                    return False
                if key not in deterministic_nonselected:
                    # The remaining nonselected candidates were rejected by
                    # trusted selection before editorial evaluation.
                    if decision["decision"] != "REJECTED":
                        return False
                    continue
                if decision["decision"] != "NEEDS_VERIFICATION":
                    return False
                try:
                    request = io.decode(decision["request"].encode())
                    result = io.decode(decision["result"].encode())
                    validate_decision(result, request)
                except (AttributeError, io.BridgeError, EditorialError, UnicodeError):
                    return False
                if (
                    request.get("simulation") is not self.simulation
                    or request.get("candidate", {}).get("candidate_key") != key
                    or request["candidate"].get("url") != decision["source_url"]
                    or request.get("verification", {}).get("verification_status") != "PARTIAL"
                    or result.get("decision") != "NEEDS_VERIFICATION"
                    or result.get("model_identifier") != "trusted-policy-v1"
                ):
                    return False
            if (
                self.connection.execute(
                    "SELECT 1 FROM ack_outbox WHERE delivery_id=?", (delivery,)
                ).fetchone()
                is not None
                or self.connection.execute(
                    "SELECT 1 FROM publication_jobs p JOIN delivery_candidates dc ON "
                    "dc.candidate_key=p.candidate_key AND dc.revision=p.revision "
                    "WHERE dc.delivery_id=? LIMIT 1",
                    (delivery,),
                ).fetchone()
                is not None
                or self.connection.execute(
                    "SELECT 1 FROM selection_items WHERE run_id=? "
                    "AND export_status!='not_exported'",
                    (run["run_id"],),
                ).fetchone()
                is not None
            ):
                return False
            if preflight:
                return True
            self.connection.execute(
                "UPDATE deliveries SET status='partial_editorial_abandon_pending',"
                "error_code='partial_editorial_batch_abandon_pending' WHERE delivery_id=?",
                (delivery,),
            )
            for key in failed_gateway:
                self.connection.execute(
                    "UPDATE selection_items SET editorial_status='partial_batch_abandoned' "
                    "WHERE run_id=? AND candidate_key=? AND status='selected'",
                    (run["run_id"], key),
                )
            return True

    def finish_partial_editorial_abandon(self, delivery: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status='partial_editorial_batch_abandoned',"
                "processing_finished_at=COALESCE(processing_finished_at,received_at),"
                "error_code='partial_editorial_batch_abandoned' WHERE delivery_id=? "
                "AND status='partial_editorial_abandon_pending'",
                (delivery,),
            )

    def fail_partial_editorial_abandon(self, delivery: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE deliveries SET status='operator_required',"
                "error_code='partial_editorial_batch_abandon_uncertain' WHERE delivery_id=? "
                "AND status='partial_editorial_abandon_pending'",
                (delivery,),
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
