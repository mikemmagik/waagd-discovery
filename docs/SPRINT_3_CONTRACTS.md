# Sprint 3 contracts — Automated Discovery

Authorized 2026-10-18. v0.2.0/4385977 is the accepted checkpoint. Development and
isolated tests only: no permanent operator scheduler activation or integrations.

## Ownership and architecture

Lead owns CLI, shared collection orchestration, schema 4, recovery, diagnostics,
filter aggregates, version/release and real-feed evaluation. Scheduler specialist
owns scheduler.py, config/scheduler.yaml and its unit tests. Infrastructure owns
Compose/Docker/CI, isolated real-container restart tests and operator docs.
Independent QA reviews the integrated work in a separate worktree before release.

One existing pipeline remains authoritative. New collection.run_once(db: Path,
sources_path: Path, policy_path: Path, *, selected: str | None = None,
dry_run: bool = False) -> dict[str, object] loads configuration, holds the existing
database_lock for recovery + collect + assessment, and reuses pipeline.collect.
Dry-run changes memory only. Manual CLI and scheduler both call this function.
An exclusive lock is required before any orphan running-run is reconciled;
wall age/PID is never sufficient evidence. Recovery marks running rows interrupted,
records detection/reason, preserves committed per-source rows, and never restores
or rolls back prior data. The kernel releases advisory locks on exit/SIGKILL.

Schema 4 adds service_state(id INTEGER PRIMARY KEY CHECK(id=1), state_json TEXT
NOT NULL) and run_interruptions(run_id TEXT PRIMARY KEY REFERENCES runs(id),
detected_at TEXT NOT NULL, reason TEXT NOT NULL). Both tables added without
rewriting legacy rows. Scheduler may upsert the singleton JSON state; interruption
audit rows are immutable. Existing v1-v3 backups migrate in memory when inspected.

## Scheduler contract

scheduler.py exports SchedulerPolicy(interval_seconds=1800,
heartbeat_seconds=15), load_scheduler_policy(path: Path),
run_scheduler(db: Path, sources_path: Path, policy_path: Path,
scheduler_policy: SchedulerPolicy) -> dict[str, object]. CLI `scheduler` loads the
policy via global --scheduler-config / WAAGD_SCHEDULER_CONFIG. No enabled flag:
only explicitly invoking scheduler/profile starts it. Production interval minimum
1 second, maximum 86400; heartbeat 1..60 and <= interval. Tests inject clocks and
wait/cycle dependencies through an explicit testable loop/interface.

Lifetime singleton file: DB path + '.scheduler.lock', independent from per-cycle
DB path + '.lock'. Scheduler must acquire lifetime lock nonblocking; another
scheduler fails safely. Each cycle takes the ordinary database lock through
run_once. Manual collect/filter/restore retain their lock semantics and can run
between scheduler cycles. Busy cycle skips safely and waits normal interval.

First cycle is immediate. Fixed delay after completion, no backlog replay.
Waiting checks both monotonic elapsed time and UTC deadline: backwards wall jumps
do not starve execution, forward jumps/wake cause one fresh cycle then rebase.
Never retry immediately forever after failures. SIGTERM/SIGINT exit gracefully;
interrupted collect is recorded, no new cycle starts. No external timer/broker.

State JSON fields: session_id, status ('starting','waiting','collecting','stopped'),
started_at, heartbeat_at, last_cycle_at, last_cycle_finished_at,
last_success_at (full success), next_run_at, consecutive_failures, last_result,
last_error. UTC ISO timestamps; nullable fields explicit. Preserve last_success_at
across restarts. last_result is existing run summary with optional filter summary.
State updates use short database_lock + Repository transactions, skip safely on
busy instead of blocking a live manual collector. Logs contain safe scalar fields.

## Diagnostics and deployment

CLI `status` returns process liveness (kernel singleton lock), persisted state,
latest/recent runs, active/orphan distinction via operation lock, source errors /
consecutive failures and filtering aggregates/reasons. `health` exits 0 for a live
scheduler with a readable application database; feed HTTP errors alone do not
make it unhealthy. Stopped/no scheduler exits nonzero. Diagnostics never recover
or mutate working data/schema. Unknown newer schemas fail closed.

Compose scheduler profile reuses image and same persistent DB volume, no public
ports/socket, nonroot/read-only/cap-drop, 1 CPU/512MiB and bounded logs,
restart unless-stopped, healthcheck `waagd-discovery health`, 30s graceful stop.
Default project startup does not activate scheduler. All development containers
use uniquely named disposable volumes and are removed before handoff.

Backup stays explicit: daily verified managed backup, default 7 newest plus 30-day
window, manual/pre-restore never rotated, weekly off-device copy; no per-cycle
backup and no automatic restore. Document storage growth and operator disk checks.

Acceptance: existing + new offline tests, controlled clocks and HTTP fixtures,
independent QA, actual Docker restart/recreate/SIGTERM/SIGKILL/health/no-overlap,
ARM64 + AMD64 builds/smokes, constrained resource measurement, real-feed policy
inspection on isolated data, schema-3 compatibility and exact-commit green CI.
Release v0.3.0 only after all gates; Sprint 4 must remain stopped.
