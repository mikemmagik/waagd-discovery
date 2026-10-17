# Opt-in automated discovery

Sprint 3 adds a resident scheduler around the same collection, storage and filtering
pipeline as `collect --once`. Starting it requires an explicit `scheduler` command
or Compose scheduler profile. Default Compose startup remains the one-shot `stats`
command. Development acceptance uses disposable volumes only; starting the operator's
persistent scheduler requires separate permission.

`config/scheduler.yaml` supplies `interval_seconds` (default 1800, range 1–86400)
and `heartbeat_seconds` (default 15, range 1–60 and no greater than the interval).
Use global `--scheduler-config PATH` or `WAAGD_SCHEDULER_CONFIG` to select another
trusted file. Restart the scheduler after changing its timing policy. Source and
filter configuration is loaded by the shared collection path for each cycle.

The first cycle starts immediately. After completion, the scheduler waits the
configured interval; it does not replay missed work after downtime. Both UTC and
monotonic time are checked so clock changes or sleep/resume cause at most one fresh
cycle before the next delay. Feed failures wait for the ordinary interval. A busy
manual operation skips a cycle safely instead of overlapping or retrying in a loop.

Two locks have different lifetimes: `<database>.scheduler.lock` prevents a second
scheduler session, while `<database>.lock` serializes each collection/filter/restore
operation. Both use local kernel advisory locks. Lock files can remain after exit;
file presence is not evidence that a process is alive. Never delete a lock file to
work around `database_busy`; inspect `status` and allow or stop the owning process.
Use local Docker volumes, not network filesystems.

SIGTERM and SIGINT stop gracefully and prevent another cycle. A committed source
transaction survives interruption. SIGKILL can leave a `running` run; the next
collection acquires the ordinary exclusive lock, marks that orphan interrupted,
and records an immutable detection/reason audit. Recovery does not restore a
backup, rewind committed data or decide liveness from the wall clock/PID alone.

`status` combines live kernel lock probes, persisted scheduler state, recent runs,
source failures and filter aggregates/reasons. A persisted heartbeat can outlive a
process; use the live lock result. `health` returns zero only for a live scheduler
and readable application database. Feed HTTP failures alone do not make the process
unhealthy: inspect failure counters and the last successful cycle separately. These
read-only commands do not reconcile or migrate the working database.

Compose retains nonroot UID 10001, read-only root, dropped capabilities,
no-new-privileges, 32 MiB tmpfs, 512 MiB memory, one CPU, 64 processes and bounded
3 × 10 MiB logs. It publishes no ports and mounts no Docker socket. The scheduler
uses `restart: unless-stopped`, a 30-second graceful stop and a CLI healthcheck.
Docker restarts on process exit; an unhealthy healthcheck by itself does not
restart the container. After an explicit stop, it stays stopped until started again.

See [RUNBOOK.md](RUNBOOK.md) for START/STOP/RESTART/STATUS/LOGS/HEALTH, updates,
rollback and backups. Backups remain explicit operator work, separate from cycles.

## Isolated lifecycle acceptance

```sh
python3 scripts/docker-scheduler-smoke.py
```

The script creates a unique named volume and containers, uses `--network none`,
and removes only those resources in its cleanup. A bind-mounted test-only wrapper
serves RSS fixtures on container loopback and substitutes only the HTTP client at
the shared collection boundary. It is outside the image/build context. Production
URL/DNS/SSRF checks and the CLI have no fixture bypass or hidden test flag.

The test exercises repeated cycles and deduplication, the lifetime singleton,
a manual operation lock conflict, active-collection SIGTERM, Docker restart,
explicit Docker SIGKILL/restart, process SIGKILL with Engine automatic restart,
immutable interruption audit, recreation
with persistent SQLite, CLI/Docker health, integrity and resource/hardening bounds.
It prints measured elapsed time and cgroup memory peak; these small lifecycle
fixtures are not a throughput benchmark or long-running reliability proof.

Set `WAAGD_SMOKE_IMAGE` and `WAAGD_SMOKE_SKIP_BUILD=1` to test an existing image.
CI runs this and the earlier persistence/export/backup smoke for both amd64 and
arm64. Public-feed evaluation remains a separate explicit operation.
