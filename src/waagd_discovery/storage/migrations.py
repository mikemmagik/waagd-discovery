"""Ordered additive schema migrations; pending versions commit atomically."""

import sqlite3
from functools import lru_cache

from waagd_discovery.models import DiscoveryError

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE sources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            type TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            etag TEXT,
            last_modified TEXT
        )""",
        """CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            description TEXT,
            publisher TEXT,
            published_at TEXT,
            date_status TEXT NOT NULL CHECK (date_status IN ('valid', 'missing', 'invalid')),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )""",
        """CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('running', 'success', 'partial', 'failed', 'interrupted')
            )
        )""",
    ),
    (
        """CREATE TABLE url_aliases (
            url TEXT PRIMARY KEY,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            first_seen_at TEXT NOT NULL
        )""",
        """INSERT INTO url_aliases (url, article_id, first_seen_at)
            SELECT url, id, first_seen_at FROM articles""",
        """CREATE TABLE source_guids (
            source_id TEXT NOT NULL REFERENCES sources(id),
            guid TEXT NOT NULL,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            PRIMARY KEY (source_id, guid)
        )""",
        """CREATE TABLE article_sources (
            article_id INTEGER NOT NULL REFERENCES articles(id),
            source_id TEXT NOT NULL REFERENCES sources(id),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (article_id, source_id)
        )""",
        """CREATE TABLE source_runs (
            run_id TEXT NOT NULL REFERENCES runs(id),
            source_id TEXT NOT NULL REFERENCES sources(id),
            status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
            items_received INTEGER NOT NULL CHECK (items_received >= 0),
            items_added INTEGER NOT NULL CHECK (items_added >= 0),
            items_existing INTEGER NOT NULL CHECK (items_existing >= 0),
            items_failed INTEGER NOT NULL CHECK (items_failed >= 0),
            items_skipped INTEGER NOT NULL CHECK (items_skipped >= 0),
            error_code TEXT,
            finished_at TEXT NOT NULL,
            PRIMARY KEY (run_id, source_id)
        )""",
        """CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            source_id TEXT NOT NULL REFERENCES sources(id),
            run_id TEXT NOT NULL REFERENCES runs(id),
            observed_at TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            original_url TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            external_guid TEXT,
            title TEXT,
            description TEXT,
            publisher TEXT,
            published_at TEXT,
            date_status TEXT NOT NULL CHECK (date_status IN ('valid', 'missing', 'invalid'))
        )""",
        "CREATE INDEX articles_recent ON articles (first_seen_at DESC, id ASC)",
        "CREATE INDEX observations_article ON observations (article_id, id)",
        "CREATE INDEX observations_source ON observations (source_id, id)",
        """CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations
            BEGIN SELECT RAISE(ABORT, 'observations_are_immutable'); END""",
        """CREATE TRIGGER observations_no_delete BEFORE DELETE ON observations
            BEGIN SELECT RAISE(ABORT, 'observations_are_immutable'); END""",
        """CREATE TRIGGER article_identity_no_update
            BEFORE UPDATE OF url, first_seen_at ON articles
            WHEN NEW.url != OLD.url OR NEW.first_seen_at != OLD.first_seen_at
            BEGIN SELECT RAISE(ABORT, 'article_identity_is_immutable'); END""",
    ),
    (
        """CREATE TABLE filter_batches (
            sequence INTEGER PRIMARY KEY,
            id TEXT NOT NULL UNIQUE,
            policy_hash TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            reference_time TEXT NOT NULL,
            assessed_at TEXT NOT NULL,
            articles_evaluated INTEGER NOT NULL CHECK (articles_evaluated >= 0),
            eligible_count INTEGER NOT NULL CHECK (eligible_count >= 0),
            deferred_count INTEGER NOT NULL CHECK (deferred_count >= 0),
            excluded_count INTEGER NOT NULL CHECK (excluded_count >= 0),
            CHECK (articles_evaluated = eligible_count + deferred_count + excluded_count)
        )""",
        """CREATE TABLE filter_decisions (
            batch_id TEXT NOT NULL REFERENCES filter_batches(id) DEFERRABLE INITIALLY DEFERRED,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            input_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('eligible', 'deferred', 'excluded')),
            reasons_json TEXT NOT NULL,
            checks_json TEXT NOT NULL,
            metadata_completeness INTEGER NOT NULL CHECK (metadata_completeness >= 0),
            PRIMARY KEY (batch_id, article_id)
        )""",
        """CREATE INDEX filter_decisions_status
            ON filter_decisions (batch_id, status, article_id)""",
        """CREATE TRIGGER filter_batches_no_update BEFORE UPDATE ON filter_batches
            BEGIN SELECT RAISE(ABORT, 'filter_audit_is_immutable'); END""",
        """CREATE TRIGGER filter_batches_no_delete BEFORE DELETE ON filter_batches
            BEGIN SELECT RAISE(ABORT, 'filter_audit_is_immutable'); END""",
        """CREATE TRIGGER filter_decisions_no_update BEFORE UPDATE ON filter_decisions
            BEGIN SELECT RAISE(ABORT, 'filter_audit_is_immutable'); END""",
        """CREATE TRIGGER filter_decisions_no_delete BEFORE DELETE ON filter_decisions
            BEGIN SELECT RAISE(ABORT, 'filter_audit_is_immutable'); END""",
    ),
    (
        """CREATE TABLE service_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state_json TEXT NOT NULL
        )""",
        """CREATE TABLE run_interruptions (
            run_id TEXT PRIMARY KEY REFERENCES runs(id),
            detected_at TEXT NOT NULL,
            reason TEXT NOT NULL
        )""",
        """CREATE TRIGGER run_interruptions_no_update BEFORE UPDATE ON run_interruptions
            BEGIN SELECT RAISE(ABORT, 'recovery_audit_is_immutable'); END""",
        """CREATE TRIGGER run_interruptions_no_delete BEFORE DELETE ON run_interruptions
            BEGIN SELECT RAISE(ABORT, 'recovery_audit_is_immutable'); END""",
    ),
)

SCHEMA_VERSION = len(MIGRATIONS)


def _schema(connection: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
    ).fetchall()
    return {(row[0], row[1], row[2]): " ".join((row[3] or "").split()) for row in rows}


@lru_cache(maxsize=SCHEMA_VERSION)
def _expected_schema(version: int) -> dict[tuple[str, str, str], str]:
    # Derive the schema from the actual ordered migrations rather than maintain
    # a weaker duplicate list of table names. This also covers constraints,
    # immutable-identity triggers, indexes, and unexpected extra objects.
    reference = sqlite3.connect(":memory:")
    try:
        for statements in MIGRATIONS[:version]:
            for statement in statements:
                reference.execute(statement)
        return _schema(reference)
    finally:
        reference.close()


def validate_schema(connection: sqlite3.Connection, version: int | None = None) -> None:
    if version is None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise DiscoveryError("database_schema_newer")
    if version < 1 or _schema(connection) != _expected_schema(version):
        raise DiscoveryError("database_schema_invalid")


def migrate(connection: sqlite3.Connection) -> None:
    # Read the version under the same write reservation used for DDL. Two
    # openers cannot both conclude that the same migration is still pending.
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise DiscoveryError("database_schema_newer")
        if version < 0:
            raise DiscoveryError("database_schema_invalid")
        if version == 0:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            ).fetchone()
            if existing is not None:
                raise DiscoveryError("database_schema_unversioned")
        else:
            validate_schema(connection, version)
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[target - 1]:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {target}")
        validate_schema(connection, SCHEMA_VERSION)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
