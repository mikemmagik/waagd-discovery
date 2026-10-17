# Sprint 2 — Reliable Discovery / v0.2.0

Prepared 2026-10-18 (Asia/Bangkok). Sprint 3 is held for architectural acceptance.
Repository: [mikemmagik/waagd-discovery](https://github.com/mikemmagik/waagd-discovery),
private, branch main. v0.1.0 remains the accepted stable checkpoint.

The release commit is resolved by the annotated v0.2.0 tag. Remote CI for that
exact commit is a release gate: no tag/release is created before all four CI jobs
are green. The operator handoff records the full commit SHA and CI URL after the
gate completes; local test results below do not substitute for remote CI.

## Changes and acceptance

1. Configurable deterministic technical filtering without LLM or keywords:
   72-hour freshness, 15-minute future tolerance, a valid canonical URL, title,
   enabled provenance source and 40 description characters by default. Publisher
   can be required explicitly. Thresholds are operator-controlled.
2. Eligible/deferred/excluded decisions preserve all eight check outcomes and
   stable reason codes. Original articles and provenance are never discarded
   for a failed quality check. No editorial truth/interest score is implied.
3. Additive schema 3 stores immutable complete audit batches, full policy/hash,
   engine version, chosen reference time, actual assessment time and input hashes.
   The latest complete batch defines current state. Changed metadata/policy/source
   settings require reassessment; cancellation rolls back an incomplete batch.
4. Selected JSON schema 2 exports only matching persisted eligible decisions,
   rechecks age/current registry in a consistent read-only snapshot and ranks
   all eligible inputs before applying the limit. Priority is publication time,
   metadata completeness and stable article ID. Empty output is explicit and
   explained. The historical schema 1 file is retained.
5. Successful/partial collection assesses once automatically; fully failed or
   interrupted runs preserve the previous filter batch. Manual filter/report and
   fixed-reference-time inspection are available. Dry-run never writes the real DB.
6. Managed backup creation reuses the existing SQLite API. Conservative retention
   keeps the union of newest seven verified backups and all verified backups from
   the last 30 days by default. Standalone rotation previews unless --apply is
   explicit. Manual/corrupt/newer-schema/linked/unsafe files are preserved and
   cannot count as survivors. A durable verified survivor is rechecked before
   every deletion; the only valid copy is never removed.
7. Existing HTTP retry, caching/304, backup engine and atomic export were reused.
   A malformed XML unknown-encoding LookupError is now converted to feed_xml,
   preserving source failure isolation. Deep/oversized/recursive and huge-integer
   policy inputs fail with safe errors.

## Independent review and regressions

Four specialist directions worked in isolated Git worktrees: filtering/storage,
backups, collector resilience and independent QA. QA reviewed feature branches in
its own integration tree before main integration. See SPRINT_2_QA_REVIEW.md.

Final integrated local result: **472 tests PASS, no warnings, 96.01% statement
coverage (1,542/1,606)** on Python 3.14.8. Ruff without cache, format checks and
strict mypy over 18 production files pass. The frozen dependency lock passes.
All original 222 safety scenarios remain; only the intentional selected-export
contract required valid assessed export fixtures and the schema 2 expectation.

New coverage includes full-scan ranking/time boundaries, engine/policy/input
invalidation, audit immutability, interrupted transactions, real SIGTERM/SIGKILL,
truncated/chunked/compressed network streams, Retry-After, malformed validators,
corrupt XML source isolation, legacy migrations and hostile backup directory/file
changes. QA independently checks survivor hashes and rejects substitution.

Review findings fixed before acceptance: current-registry export invalidation,
fully failed run assessment, recursive/huge-integer YAML errors, and large-backup
verification exceeding the container temporary filesystem. The last issue was
reproduced against a valid 67 MB database: the old temporary-copy path returned
backup_verification_failed. It was replaced by guarded pinned-descriptor immutable
reads plus the existing memory snapshot. Working WAL snapshots retain normal
WAL-aware behavior. Independent 41.8 MB and final 67.1 MB backup proofs pass with
the unchanged 32 MiB temporary filesystem. No full temporary disk copy remains.

The first remote Docker job passed every application assertion but failed in its
EXIT cleanup: the host runner could not traverse a mode 0700 fixture directory
created by container UID 10001. The smoke harness now precreates that disposable
managed directory as the host owner, with the same fixture-only 1777 permissions
as its existing export/backup test directories. Production permissions are unchanged.
The fix is independently reviewed and both architecture smokes are rerun; the
release still requires the corrected commit's complete remote CI to be green.

## Docker and constrained workload

Both linux/arm64 (native) and linux/amd64 (emulated) images build and pass offline
container acceptance. It covers non-root UID10001, read-only root filesystem,
zero capabilities, no-new-privileges, no Docker socket/network, persistence across
recreated containers, replay deduplication, filtering/report, selected JSON 2,
backup/restore and managed retention preview/apply. No public port is published.

Final larger run: native arm64 Python 3.13.16 in Docker, one CPU, memory limit
536,870,912 bytes, /tmp 33,554,432 bytes, no networking. Synthetic RSS fixture:
10 sources, 10,000 unique articles, 800-character normal descriptions, three
collection passes / 30,000 observations. Reference time is fixed to
2026-10-17T12:00:00Z, independent of machine date. Measured on 2026-10-17.

| Phase | Seconds |
| --- | ---: |
| RSS parse/normalize/store, first 10,000 | 0.8277 |
| Filter and select all inputs | 0.8590 |
| Replay/dedup, zero new articles | 0.7941 |
| Repeated deterministic filtering | 0.8281 |
| Metadata update, stale gating and reassessment | 1.6055 |
| Consistent snapshot, schema validation, atomic export | 0.4251 |
| Online backup and integrity check | 0.2416 |
| Large managed backup and retention preview | 0.7437 |
| Competing SQLite writer timeout | 0.0623 |
| Restore and integrity verification | 0.4667 |
| Entire workload | **7.0080** |

Process peak RSS: **110,936 KiB / 108.34 MiB**. Cgroup peak including charged file
cache: **325,484,544 bytes / 310.41 MiB**, below the 512 MiB limit. Docker reports
exit 0, OOMKilled=false, one CPU. These are one measured fixture run, not an SLA or
a long-running soak result. Raw measurements are in SPRINT_2_BENCHMARK.json.

Initial filter counts: eligible 6,896 / deferred 840 / excluded 2,264, with all 10,000
articles retained. Changing 1,000 titles makes those assessments stale until
reassessment. Selected export contains 50 eligible records with explanations.

SQLite remained in WAL mode. Logical database/backup size: 67,092,480 bytes
(63.98 MiB), 16,380 pages of 4,096 bytes. WAL before checkpoint: 5,957,552 bytes
(5.68 MiB). Passive checkpoint completed with no busy result and 1,040/1,040 frames.
The URL identity lookup uses sqlite_autoindex_url_aliases_1. A second writer waits
and times out under an explicit 50 ms test timeout, then can acquire a transaction
after the first writer rolls back. Integrity/foreign-key checks and restored
article/source/run/observation counts all pass.

## Real v0.1 archive migration

On 2026-10-18 at 02:45 Bangkok, an isolated Docker volume restored a closed v0.1
archive, migrated schema 2→3, assessed and exported selected candidates. The live
stable volume and original archive were never modified. Input SHA256:
73a7635afd4aa75d630b085c28e2d8adca1ab1288be2ff087f9f765a3b31f146.

All original rows match before/after: 94 articles, 96 article-source links,
127 observations, 2 discovery runs, 3 sources, 96 source GUIDs, 94 URL aliases,
6 per-source outcomes. Canonical legacy-row SHA256 before and after:
5170fa16f13530fc766de28d8f727cfe2dac376c11561c7e548ff4001c840915.
At reference 2026-10-17T19:45:25.894038Z, 74 were eligible and 20 excluded by policy;
all 94 remain stored. Selected export returned 50. A new schema 3 backup verifies.
The original archive hash is unchanged. See SPRINT_2_MIGRATION.json.

The read-only mounted archive was staged as a known closed file on the disposable
writable volume before ordinary restore, because its WAL header can require
temporary SQLite bookkeeping. This is not copying an active working database.
Operational restore inputs belong in the dedicated writable /backups directory.

## Operator commands and deployment boundary

The v0.2 source/image has been prepared and tested, not deployed over the stable
working database. For an upgrade, stop other manual jobs, create a fresh backup
with the accepted v0.1 image, then build v0.2 and initialize/migrate explicitly:

```sh
docker compose run --rm discovery backup --output /backups/before-v0.2-20261018.sqlite3
git pull --ff-only
docker compose build
docker compose run --rm discovery init-db
docker compose run --rm discovery verify
docker compose run --rm discovery filter
docker compose run --rm discovery filter-report --limit 100
docker compose run --rm discovery export --output /exports/candidates.json --limit 50
```

Manual collection: `docker compose run --rm discovery collect --once`.
Dry-run: append --dry-run. Reassessment at a fixed time: `filter --at ISO8601`;
selected export supports the same --at option. Inspect stats with `stats`.

```sh
docker compose run --rm discovery backup-managed --directory /backups/managed
docker compose run --rm discovery backup-rotate --directory /backups/managed
docker compose run --rm discovery backup-rotate --directory /backups/managed --apply
docker compose run --rm discovery restore --input /backups/manual.sqlite3 --confirm
```

The managed-creation command applies configured retention after the new copy is
verified/durable. Standalone rotation previews by default. Blocked rotation exits 1
with reasons and preserves usable copies. Retain off-device backups separately.
For stopping/logs, start with --name waagd-manual, use `docker stop --time 30 waagd-manual`, `docker logs waagd-manual`, then remove the stopped container.
Never use `docker compose down -v` against the operator volume.

Rollback to v0.1 requires a preserved current v0.2 backup AND the matching
pre-upgrade schema 2 backup. Switch/build the target v0.1 code/image first, then
restore the schema 2 archive with that older image's restore command. Restoring
through v0.2 would migrate the archive to schema 3; v0.1 deliberately refuses it.
See RUNBOOK.md, FILTERING.md, BACKUPS.md and DEPLOYMENT.md for full procedures.

## Limits and architectural handoff

No LLM/OpenRouter requests, OpenClaw/agent connections, scheduler, publication,
Cloudflare/DNS/email or existing-agent modifications were introduced. Existing
HTTP retry/ETag/Last-Modified behavior was retained. v0.1 source/tag and working
data remain the rollback checkpoint. No live-feed availability claim is added
by this offline sprint; the earlier v0.1 live source evidence remains historical.

Eligibility is technical, not factual/editorial verification. Unknown dates are
deferred. Semantic clustering, article crawling, scheduled jobs and agent handoff
remain absent. Audit/observations are append-only with no retention pruning.
Consistent snapshots use memory; significantly larger datasets need separate
resource measurement. The measured 64 MiB database is not an unbounded-size claim.
Advisory locks assume local storage and cooperating tools. Backup retention does
not replace off-device protection. A force kill can leave a discovery run marked
running; stale-run reconciliation is deferred to Sprint 3. No sustained uptime,
power-loss hardware or long soak test is claimed.

Architectural review should accept the policy defaults, the eligibility/audit
boundary, JSON schema 2 and conservative retention/operating limits. Sprint 3 does
not start automatically after this release.
