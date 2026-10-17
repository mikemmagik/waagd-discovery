# Sprint 3 — Automated Discovery / v0.3.0

Prepared 2026-10-18, Asia/Bangkok. Sprint 2/v0.2.0 commit
43859777f686938a0f486f60f321f9d368fb17e6 is the accepted checkpoint.
Repository: https://github.com/mikemmagik/waagd-discovery (private), main.
The annotated v0.3.0 tag resolves the release commit. A full SHA and exact-commit
CI URL are attached to the GitHub Release and operator report after all four jobs
succeed. No tag or release is created before this gate passes.

## Delivered behavior

- One modular Python application, one existing collection pipeline. Both manual
  `collect --once` and the explicitly invoked scheduler use collection.run_once:
  trusted configuration, exclusive database lock, orphan reconciliation, existing
  pipeline, and the same deterministic filtering. No duplicate collector exists.
- Configurable fixed delay: 1800 seconds by default, first cycle immediate,
  interval 1..86400 seconds, heartbeat 1..60 and <= interval. Timing configuration
  loads at startup; sources and filtering reload each cycle. Every failure/busy
  outcome waits the ordinary interval. Long cycles never overlap or create backlog.
- Separate canonical-path lifetime scheduler flock and per-operation database
  flock. Manual writes are allowed between scheduled cycles; concurrent/conflicting
  operations fail safely. File existence, PID and wall age do not establish liveness.
- UTC plus monotonic deadlines prevent backwards-clock starvation. Forward jumps,
  wake and process restart cause one current collection, followed by a fresh delay.
  Deterministic tests cover 30-day jumps; no long real-time sleep is needed in CI.
- SIGTERM/SIGINT interrupts the current shared pipeline, preserves completed source
  transactions, records interruption and stops without scheduling another cycle.
  SIGKILL releases locks; the next collector marks orphan running rows interrupted.
- Additive schema 4 adds singleton scheduler state and immutable interruption audit.
  Recovery is performed only after operation-lock acquisition and preserves prior
  articles, observations, GUIDs, aliases, provenance and completed source outcomes.
  It never automatically restores or rolls back the working database.
- Read-only `status` reports kernel liveness, operation active/orphan/idle state,
  persisted last/last-success/next cycle timestamps, latest source/count results,
  recent recoveries, failure streaks (bounded at 100) and persistent source problems.
  `health` succeeds for a live scheduler with readable recognized SQLite schema.
  HTTP 429 or another unavailable feed alone does not make the container unhealthy.
- `filter-stats` and status show eligible/deferred/excluded plus reason counts.
  Counts describe the latest persisted audit, explicitly not current export
  eligibility. Existing JSON schema v2 still checks current source/policy/input,
  age and persisted decisions; its contract and all original materials remain.
- Separate opt-in Compose profile reuses the same runtime image and data volume:
  unless-stopped, nonroot 10001, read-only root, cap-drop ALL/no-new-privileges,
  no ports or Docker socket, 1 CPU/512 MiB, 32 MiB tmpfs, 64 PIDs, healthcheck,
  30-second grace period and bounded logs. Default Compose services list excludes
  scheduler. Permanent operator scheduling has not been activated.
- Backup stays explicit daily operator work. Existing verified managed backup and
  retention are reused; no per-cycle backup, automatic restore or new dependency.
  Keep seven newest verified copies plus all within 30 days; manual/pre-restore
  copies are untouched and a sole verified backup is never deleted. Copy off-device
  weekly and after significant changes; monitor disk usage and test restoration.

## Independent QA and regressions

Specialists implemented scheduler and Docker/operations in separate worktrees.
Independent QA reviewed feature commits in a separate integration worktree before
integration and added 27 adversarial cases. Final result: no unresolved blocker.
Full evidence: SPRINT_3_QA_REVIEW.md.

Two important review findings were fixed and independently reproduced/retested:

1. The existing pipeline absorbs KeyboardInterrupt and returns interrupted. The
   scheduler must stop on that returned summary, as well as on escaping interrupts.
2. Export through a database symlink could replace canonical lock/WAL/SHM files
   after canonical locks were introduced. Both alias and canonical sidecars are now
   protected. Real held-lock regressions verify unchanged inodes/exclusion.

The lifecycle fixture's original fixed future publication date was also replaced
with current UTC and explicit eligible=2 assertions. This avoids a passing lifecycle
whose articles all fail freshness after calendar changes. Explicit Docker kill and
process crash are tested separately because an operator kill can suppress Engine
restart; only a process crash is required to exercise unless-stopped autorestart.

Final local verification: **543 tests PASS, no warnings**, **95.69% coverage**
(1,819/1,901 statements), Ruff without cache, format checks for 55 Python files,
strict mypy for 22 application modules, frozen lock and tracked-source safety PASS.
All 472 prior tests remain; only their HTTP-client injection module moved from CLI
to collection with no relaxed assertions. No dependency versions changed.

Offline tests cover multiple cycles/no-new-material, slow/failed/busy delays,
no overlap, partial feeds/429, controlled clocks/downtime, startup/restart success
state, actual child SIGTERM/SIGKILL, immutable recovery audit, active/orphan locks,
read-only legacy/future-schema health, deduplication and provenance preservation.

Both linux/arm64 and linux/amd64 images build and pass the previous one-shot
persistence/export/backup/restore smoke plus actual scheduler lifecycle acceptance.
Lifecycle evidence and exact durations: SPRINT_3_DOCKER.json. It performs actual
Docker stop/start/restart/kill, process SIGKILL/Engine restart, removal/recreation
with the same volume, singleton/manual exclusion, CLI/Docker health and integrity.
The test-only loopback HTTP wrapper is bind-mounted outside the image/build context;
production SSRF protection and CLI contain no hidden fixture bypass.

The first remote ARM64 lifecycle attempt timed out before observing two cycles,
while its image build and earlier one-shot smoke passed. CLI startup under that
QEMU runner took about seven seconds; the test's two-second health cadence and
five-second timeout were an implausible startup load/budget. This was a plausible
cause, not proven by the original incomplete failure logs. The harness now uses
30-second probes/start grace, a 20-second emulation budget and bounded 180-second
waits, with the same resource limits and every lifecycle assertion unchanged.
Failure inspect (including health/OOM/restarts), both log streams and DB state are
captured before cleanup. No production timing or application behavior changed.
A temporary once-only 65-second startup delay also passed the full lifecycle in
126.726 seconds; a forced failure independently verified diagnostics before
cleanup. The corrected exact-commit full CI remains mandatory before release. Final local
lifecycle evidence below uses the corrected harness; the earlier independent QA
run remains separately recorded as historical acceptance evidence.

## Resources and SQLite

SPRINT_3_BENCHMARK.json records a synthetic native arm64 Linux/Python 3.13.16
run: 10 sources, 10,000 unique articles, 800-character normal descriptions,
three passes / 30,000 observations, real parsing/normalization/filtering/export/
backup/restore. Docker uses one CPU, 512 MiB memory, 32 MiB /tmp, no network.

- Entire workload: **7.8867 seconds**.
- Process peak: **111,116 KiB / 108.51 MiB**.
- Cgroup peak including charged cache: **316,256,256 bytes / 301.61 MiB**.
- Exit 0, no OOM. Database 67,133,440 bytes / 64.02 MiB in WAL mode.
- WAL 5,957,552 bytes before successful checkpoint (0 busy, 1040/1040 frames).
- Indexed URL lookup, competing-writer timeout then successful acquisition,
  restore integrity/foreign keys and all expected counts pass.
- First audit: 6896 eligible / 840 deferred / 2264 excluded; all articles retained.
  Changed metadata invalidates 1000 decisions until reassessment; export selects 50.
- Lifecycle peak is measured separately in its final recreated small-fixture cgroup;
  it is not a whole-test/global host peak. Both measurements are bounded proofs,
  not an SLA, long soak, actual 24-hour uptime or hardware power-loss experiment.

## Actual v0.2 migration

An isolated volume was seeded by the accepted v0.2 image, including a completed
source/article/filter audit and a running orphan. The v0.3 image migrated schema
3 to 4. Every original row matched before/after migration (SHA256
28108f5534c0c7193f91bc8a4b80c9ffaebd8d143577fcab5c2f75e5ed4b59e7).
The next shared collection recorded one interruption, retained the article and
observation, added no duplicate article and passed integrity. The original v0.2
archive hash was unchanged. See SPRINT_3_MIGRATION.json. The operator working
volume was never migrated or used for tests.

## Real RSS filtering evaluation

A separate explicit collection at 2026-10-17T20:08:23Z (18 October Bangkok) fetched
Guardian World, Guardian Technology and Ars Technica successfully: 96 items,
95 unique articles, 75 eligible, zero deferred, 20 excluded only for age.
All articles had valid publication dates and >=40 description characters.
Per-source counts/reasons and description lengths: SPRINT_3_LIVE_FILTER.json.
Keep the existing 72-hour/40-character/known-date defaults unchanged. This is
one observed snapshot, not general proof for arbitrary feeds or future uptime.

## Operations and release boundary

Exact START/STOP/RESTART/STATUS/LOGS/HEALTH/BACKUP/UPDATE/ROLLBACK commands are in
RUNBOOK.md; behavior/configuration and isolated acceptance are in SCHEDULER.md.
START requires explicit profile/service selection and builds reviewed source.
Updating preserves the existing volume: stop first, back up using v0.2, record
commit/image, fetch/check out v0.3.0, build, init-db, verify/status. Starting the
persistent scheduler afterward remains a separate operator approval.

Rollback requires the schema-3 pre-upgrade backup and the actual older v0.2 image.
The newer restore migrates forward; the older binary refuses schema 4. Preserve
both old and current backups. Never delete/reset the working volume or use down -v.

A sleeping/powered-off Mac performs no container work; Docker Desktop must run.
After wake, one fresh cycle runs without catch-up backlog. Synchronous HTTP has
wait-phase heartbeats; health does not claim an asynchronous stall watchdog.
An unhealthy healthcheck alone does not restart Docker; process exit triggers the
configured restart policy. Local advisory locking assumes cooperating tools/local
volumes, not arbitrary direct writers or NFS. Audits/observations grow and require
operator disk monitoring; no automatic provenance pruning was introduced.
Significantly larger SQLite snapshots need separate resource measurement.

No OpenClaw/Shmul/WAAGD-agent, OpenRouter/LLM, Telegram, Cloudflare, production,
email or publication changes were made. No permanent scheduler is left running.
Sprint 4 and deployment/integration on the operator Mac require separate agreement.
