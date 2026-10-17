"""Reconcile orphan runs only while the caller owns the exclusive operation lock."""

from datetime import UTC, datetime

from waagd_discovery.logging import event
from waagd_discovery.storage import Repository


def recover_runs(repository: Repository) -> list[str]:
    """Caller MUST hold database_lock for this database (or operate on a dry-run copy).

    Acquiring that lock proves no cooperating collector is active. PID reuse and
    wall-clock ages are deliberately irrelevant. Keep committed source outcomes.
    """
    recovered: list[str] = []
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    with repository._transaction() as connection:
        for row in connection.execute("SELECT id FROM runs WHERE status = 'running'").fetchall():
            connection.execute(
                "INSERT INTO run_interruptions (run_id, detected_at, reason) VALUES (?, ?, ?)",
                (row["id"], now, "operation_lock_reacquired"),
            )
            connection.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            recovered.append(row["id"])
    for run_id in recovered:
        event("discovery_recovered", run_id=run_id, reason="operation_lock_reacquired")
    return recovered
