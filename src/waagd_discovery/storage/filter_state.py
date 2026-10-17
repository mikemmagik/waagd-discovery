"""Streaming filter inputs and append-only audit persistence.

Decisions use a deferred batch foreign key: they stream directly to SQLite,
then their completed summary is inserted before the atomic transaction commits.
No partially assessed batch is ever visible, and memory is independent of the
number or description size of assessed articles.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class SourceState:
    id: str
    name: str
    url: str
    enabled: bool

    def public(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "url": self.url}


@dataclass(frozen=True)
class FilterRecord:
    candidate: dict[str, object]
    sources: tuple[SourceState, ...]
    persisted_status: str | None
    persisted_fingerprint: str | None


@contextmanager
def transaction(connection: sqlite3.Connection, *, write: bool) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def input_records(
    connection: sqlite3.Connection, batch_id: str | None = None
) -> Iterator[FilterRecord]:
    # One joined cursor prevents an extra source query per article. Stream rows
    # instead of materializing descriptions once for each provenance source.
    rows = connection.execute(
        """SELECT a.id, a.title, a.url, a.publisher, a.published_at, a.description,
        a.date_status, a.first_seen_at, a.last_seen_at,
        s.id AS source_id, s.name AS source_name, s.url AS source_url,
        s.enabled AS source_enabled, d.status AS persisted_status,
        d.input_fingerprint AS persisted_fingerprint
        FROM articles a
        LEFT JOIN article_sources p ON p.article_id = a.id
        LEFT JOIN sources s ON s.id = p.source_id
        LEFT JOIN filter_decisions d ON d.article_id = a.id AND d.batch_id = ?
        ORDER BY a.id ASC, s.id ASC""",
        (batch_id,),
    )
    current_id: int | None = None
    current: dict[str, object] = {}
    sources: list[SourceState] = []
    status = fingerprint = None
    for row in rows:
        if current_id != row["id"]:
            if current_id is not None:
                current["sources"] = [source.public() for source in sources]
                yield FilterRecord(current, tuple(sources), status, fingerprint)
            current_id = row["id"]
            current = {
                key: row[key]
                for key in (
                    "id",
                    "title",
                    "url",
                    "publisher",
                    "published_at",
                    "description",
                    "date_status",
                    "first_seen_at",
                    "last_seen_at",
                )
            }
            sources = []
            status, fingerprint = row["persisted_status"], row["persisted_fingerprint"]
        if row["source_id"] is not None:
            sources.append(
                SourceState(
                    row["source_id"],
                    row["source_name"],
                    row["source_url"],
                    bool(row["source_enabled"]),
                )
            )
    if current_id is not None:
        current["sources"] = [source.public() for source in sources]
        yield FilterRecord(current, tuple(sources), status, fingerprint)


def latest_batch(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        connection.execute(
            "SELECT * FROM filter_batches ORDER BY sequence DESC LIMIT 1"
        ).fetchone(),
    )


def batch_summary(row: sqlite3.Row) -> dict[str, object]:
    return {
        "run_id": row["id"],
        "reference_time": row["reference_time"],
        "assessed_at": row["assessed_at"],
        "policy_hash": row["policy_hash"],
        "policy": json.loads(row["policy_json"]),
        "engine_version": row["engine_version"],
        "articles_evaluated": row["articles_evaluated"],
        "counts": {
            "eligible": row["eligible_count"],
            "deferred": row["deferred_count"],
            "excluded": row["excluded_count"],
        },
    }


def insert_decision(
    connection: sqlite3.Connection,
    batch_id: str,
    article_id: int,
    fingerprint: str,
    status: str,
    reasons: list[str],
    checks: dict[str, bool],
    completeness: int,
) -> None:
    connection.execute(
        """INSERT INTO filter_decisions
        (batch_id, article_id, input_fingerprint, status, reasons_json, checks_json,
         metadata_completeness) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            article_id,
            fingerprint,
            status,
            json.dumps(reasons, separators=(",", ":")),
            json.dumps(checks, separators=(",", ":")),
            completeness,
        ),
    )


def insert_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    policy_hash: str,
    policy_json: str,
    engine_version: str,
    reference_time: str,
    assessed_at: str,
    counts: dict[str, int],
) -> None:
    connection.execute(
        """INSERT INTO filter_batches
        (id, policy_hash, policy_json, engine_version, reference_time, assessed_at,
         articles_evaluated, eligible_count, deferred_count, excluded_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            policy_hash,
            policy_json,
            engine_version,
            reference_time,
            assessed_at,
            sum(counts.values()),
            counts["eligible"],
            counts["deferred"],
            counts["excluded"],
        ),
    )
