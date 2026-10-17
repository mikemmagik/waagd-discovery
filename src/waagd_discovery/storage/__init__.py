"""Transactional SQLite repository for discovery identities and provenance."""

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import uuid4

from waagd_discovery.models import ArticleCandidate, DiscoveryError, FetchResult, Source
from waagd_discovery.normalization import normalize_url
from waagd_discovery.storage.migrations import SCHEMA_VERSION, migrate, validate_schema

__all__ = ["SCHEMA_VERSION", "Repository"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _optional_text(value: str | None, maximum: int) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > maximum):
        raise ValueError("invalid_item")


def _validate_source(source: Source) -> None:
    if not isinstance(source.id, str) or not re.fullmatch(r"[a-z0-9_]+", source.id):
        raise ValueError("invalid_source")
    if not isinstance(source.name, str) or not source.name.strip() or len(source.name) > 300:
        raise ValueError("invalid_source")
    if not isinstance(source.type, str) or not source.type or len(source.type) > 32:
        raise ValueError("invalid_source")
    if not isinstance(source.enabled, bool):
        raise ValueError("invalid_source")
    normalize_url(source.url)
    _optional_text(source.etag, 4096)
    _optional_text(source.last_modified, 4096)


def _validate_item(item: ArticleCandidate) -> tuple[str, str | None]:
    if not isinstance(item, ArticleCandidate):
        raise ValueError("invalid_item")
    normalized = normalize_url(item.original_url)
    _optional_text(item.title, 1000)
    _optional_text(item.description, 4000)
    _optional_text(item.publisher, 300)
    _optional_text(item.external_guid, 4096)
    if not isinstance(item.date_status, str) or item.date_status not in {
        "valid",
        "missing",
        "invalid",
    }:
        raise ValueError("invalid_item")
    published_at = None
    if item.published_at is not None:
        if (
            not isinstance(item.published_at, datetime)
            or item.published_at.tzinfo is None
            or item.published_at.utcoffset() is None
            or item.date_status != "valid"
        ):
            raise ValueError("invalid_item")
        published_at = item.published_at.astimezone(UTC).isoformat()
    elif item.date_status == "valid":
        raise ValueError("invalid_item")
    return normalized, published_at


class Repository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        """Open connection, also usable as an online-backup snapshot target."""
        if self._connection is None:
            raise RuntimeError("repository_not_open")
        return self._connection

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise RuntimeError("repository_already_open")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        self._connection = connection
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA trusted_schema = OFF")
            # Detect unsupported schemas before changing persistent pragmas.
            migrate(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            connection.close()
            self._connection = None
            raise
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.connection.close()
        self._connection = None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def sync_sources(self, sources: list[Source]) -> None:
        identities = set()
        for source in sources:
            _validate_source(source)
            if source.id in identities:
                raise ValueError("duplicate_source_id")
            identities.add(source.id)
        with self._transaction() as connection:
            connection.execute("UPDATE sources SET enabled = 0")
            for source in sources:
                connection.execute(
                    """INSERT INTO sources (id, name, url, type, enabled, etag, last_modified)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        name = excluded.name, type = excluded.type, enabled = excluded.enabled,
                        etag = CASE WHEN sources.url = excluded.url THEN sources.etag ELSE NULL END,
                        last_modified = CASE WHEN sources.url = excluded.url
                            THEN sources.last_modified ELSE NULL END,
                        url = excluded.url""",
                    (
                        source.id,
                        source.name,
                        source.url,
                        source.type,
                        int(source.enabled),
                        source.etag,
                        source.last_modified,
                    ),
                )

    def get_source(self, source_id: str) -> Source:
        row = self.connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise DiscoveryError("source_not_found")
        return Source(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            type=row["type"],
            enabled=bool(row["enabled"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
        )

    def start_run(self) -> str:
        identity = str(uuid4())
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO runs (id, started_at, status) VALUES (?, ?, 'running')",
                (identity, _now()),
            )
        return identity

    def _require_running(self, run_id: str) -> None:
        row = self.connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None or row["status"] != "running":
            raise ValueError("run_not_running")

    def _require_source(self, source: Source) -> None:
        _validate_source(source)
        stored = self.get_source(source.id)
        if stored.url != source.url:
            raise ValueError("source_url_changed")

    def _require_unprocessed(self, run_id: str, source_id: str) -> None:
        previous = self.connection.execute(
            "SELECT 1 FROM source_runs WHERE run_id = ? AND source_id = ?", (run_id, source_id)
        ).fetchone()
        if previous is not None:
            raise ValueError("source_already_processed")

    def save_source(
        self,
        run_id: str,
        source: Source,
        items: list[ArticleCandidate],
        fetched: FetchResult,
        skipped: int = 0,
    ) -> tuple[int, int, int]:
        if fetched.status_code not in {200, 304}:
            raise ValueError("invalid_fetch_status")
        if not isinstance(skipped, int) or isinstance(skipped, bool) or skipped < 0:
            raise ValueError("invalid_skipped_count")
        _optional_text(fetched.etag, 4096)
        _optional_text(fetched.last_modified, 4096)
        added = existing = failed = 0
        observed_at = _now()
        with self._transaction() as connection:
            self._require_running(run_id)
            self._require_source(source)
            self._require_unprocessed(run_id, source.id)
            if not fetched.not_modified:
                for item in items:
                    try:
                        normalized, published_at = _validate_item(item)
                        was_added = self._save_item(
                            run_id, source, item, normalized, published_at, observed_at
                        )
                    except ValueError:
                        failed += 1
                        continue
                    if was_added:
                        added += 1
                    else:
                        existing += 1
            # A 304 may include fresh validators, but omitted ones retain their
            # prior values. A new representation replaces both validators.
            if fetched.not_modified:
                connection.execute(
                    """UPDATE sources SET etag = COALESCE(?, etag),
                    last_modified = COALESCE(?, last_modified) WHERE id = ?""",
                    (fetched.etag, fetched.last_modified, source.id),
                )
            else:
                connection.execute(
                    "UPDATE sources SET etag = ?, last_modified = ? WHERE id = ?",
                    (fetched.etag, fetched.last_modified, source.id),
                )
            self._record_outcome(
                run_id,
                source.id,
                "success",
                added,
                existing,
                failed,
                0 if fetched.not_modified else skipped,
                None,
                observed_at,
            )
        return added, existing, failed

    def _save_item(
        self,
        run_id: str,
        source: Source,
        item: ArticleCandidate,
        normalized: str,
        published_at: str | None,
        observed_at: str,
    ) -> bool:
        connection = self.connection
        url_identity = connection.execute(
            "SELECT article_id FROM url_aliases WHERE url = ?", (normalized,)
        ).fetchone()
        guid_identity = None
        if item.external_guid:
            guid_identity = connection.execute(
                "SELECT article_id FROM source_guids WHERE source_id = ? AND guid = ?",
                (source.id, item.external_guid),
            ).fetchone()
        if (
            url_identity is not None
            and guid_identity is not None
            and url_identity["article_id"] != guid_identity["article_id"]
        ):
            # Both identities already exist; guessing would destroy provenance.
            raise ValueError("identity_conflict")
        identity = url_identity if url_identity is not None else guid_identity
        was_added = identity is None
        metadata = (item.title, item.description, item.publisher, published_at, item.date_status)
        if was_added:
            cursor = connection.execute(
                """INSERT INTO articles
                (url, title, description, publisher, published_at, date_status,
                 first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (normalized, *metadata, observed_at, observed_at),
            )
            article_id = cursor.lastrowid
        else:
            assert identity is not None
            article_id = identity["article_id"]
            connection.execute(
                """UPDATE articles SET title = ?, description = ?, publisher = ?,
                published_at = ?, date_status = ?, last_seen_at = ? WHERE id = ?""",
                (*metadata, observed_at, article_id),
            )
        connection.execute(
            """INSERT INTO url_aliases (url, article_id, first_seen_at) VALUES (?, ?, ?)
            ON CONFLICT (url) DO NOTHING""",
            (normalized, article_id, observed_at),
        )
        if item.external_guid:
            connection.execute(
                """INSERT INTO source_guids (source_id, guid, article_id) VALUES (?, ?, ?)
                ON CONFLICT (source_id, guid) DO NOTHING""",
                (source.id, item.external_guid, article_id),
            )
        connection.execute(
            """INSERT INTO article_sources (article_id, source_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?) ON CONFLICT (article_id, source_id)
            DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (article_id, source.id, observed_at, observed_at),
        )
        connection.execute(
            """INSERT INTO observations
            (article_id, source_id, run_id, observed_at, source_name, source_url,
             original_url, normalized_url, external_guid, title, description,
             publisher, published_at, date_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                article_id,
                source.id,
                run_id,
                observed_at,
                source.name,
                source.url,
                item.original_url,
                normalized,
                item.external_guid,
                *metadata,
            ),
        )
        return was_added

    def _record_outcome(
        self,
        run_id: str,
        source_id: str,
        status: str,
        added: int,
        existing: int,
        failed: int,
        skipped: int,
        error_code: str | None,
        finished_at: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO source_runs
            (run_id, source_id, status, items_received, items_added, items_existing,
             items_failed, items_skipped, error_code, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                source_id,
                status,
                added + existing + failed + skipped,
                added,
                existing,
                failed,
                skipped,
                error_code,
                finished_at,
            ),
        )

    def fail_source(self, run_id: str, source: Source, code: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid_error_code")
        with self._transaction():
            self._require_running(run_id)
            self._require_source(source)
            self._require_unprocessed(run_id, source.id)
            self._record_outcome(run_id, source.id, "failed", 0, 0, 0, 0, code, _now())

    def finish_run(self, run_id: str, status: str) -> dict[str, object]:
        if status not in {"success", "partial", "failed", "interrupted"}:
            raise ValueError("invalid_run_status")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("run_not_found")
            if row["status"] != "running" and row["status"] != status:
                raise ValueError("run_already_finished")
            if row["status"] == "running":
                connection.execute(
                    "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                    (status, _now(), run_id),
                )
            summary = self._run_summary(run_id)
        return summary

    def _run_summary(self, run_id: str) -> dict[str, object]:
        run = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert run is not None
        outcomes = self.connection.execute(
            "SELECT * FROM source_runs WHERE run_id = ? ORDER BY source_id", (run_id,)
        ).fetchall()
        summary: dict[str, object] = {
            "run_id": run_id,
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "sources_checked": len(outcomes),
            "sources_failed": sum(row["status"] == "failed" for row in outcomes),
            "sources_successful": sum(row["status"] == "success" for row in outcomes),
        }
        for column in (
            "items_received",
            "items_added",
            "items_existing",
            "items_failed",
            "items_skipped",
        ):
            summary[column] = sum(row[column] for row in outcomes)
        summary["sources"] = [
            {key: row[key] for key in row.keys() if key != "run_id"} for row in outcomes
        ]
        return summary

    def stats(self) -> dict[str, object]:
        counts: dict[str, object] = {}
        for table in ("articles", "sources", "runs", "observations"):
            counts[table] = self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        counts["enabled_sources"] = self.connection.execute(
            "SELECT count(*) FROM sources WHERE enabled = 1"
        ).fetchone()[0]
        return counts

    def candidates(self, limit: int = 50) -> list[dict[str, object]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("invalid_limit")
        rows = self.connection.execute(
            """SELECT id, title, url, publisher, published_at, description, date_status,
            first_seen_at, last_seen_at FROM articles
            ORDER BY first_seen_at DESC, id ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            candidate = dict(row)
            source_rows = self.connection.execute(
                """SELECT s.id, s.name, s.url FROM sources s JOIN article_sources a
                ON a.source_id = s.id WHERE a.article_id = ? ORDER BY s.id""",
                (row["id"],),
            ).fetchall()
            candidate["sources"] = [dict(source) for source in source_rows]
            result.append(candidate)
        return result

    def backup(self, destination: Path) -> None:
        if self.connection.in_transaction:
            raise RuntimeError("database_transaction_active")
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        target: sqlite3.Connection | None = None
        try:
            target = sqlite3.connect(destination)
            self.connection.backup(target)
            _verify_connection(target)
            target.close()
            target = None
        except BaseException:
            if target is not None:
                target.close()
            destination.unlink(missing_ok=True)
            raise

    def verify(self) -> None:
        _verify_connection(self.connection)


def _verify_connection(connection: sqlite3.Connection) -> None:
    validate_schema(connection)
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        raise DiscoveryError("database_integrity_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DiscoveryError("database_foreign_key_failed")
