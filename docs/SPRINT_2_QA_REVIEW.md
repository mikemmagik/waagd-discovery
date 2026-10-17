# Sprint 2 independent pre-integration review

Date: 2026-10-17. Review performed on the isolated
`test/sprint2-independent-qa` worktree before the feature branches were merged
into main. Reviewed lead changes through `abe2a45`, filtering `c2d0390`, managed
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

The two added test files contain 30 offline cases:

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
backup/snapshot/verification paths, validates legacy backups on private copies,
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
- **463 tests in 4.23 seconds, no warnings**, including all baseline scenarios,
  feature-owned tests and 30 independent additions.
- Statement coverage: **1,532/1,598 (95.87%, displayed as 96%)**.

All added tests use temporary databases and local synthetic inputs. No stable
operator volume, live feed, LLM, publication, scheduler or external integration
was used for this review. Container and CI results are recorded separately by
the release coordinator.
