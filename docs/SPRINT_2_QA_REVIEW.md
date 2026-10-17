# Sprint 2 independent pre-integration review

Date: 2026-10-17. Review performed on the isolated
`test/sprint2-independent-qa` worktree before the feature branches were merged
into main. Reviewed lead changes through `abe2a45`, filtering `1383239`, managed
backups `94666e0`, and resilience `99aed57`.

**Verdict: no unresolved blocking defect found; the reviewed changes are suitable
for integration.** Release acceptance still requires the coordinator's constrained
benchmark, container architecture checks and CI evidence. This review makes no
availability or editorial-quality claim.

## Findings resolved during review

1. Export originally consulted the stored source registry without loading the
   current trusted registry. A source disabled, removed or edited in configuration
   could retain its previous eligible decision. Export now synchronizes the current
   registry into its memory snapshot before selection. Independent CLI tests verify
   disable/remove/name/URL changes invalidate decisions without changing disk, and
   eligible source edits become exportable only after explicit reassessment.
2. Automatic assessment originally also ran after every source fetch failed.
   The CLI now assesses only successful or partial collections. An independent
   failure test confirms that a fully failed run preserves the previous audit.
3. The retention policy loader needed to handle deeply nested YAML recursion as a
   safe configuration error. The backup branch now catches this case; an
   independent 3 KiB hostile nesting fixture passes without a traceback.

No production files were edited by the independent reviewer. Fixes were made by
the corresponding owners and reviewed again in this isolated integration.

## Independent checks

The two added test files originally contained 30 offline cases, with three
additional focused regressions from the backup boundary follow-up below:

- Freshness and future tolerance at the exact boundary and one microsecond on
  either side, including stable complete check ordering.
- Ranking after full filtering rather than truncating the raw article listing;
  stable publication/completeness/ID ordering across repeated assessments.
- Latest-complete-batch precedence, deferred decisions requiring reassessment,
  and metadata/policy/current-source changes invalidating selected export.
- Complete audit rollback on interruption, immutable batch/decision records, and
  a real SIGKILL after an uncommitted decision insertion. Reopening the database
  verifies integrity, retains all prior data and exposes only the previous
  complete assessment.
- v0.1/schema-2 snapshot and restore migration on isolated fixture copies,
  retaining original backup bytes and keeping migrated articles unassessed until
  a new batch is explicitly recorded.
- Retention preview and apply with valid, manual, pre-restore, corrupt, newer
  schema, symlink, FIFO, directory, hardlink and unsafe filename entries. Only
  verified obsolete managed files are deleted; reports contain safe basenames.
- No-valid/sole-valid survivor cases, survivor corruption after scanning,
  replacement of an obsolete file by a symlink, and new-backup fsync failure.
  These failures retain the older verified backup and do not follow symlinks.

The reviewed filtering code streams all article/source inputs, fingerprints
quality and current provenance inputs, gates persisted eligibility by matching
policy/engine/fingerprint, and reevaluates freshness at export. The append-only
batch and decisions commit atomically. The retention code reuses existing SQLite
backup/snapshot/verification paths, validates legacy backups on memory snapshots,
locks the dedicated directory, and rechecks survivor and file identities before
deletion. The benchmark uses disposable fixtures and records resource limits.

Legacy test changes were inspected against the Sprint 1 baseline: all safety
scenarios and original safety assertions remain. Export fixtures add valid dated
metadata and assessment, current source configuration, and the intentional v2
schema expectation.

## Verification evidence

On macOS/Python 3.14.8, the isolated combined tree passed:

- Ruff lint with its cache disabled and formatting checks for all 45 Python files.
- Strict mypy over all 18 production source files.
- **466 tests in 3.81 seconds, no warnings**, including all baseline scenarios,
  feature-owned tests and 30 independent additions.
- Statement coverage: **1,539/1,603 (96.01%, displayed as 96%)**.

## Follow-up: managed backups beyond the 32 MiB temporary filesystem

A measured release boundary exposed a complete temporary disk copy in managed
backup verification. A valid backup larger than the production container's
32 MiB `/tmp` could not be verified. The independent regression reproduced
rejection of a 41,844,736-byte valid backup when temporary disk allocation was
unavailable.

Reviewed owner fix `05ffd249` against lead `45ef653`, including the guarded
immutable snapshot option and preservation of file-descriptor aliases. Verification
now opens a checked standalone descriptor read-only/immutable and uses the existing
memory snapshot. Regular-file identity, hardlink/symlink and sidecar checks remain
in place, including revalidation before any old backup deletion. Working database
snapshots retain their ordinary WAL-aware behavior.

Three added independent cases verify large-backup preview/create without temporary
disk, replacement by a symlink during immutable snapshotting, and safe rejection of
a 5,000-digit YAML integer. The earlier corruption, legacy, manual-file, survivor,
substitution and fsync-failure scenarios still pass. The YAML integer initially
escaped as `ValueError`; the owner fix now returns a safe configuration error.

The final combined native run passed **472 tests in 5.02 seconds with no warnings**,
Ruff without its cache, formatting and strict mypy. Coverage is
**1,542/1,606 statements (96.01%)**. No unresolved blocking defect remains in the fix.

An additional independent Docker run used the reviewed source through a read-only
overlay, a fresh disposable data volume, non-root/read-only runtime, one CPU,
512 MiB memory and the original 32 MiB `/tmp`. With 9,000 observations and a
41,844,736-byte backup, preview, managed creation and applied rotation all passed.
The retained backup hash was unchanged, and `/tmp` remained empty. Observed peak
process RSS was 71,084 KiB; the proof completed in 1.188 seconds. Cgroup values
were `memory.max=536870912` and `cpu.max=100000 100000`; temporary filesystem size
was 33,554,432 bytes. The dedicated test volume was removed afterward. This proves
the focused boundary; the coordinator separately rebuilds the final release image
and repeats the larger workload and architecture checks.

All added tests use temporary databases and local synthetic inputs. No stable
operator volume, live feed, LLM, publication, scheduler or external integration
was used for this review. Container and CI results are recorded separately by
the release coordinator.
