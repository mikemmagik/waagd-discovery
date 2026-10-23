"""Durable, one-shot coordinator for the separately configured editorial cycle.

This module deliberately does not collect feeds, contact a Gateway, or publish.
It owns only the calendar, singleton lock, and a small private SQLite journal.
The configured cycle command is the trusted integration boundary and must make
every downstream external action idempotent by its supplied ``cycle_id``.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from waagd_discovery.models import DiscoveryError

_SCHEMA_VERSION = 1
_MAX_OUTPUT = 16 * 1024
_DEFAULT_CYCLE_TIMEOUT_SECONDS = 28_800
_DEFAULT_INNER_CYCLE_SECONDS = 25_200
_MAX_CYCLE_TIMEOUT_SECONDS = 32_400
_OUTER_OVERHEAD_SECONDS = 300


def _utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise DiscoveryError("autonomous_clock_invalid")
    return value.astimezone(UTC)


def _stamp(now: datetime | None = None) -> str:
    return _utc(now).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class DailyPolicy:
    timezone: str = "Asia/Bangkok"
    daily_time: str = "08:00"
    maximum_attempts: int = 3

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
            parsed = time.fromisoformat(self.daily_time)
        except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
            raise DiscoveryError("autonomous_policy_invalid") from error
        if parsed.second or parsed.microsecond or len(self.daily_time) != 5:
            raise DiscoveryError("autonomous_policy_invalid")
        if (
            isinstance(self.maximum_attempts, bool)
            or not isinstance(self.maximum_attempts, int)
            or not 1 <= self.maximum_attempts <= 10
        ):
            raise DiscoveryError("autonomous_policy_invalid")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def local_time(self) -> time:
        return time.fromisoformat(self.daily_time)


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    lock = root / "autonomous.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DiscoveryError("autonomous_cycle_running") from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class DailyStore:
    """Private forward-only state journal, independent from Discovery SQLite."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = self.root / "autonomous.sqlite3"

    def open(self) -> sqlite3.Connection:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        connection = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > _SCHEMA_VERSION:
            connection.close()
            raise DiscoveryError("autonomous_schema_newer")
        if version == 0:
            connection.executescript(
                """
                BEGIN;
                CREATE TABLE settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
                    timezone TEXT NOT NULL,
                    daily_time TEXT NOT NULL,
                    maximum_attempts INTEGER NOT NULL,
                    installed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE cycles (
                    cycle_id TEXT PRIMARY KEY,
                    scheduled_local_date TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK (status IN
                        ('running', 'completed', 'retryable', 'operator_required', 'interrupted')),
                    attempts INTEGER NOT NULL CHECK (attempts >= 1),
                    result_code TEXT,
                    UNIQUE(scheduled_local_date, attempts)
                );
                PRAGMA user_version=1;
                COMMIT;
                """
            )
        elif version != _SCHEMA_VERSION:
            connection.close()
            raise DiscoveryError("autonomous_schema_invalid")
        return connection


CycleRunner = Callable[[str, str], dict[str, object]]


class DailyCoordinator:
    def __init__(self, root: Path, policy: DailyPolicy, runner: CycleRunner) -> None:
        self.store = DailyStore(root)
        self.policy = policy
        self.runner = runner

    def install(self, now: datetime | None = None) -> dict[str, object]:
        """Initialize state only; installing never enables writes or a scheduler."""
        with _lock(self.store.root):
            connection = self.store.open()
            try:
                stamp = _stamp(now)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT id FROM settings WHERE id=1").fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO settings VALUES (1, 0, 0, ?, ?, ?, ?, ?)",
                        (
                            self.policy.timezone,
                            self.policy.daily_time,
                            self.policy.maximum_attempts,
                            stamp,
                            stamp,
                        ),
                    )
                    status = "installed"
                else:
                    status = "already_installed"
                connection.commit()
                return {"status": status, "state": self.path_name}
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    @property
    def path_name(self) -> str:
        return self.store.path.name

    def _settings(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM settings WHERE id=1").fetchone()
        if row is None:
            raise DiscoveryError("autonomous_not_installed")
        try:
            DailyPolicy(row["timezone"], row["daily_time"], row["maximum_attempts"])
        except (DiscoveryError, TypeError):
            raise DiscoveryError("autonomous_state_invalid") from None
        return cast(sqlite3.Row, row)

    def _change(
        self, *, enabled: int | None = None, paused: int | None = None
    ) -> dict[str, object]:
        with _lock(self.store.root):
            connection = self.store.open()
            try:
                row = self._settings(connection)
                next_enabled = row["enabled"] if enabled is None else enabled
                next_paused = row["paused"] if paused is None else paused
                connection.execute(
                    "UPDATE settings SET enabled=?, paused=?, updated_at=? WHERE id=1",
                    (next_enabled, next_paused, _stamp()),
                )
                connection.commit()
                return {
                    "status": "updated",
                    "enabled": bool(next_enabled),
                    "paused": bool(next_paused),
                }
            finally:
                connection.close()

    def enable(self) -> dict[str, object]:
        return self._change(enabled=1, paused=0)

    def pause(self) -> dict[str, object]:
        return self._change(paused=1)

    def resume(self) -> dict[str, object]:
        return self._change(paused=0)

    def disable(self) -> dict[str, object]:
        return self._change(enabled=0, paused=1)

    def uninstall(self) -> dict[str, object]:
        """Disable without deleting the journal needed for audit/recovery."""
        return self.disable() | {"state_retained": True}

    def status(self) -> dict[str, object]:
        with _lock(self.store.root):
            connection = self.store.open()
            try:
                settings = self._settings(connection)
                latest = connection.execute(
                    "SELECT cycle_id, scheduled_local_date, status, attempts, result_code, "
                    "started_at, finished_at FROM cycles ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                return {
                    "status": "ok",
                    "enabled": bool(settings["enabled"]),
                    "paused": bool(settings["paused"]),
                    "timezone": settings["timezone"],
                    "daily_time": settings["daily_time"],
                    "latest_cycle": dict(latest) if latest else None,
                }
            finally:
                connection.close()

    def _due_date(self, now: datetime, last_completed: str | None) -> tuple[str, datetime] | None:
        local = now.astimezone(self.policy.zone)
        today_due = datetime.combine(local.date(), self.policy.local_time, self.policy.zone)
        if local >= today_due:
            date = local.date()
            due = today_due
        else:
            # A pre-slot wake may catch up only yesterday; it never drains a stale backlog.
            date = local.date() - timedelta(days=1)
            due = datetime.combine(date, self.policy.local_time, self.policy.zone)
        key = date.isoformat()
        if last_completed == key:
            return None
        return key, due.astimezone(UTC)

    def _recover_interrupted(self, connection: sqlite3.Connection, stamp: str) -> None:
        connection.execute(
            "UPDATE cycles SET status='operator_required', finished_at=?, "
            "result_code='process_interrupted' "
            "WHERE status='running'",
            (stamp,),
        )

    def run_due(self, now: datetime | None = None) -> dict[str, object]:
        """Run at most one due local-calendar cycle under a non-blocking singleton lock."""
        value = _utc(now)
        try:
            with _lock(self.store.root):
                return self._run_due_locked(value)
        except DiscoveryError as error:
            if error.code == "autonomous_cycle_running":
                return {"status": "busy"}
            raise

    def _run_due_locked(self, now: datetime) -> dict[str, object]:
        connection = self.store.open()
        try:
            settings = self._settings(connection)
            if not settings["enabled"]:
                return {"status": "disabled"}
            if settings["paused"]:
                return {"status": "paused"}
            stamp = _stamp(now)
            connection.execute("BEGIN IMMEDIATE")
            self._recover_interrupted(connection, stamp)
            completed = connection.execute(
                "SELECT scheduled_local_date FROM cycles "
                "WHERE status IN ('completed','operator_required') "
                "ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            due = self._due_date(now, completed[0] if completed else None)
            if due is None:
                connection.commit()
                return {"status": "not_due"}
            date, due_at = due
            previous = connection.execute(
                "SELECT attempts, status FROM cycles WHERE scheduled_local_date=? "
                "ORDER BY attempts DESC LIMIT 1",
                (date,),
            ).fetchone()
            attempts = (previous["attempts"] if previous else 0) + 1
            if attempts > settings["maximum_attempts"]:
                connection.commit()
                return {"status": "operator_required", "reason": "retry_limit", "date": date}
            cycle_id = str(uuid4())
            connection.execute(
                "INSERT INTO cycles(cycle_id, scheduled_local_date, due_at, started_at, "
                "status, attempts) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (cycle_id, date, _stamp(due_at), stamp, attempts),
            )
            connection.commit()
            code: str | None
            try:
                result = self.runner(cycle_id, date)
                status = result.get("status") if isinstance(result, dict) else None
                if status not in {"completed", "retryable", "operator_required"}:
                    status = "operator_required"
                    code = "cycle_result_invalid"
                else:
                    raw_code = result.get("code")
                    code = raw_code if isinstance(raw_code, str) else None
            except Exception:
                # The runner may have passed work to Gateway or Site before an
                # exception reached this coordinator.  Its outcome is unknown
                # and must never consume an automatic retry.
                status, code = "operator_required", "cycle_runner_interrupted"
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE cycles SET status=?, finished_at=?, result_code=? WHERE cycle_id=?",
                (status, _stamp(), code, cycle_id),
            )
            connection.commit()
            return {"status": status, "cycle_id": cycle_id, "date": date, "attempts": attempts}
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def subprocess_runner(
    command: tuple[str, ...],
    timeout_seconds: int = _DEFAULT_CYCLE_TIMEOUT_SECONDS,
    inner_cycle_seconds: int = _DEFAULT_INNER_CYCLE_SECONDS,
) -> CycleRunner:
    """Make a bounded no-shell runner without passing ambient credentials onward."""
    if (
        not command
        or not Path(command[0]).is_absolute()
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or isinstance(inner_cycle_seconds, bool)
        or not isinstance(inner_cycle_seconds, int)
        or not 1 <= inner_cycle_seconds <= _MAX_CYCLE_TIMEOUT_SECONDS
        or not inner_cycle_seconds + _OUTER_OVERHEAD_SECONDS <= timeout_seconds
        or timeout_seconds > _MAX_CYCLE_TIMEOUT_SECONDS
    ):
        raise DiscoveryError("autonomous_runner_invalid")

    def run(cycle_id: str, date: str) -> dict[str, object]:
        environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "WAAGD_AUTONOMOUS_CYCLE_ID": cycle_id,
            "WAAGD_AUTONOMOUS_LOCAL_DATE": date,
        }
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=timeout_seconds,
                check=False,
            )
        except OSError:
            return {"status": "retryable", "code": "cycle_runner_unavailable"}
        except subprocess.TimeoutExpired:
            return {"status": "operator_required", "code": "cycle_runner_timeout"}
        if completed.returncode != 0 or len(completed.stdout) > _MAX_OUTPUT:
            return {"status": "operator_required", "code": "cycle_runner_outcome_unknown"}
        try:
            value = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"status": "operator_required", "code": "cycle_runner_invalid"}
        return value if isinstance(value, dict) else {"status": "operator_required"}

    return run
