# Sprint 4 — OpenClaw data interface / v0.4.0

Prepared 2026-10-18, Asia/Bangkok. Accepted checkpoint: v0.3.0,
`e6cddfb292dbb34a09833aeef17b5caa479cfde9`. The existing Intel Mac deployment is
operator-owned and inaccessible here. All acceptance uses fictional news,
disposable databases and fake workspaces. No real agent or production integration
has been activated. Sprint 5 is not started.

The annotated v0.4.0 tag identifies the final commit. Its full SHA, exact-commit
four-job CI result and evidence are recorded in the GitHub Release/operator report
after acceptance. A tag or release must not precede a successful exact-commit CI.

## Delivered behavior

`Discovery → deterministic filtering → selected JSON v2 → host bridge → JSON inbox`

The minimal host-side bridge is preferable here to a new API/plugin or an agent
with database access: it has no listener or OpenClaw dependency, reads one bounded
export, maintains separate private state and writes one fixed inbox. The same
locked Python project provides the separate `waagd-bridge` entrypoint without
importing Discovery storage/SQLite, HTTP or Docker clients. No dependencies changed.

1. **Automatic current export.** Opt-in `config/handoff.yaml` reuses the existing
   selected JSON-v2 export after successful/partial collection and completed
   assessment under the existing operation lock. Failed/interrupted and dry-run
   cycles never publish. Export failure preserves collected data and later cycles;
   no duplicate collector, scheduler, retry, ETag, backup or filter was introduced.
2. **Atomic validated publication.** Both export and inbox use same-directory
   temporary files, file fsync, atomic replacement and directory fsync. Strict
   offline schema/format validation rejects malformed or incoherent input before
   delivery. Configured automatic output cannot overwrite private DB descendants,
   configuration or lock files; manual export cannot compete for its enabled path.
3. **Bounded analysis input.** Automatic export defaults to 50 selected records;
   bridge defaults to 10 per batch, configurable 1..50, with only one pending
   batch. There is no automatic paging or implied LLM processing budget.
4. **Separate identities and states.** SHA-256 of canonical article URL survives
   database-ID, metadata and assessment changes. New, prepared/delivered, processed
   and explicitly dismissed states remain distinct. ACK asserts completion of the
   whole exact delivery UUID. Dismissal retains URL deduplication without claiming
   processing. Neither expiry nor an empty/missing inbox acknowledges anything.
5. **Durable recovery.** PREPARE persists exact IDs/payload before inbox publication,
   then marks delivery. Retry repairs/reuses the same batch; unchanged bytes are
   not rewritten. ACK persists processed history before clearing the inbox. Missing
   initialized history, corrupt state or exhausted 10,000-key history fails closed.
   Kernel locks fail promptly when busy. No automatic history reset/pruning exists.
6. **Restricted workspace handoff.** Explicit absolute paths; no workspace scanning.
   Only `WORKSPACE/waagd-discovery/inbox.json` is written. Private state must be
   outside the whole workspace and export directory, with nonoverlapping roots.
   Descriptor-relative checks reject symlink ancestors/finals, hardlinks, FIFOs,
   devices, unsafe ownership/modes and substitutions. Private directories are
   0700; files 0600. Export may have container ownership if safely host-readable.
7. **Resource boundary.** Discovery mounts no workspace/state; bridge tests mount
   no database, source registry, backups, Docker socket or secrets. The host bridge
   needs no Docker permission. It cannot revoke an existing agent's preexisting
   privileges; operator verification remains required before actual integration.
8. **Text and freshness bounds.** Strict UTF-8 input <=8 MiB rejects duplicate keys,
   nonfinite tokens and exponent overflow. Export age <2 hours/future <=5 minutes;
   article age <=72 hours/future <=15 minutes, tightened by source policy against
   actual UTC. Titles <=300 chars, descriptions <=1000, publishers <=150, provenance
   <=5 sources, source names <=100, URLs <=2048. Long URLs are rejected, not cut.
   Whole envelope <=128 KiB; only the projection is shortened. Originals/provenance
   and SQLite schema 4 remain unchanged.
9. **Untrusted news boundary.** Envelope v1 carries fixed trust/handling metadata.
   Source text never becomes paths, shell commands, templates, network calls or
   trusted policy. Control-character handling and JSON preserve presentation
   boundaries; they do not remove semantic prompt injection. A future consumer
   must separate external strings from instructions, check expiry and durably
   deduplicate candidate keys before execution. The existing agent is unchanged.
10. **Separate diagnostics.** Discovery `integration-status` reports configuration,
    snapshot validity/age/count and saved last attempt/success independently of
    scheduler health. A private `.handoff.json` diagnostic sidecar is separate from
    both SQLite and bridge delivery history. Bridge `status` is read-only.

## Independent QA and local checks

Independent specialists worked in separate Git worktrees; QA approved automatic
export, bridge and infrastructure commits before integration. Full source review
and findings are in [SPRINT_4_QA_REVIEW.md](SPRINT_4_QA_REVIEW.md).

- **691 tests PASS** locally in 8.37 seconds; independent run 8.88 seconds.
  All 543 accepted baseline tests remain, plus 22 automatic-export, 53 bridge and
  73 independent adversarial/recovery tests. No baseline test files were changed.
- Coverage **2,526 / 2,683 statements = 94.15%**, above CI's 85% gate.
- Ruff lint/format: **71 Python files PASS**; strict mypy **27 source files PASS**.
  Frozen lock and tracked-source safety checks pass.
- Three genuine child-process **SIGKILL** checks interrupt after durable PREPARE,
  after publication/before DELIVERED, and after durable ACK/before inbox clearing.
  Restart preserves exact UUID/payload, parses state and never redelivers completed
  work. Exception fault injection, actual cross-process contention, wrong ACK,
  tampered/missing inbox, missing/corrupt history and filesystem attacks also pass.
- Fixed review findings include exponent-overflow/huge numeric input, lost-ledger
  replay, missing-inbox status history, private-DB descendant export protection and
  diagnostics for the very first failed export. No unresolved QA blocker remains.

## Docker, native host and compatibility evidence

[SPRINT_4_DOCKER.json](SPRINT_4_DOCKER.json) records **all three smoke suites PASS
on linux/arm64 and linux/amd64**: existing persistence/export/backup/restore;
scheduler singleton, SIGTERM/SIGKILL, Engine restart, recreation, health, integrity;
and real synthetic RSS → filtering → automatic export → separate bridge container.
ARM64 was native on this development Mac; AMD64 was emulated, not a target Intel
hardware test. Final CI independently builds/runs both architectures.

Each bridge test exports 25 fictional articles, delivers 10, repeats without
rewriting, repairs a removed inbox after process restart, rejects wrong ACK,
accepts repeated explicit fake ACK and delivers a disjoint next batch. Runtime
probes verify UID 10001, no capabilities, no-new-privileges, read-only input,
no DB/socket, 512 MiB, one CPU and 64 PIDs; root is read-only and networking disabled
through container run options. A separate
QA proof uses only temporary filesystem, no host mounts/volumes:
[SPRINT_4_QA_CONTAINER.json](SPRINT_4_QA_CONTAINER.json).

Scheduler lifecycle elapsed **58.699 s ARM64 / 66.119 s AMD64**; peak memory of
the final recreated scheduler cgroup was **56,778,752 / 91,676,672 bytes** under
512 MiB and one CPU. These are small-fixture scheduler measurements, not bridge
peak memory, whole-workload peaks, target performance or an SLA. The accepted
10,000-article SQLite benchmark remains documented in Sprint 3; no storage schema
or collection workload was rewritten. All temporary Docker fixtures were removed.

[SPRINT_4_NATIVE_HOST.json](SPRINT_4_NATIVE_HOST.json) records the installed native
macOS ARM64/Python 3.14 bridge, each command in a fresh process: 25 fictional
records, 10-item delivery, repeat without rewrite, exact inbox repair, repeated
ACK, disjoint next batch, separate dismissal. First sync took 0.62958 s and its
envelope was 5,256 bytes. These timings are observations, not guarantees.

The isolated v0.3→v0.4 same-schema/backup/rollback proof passed:
[SPRINT_4_COMPATIBILITY.json](SPRINT_4_COMPATIBILITY.json). All 12 table-content
hashes matched the v0.3 backup after v0.4 opening/configured export and v0.3
reopening. A real new cycle on a backup-derived copy added no duplicate articles,
recorded four existing observations and exported three candidates. Original
history, article/provenance identities and first-seen times remained; last-seen
refresh was expected. The v0.3 image reopened the post-cycle schema-4 database
with exact table hashes. All disposable volumes were removed; no operator data
or volumes were used.

## Operator update and limits

[BRIDGE_OPERATIONS.md](BRIDGE_OPERATIONS.md) contains update, native installation,
mock acceptance, configuration, delivery/ACK/dismissal, coherent ledger backup,
recovery, disable and rollback procedures. Preserve the existing Compose project,
named SQLite volume, source policies and 30-minute schedule. Stop, record/tag the
old image, make a verified uniquely named backup, check out v0.4.0, build,
verify/init schema 4 and restart the accepted scheduler only after checks. Never
use `down -v` or reset the database. Same-schema code rollback normally requires
no restore; preserve both old/current backups and bridge history regardless.

Auto-export ships **disabled** for operator-controlled activation. Host venv
installation starts no service. Optional foreground watch polls at 60 seconds
(10..3600 allowed); no LaunchAgent, cron or persistent bridge was installed.
The actual export/workspace permissions and existing agent tool access require
operator verification on the separate Mac. A JSON inbox does not establish that
the existing agent consumes it; no real LLM/OpenRouter work or automatic ACK occurs.

Stop bridge processes before coherently backing up the entire private state
(marker, ledger and lock); SQLite backup does not include this external history.
Never clear an error by removing history. Restoring stale history requires
consumer reconciliation. Physical publication is at least once with stable IDs;
external execution is not exactly once. Checksums detect corruption, not malicious
same-user edits. Process-crash tests do not simulate storage loss or power failure.

No changes to waagd, new agent, OpenClaw Gateway, Shmul, Telegram, Cloudflare,
production or publication. No paid/free LLM invocation. Target installation and
real integration checks belong to the operator and separate deployment chat.
Sprint 5 awaits architectural review.
