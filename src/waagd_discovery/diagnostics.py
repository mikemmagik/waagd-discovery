"""Read-only service, collection and filtering diagnostics; no recovery on reads."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from waagd_discovery.locking import lock_is_held
from waagd_discovery.models import DiscoveryError
from waagd_discovery.storage.migrations import validate_schema


def filtering_statistics(connection: sqlite3.Connection) -> dict[str, object]:
    batch = connection.execute(
        "SELECT * FROM filter_batches ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    if batch is None:
        return {
            "assessment": None,
            "counts": {"eligible": 0, "deferred": 0, "excluded": 0},
            "reasons": {"deferred": {}, "excluded": {}},
        }
    reasons: dict[str, dict[str, int]] = {"deferred": {}, "excluded": {}}
    for row in connection.execute(
        "SELECT status, reasons_json FROM filter_decisions WHERE batch_id = ? "
        "AND status IN ('deferred', 'excluded')",
        (batch["id"],),
    ):
        for reason in json.loads(row["reasons_json"]):
            reasons[row["status"]][reason] = reasons[row["status"]].get(reason, 0) + 1
    return {
        "assessment": {
            "id": batch["id"],
            "assessed_at": batch["assessed_at"],
            "reference_time": batch["reference_time"],
            "policy_hash": batch["policy_hash"],
        },
        "counts": {key: batch[key + "_count"] for key in ("eligible", "deferred", "excluded")},
        "reasons": {
            status: dict(sorted(counts.items(), key=lambda p: (-p[1], p[0])))
            for status, counts in reasons.items()
        },
        "scope": "latest_persisted_assessment_not_current_export_eligibility",
    }


def service_status(db: Path) -> dict[str, object]:
    db = db.resolve()
    scheduler_alive = lock_is_held(Path(str(db) + ".scheduler.lock"))
    operation_active = lock_is_held(Path(str(db) + ".lock"))
    if not db.is_file():
        raise DiscoveryError("database_missing")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("BEGIN")
        validate_schema(connection)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        state = None
        if version >= 4:
            row = connection.execute("SELECT state_json FROM service_state WHERE id=1").fetchone()
            if row:
                state = json.loads(row["state_json"])
                if not isinstance(state, dict):
                    raise DiscoveryError("scheduler_state_invalid")
        rows = connection.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 10").fetchall()
        latest = dict(rows[0]) if rows else None
        success = connection.execute(
            "SELECT * FROM runs WHERE status='success' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        running = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM runs WHERE status='running' ORDER BY rowid DESC"
            )
        ]
        latest_outcomes = (
            []
            if latest is None
            else [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM source_runs WHERE run_id=? ORDER BY source_id", (latest["id"],)
                )
            ]
        )
        issues = []
        for source in connection.execute("SELECT id, enabled FROM sources ORDER BY id"):
            # Bounded inspection; cap is explicit rather than an unbounded history scan.
            outcomes = connection.execute(
                "SELECT s.status, s.error_code, s.finished_at FROM source_runs s "
                "JOIN runs r ON r.id=s.run_id WHERE s.source_id=? ORDER BY r.rowid DESC LIMIT 100",
                (source["id"],),
            ).fetchall()
            streak = 0
            for outcome in outcomes:
                if outcome["status"] != "failed":
                    break
                streak += 1
            if streak:
                issues.append(
                    {
                        "source_id": source["id"],
                        "enabled": bool(source["enabled"]),
                        "consecutive_failures": streak,
                        "count_capped": streak == 100,
                        "last_error": outcomes[0]["error_code"],
                        "last_failure_at": outcomes[0]["finished_at"],
                        "persistent_problem": streak >= 3,
                    }
                )
        recovered = (
            []
            if version < 4
            else [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM run_interruptions ORDER BY rowid DESC LIMIT 10"
                )
            ]
        )
        filtering = filtering_statistics(connection) if version >= 3 else None
        result: dict[str, object] = {
            "observed_at": datetime.now(UTC).isoformat(),
            "schema_version": version,
            "scheduler_alive": scheduler_alive,
            "scheduler": state,
            "healthy": scheduler_alive,
            "operation_active": operation_active,
            "operation_state": "active" if operation_active else "orphaned" if running else "idle",
            "running_runs": running,
            "latest_run": latest,
            "last_successful_run": dict(success) if success else None,
            "recent_runs": [dict(row) for row in rows],
            "latest_sources": latest_outcomes,
            "latest_counts": {
                key: sum(row[key] for row in latest_outcomes)
                for key in (
                    "items_received",
                    "items_added",
                    "items_existing",
                    "items_failed",
                    "items_skipped",
                )
            },
            "source_problems": issues,
            "recent_recoveries": recovered,
            "filtering": filtering,
        }
    return result
