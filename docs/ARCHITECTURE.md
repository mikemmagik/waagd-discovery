# Architecture — v0.4 Data-only Integration

One synchronous Python modular monolith runs as a one-shot or opt-in scheduled non-root container.
Trusted YAML registry → bounded HTTP → safe RSS/Atom parser → URL normalization
and deterministic identity → SQLite transactions. No LLM, article fetching,
editorial decision, publication or connection to existing WAAGD systems.

SQLite lives on a named volume, uses WAL, foreign keys and busy_timeout. A run
records per-source outcomes. Each successful source ingestion is atomic. A failed
source cannot roll back previously ingested sources. URL identity is global;
GUID identity is scoped to a source. Observations retain original metadata and
URL aliases so updates and cross-source discoveries remain traceable.

The CLI orchestrates the components and emits JSON on stdout; JSON logs go to
stderr. Dry-run uses an in-memory database populated from a read-only snapshot,
so the actual database, including schema and source registry, is never changed.

Optional bounded JSON export is a data-only boundary for future integration.
Backup uses SQLite's online backup API; restoration requires explicit operator
action and a pre-restore backup. These reliability essentials were implemented
in Sprint 1; live editorial/LLM integration remains deferred.

An advisory lock serializes mutating operations. OS release on process exit
avoids stale lock files blocking recovery. This assumes a local Docker volume,
not NFS. SIGTERM cancels between sources and closes transactions/connections.

Sprint 2 adds one deterministic filter module and additive schema v3 audit tables.
Latest complete immutable assessment batches define eligibility, separate from
all original discovery metadata and future editorial states. A hash of policy,
engine and relevant input prevents outdated eligibility from leaking into selected
export. Export reads a consistent memory snapshot, checks the operator's current
registry and re-evaluates age at one UTC reference time before ranking/limiting.

Managed backups reuse the online backup engine and introduce a dedicated-directory
lock plus conservative retention. Only matching managed files are candidates;
manual/pre-restore/corrupt/unknown files are preserved. The newest verified count
and age-window union is kept, with at least one verified survivor before deletion.
New managed backups are verified and fsynced before any previous file is removed.
Rotation is an explicit operator operation; no timer has been introduced.

Sprint 3 adds schema 4: a singleton scheduler state and immutable interruption
recovery audit. Both execution modes call collection.run_once, which owns the
ordinary database lock across recovery, the original pipeline and filtering.
Only an acquired operation lock proves prior running records are orphaned;
recovery marks them interrupted without discarding committed source outcomes.
A separate canonical-path scheduler lifetime flock prevents multiple daemon
instances. Manual writes remain possible while the scheduler waits.

The scheduler runs immediately at startup and then uses a configurable fixed
delay after each cycle. Monotonic and UTC deadlines prevent backwards clock jumps
from starving work and allow one current collection after downtime/forward jumps.
There is no backlog queue or immediate retry loop. SIGTERM/SIGINT interrupts the
existing pipeline, records interruption and stops without another cycle. SIGKILL
releases kernel locks; the next collector reconciles orphaned runs transactionally.

Service diagnostics read the actual schema without migrating it. Kernel locks
report process/operation liveness; source failure streaks report upstream problems
separately. Persisted heartbeats update while waiting, not during synchronous HTTP;
they are not a promise of asynchronous progress monitoring or external watchdog
restarts. Health verifies scheduler ownership and database readability. The latest
filter audit's counts/reasons are explicitly distinguished from freshness-gated
export eligibility. Default Compose keeps scheduling off. No operator deployment
or integration with WAAGD/OpenClaw is included in this release.

Sprint 4 keeps SQLite schema 4 and the existing pipeline. Completed successful/
partial assessments can opt into automatic schema-v2 publication; failed/dry-run/
interrupted collections do not publish. Export failures retain ingestion and are
reported separately in result/logs and a private .handoff.json diagnostic sidecar.
No handoff is enabled by default during operator rollout.

The host-side waagd-bridge entrypoint is offline and imports no database/Docker/
HTTP client. It validates the bounded export and independently rechecks actual
freshness, projects text limits and publishes one fixed workspace inbox. A durable
private JSON ledger outside workspace/export roots records canonical-URL hashes,
prepared/delivered/processed/dismissed states and exact pending payload. Prepare
precedes atomic publication; restart reuses its ID, ack/dismiss precedes clearing.
One pending batch provides backpressure. Expiry blocks new work until explicit
resolution; no LLM or editor execution is performed. The bridge only exposes data;
it cannot revoke existing agent privileges or guarantee semantic injection removal.
Operator installation and actual consumer behavior on the separate Intel Mac are
explicitly outside development access. No agent, gateway or production change.
