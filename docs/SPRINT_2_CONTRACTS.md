# Sprint 2 — Reliable Discovery contracts

Baseline v0.1.0 is retained. Release v0.2.0 is Reliable Discovery; the scheduler
release moves to v0.3.0. No scheduler or external integration in this sprint.

## Filtering and persistence (Storage/Filtering agent)

Own filtering.py, storage/, config/filter.yaml, tests/filtering/, new storage
filter tests. Do not rewrite collector or existing storage identity logic.

Public API in `waagd_discovery.filtering`:

* frozen `FilterPolicy` defaults: max_age_hours=72, future_tolerance_minutes=15,
  min_description_chars=40, require_publisher=False. Strict finite numeric and
  boolean config validation, unknown keys fail. No keywords or topics.
* `load_policy(path: Path) -> FilterPolicy`, YAML `filter: {policy fields}`.
* `assess(repository: Repository, policy: FilterPolicy, as_of: datetime | None = None)
  -> dict[str, object]`. Assess every article with one aware UTC reference time.
  Append one complete immutable audit batch in a transaction. No article deletion
  or editorial status updates. Record policy hash + full policy, engine version,
  reference time, actual assessment time, input fingerprint, decision and all
  passed/failed checks for each article. Latest complete batch defines current
  filter state; older batches remain available. An interruption rolls back the
  entire batch. Repeating identical inputs/policy/time yields identical decisions
  and ordering (new audit batch IDs/timestamps may differ).
* `select_candidates(repository, policy, as_of: datetime | None = None,
  limit: int = 50) -> tuple[list[dict[str, object]], dict[str, object]]`.
  Read only. Candidate must have an eligible persisted decision in the latest
  complete batch, matching policy and current input fingerprint AND pass the
  same rules at export reference time. Unassessed/changed inputs are not exported.
  Policy change requires reassessment. Freshness is rechecked even after HTTP304.
  Zero candidates is valid, and metadata explains unavailable/stale assessments.
  Do not apply limit before filtering/ranking all eligible articles.
* `filter_report(repository, limit: int = 100) -> dict[str, object]` returns latest
  batch summary + bounded decisions (eligible/deferred/excluded and reasons).

Rules: canonical HTTP(S) URL, nonempty title, at least one currently enabled
provenance source, description meeting configured minimum, optional publisher,
valid publication timestamp, age <= max_age_hours; future timestamp exceeding
tolerance deferred. Missing/invalid date/title/metadata is deferred. Old records
and disabled sources excluded. Collect all check outcomes in stable order.
There is no satirical-interest, political, language or topic scoring.
Rank by published_at DESC, metadata completeness DESC, article ID ASC. Preserve
all original candidate metadata/provenance fields; add `selection` object with
eligible status, reason/check data, policy hash and persisted assessment batch ID.
Selection context must be JSON compatible; send final exact shape to coordinator.

Add only additive transactional migrations to existing MIGRATIONS. Validate old
schemas and exact new schema as before. Index decision lookups. Existing articles,
aliases, GUIDs, observations and runs are preserved. Existing Repository methods
and stats keys stay compatible. Raw Repository.candidates remains a storage
primitive; CLI export must use select_candidates, never this raw listing.

## Export and CLI (Lead)

JSON payload v2 explicitly versions the change from raw discoveries to selected
candidates, includes reference_time/policy hash/assessment metadata and selection
explanation. Retain v1 schema as a historical artifact. Atomic write and schema
validation code is reused. CLI uses selected export only. Baseline tests retain
their assertions of data safety; export-specific fixtures/expected schema are
updated for the intentional new contract (no removed legacy test scenarios).

Global --policy PATH (WAAGD_FILTER default config/filter.yaml). New commands:
`filter [--at UTC_TIMESTAMP]` writes a new audit batch and syncs the trusted source
registry first, `filter-report [--limit 100]` is read only.
Successful/partial collect automatically assesses the collected database once;
dry-run assesses its in-memory snapshot only. Interrupted collect skips filtering.
`export --output PATH [--limit 50] [--at UTC_TIMESTAMP]` reads a snapshot and
exports only matching persisted eligible decisions, rechecking current freshness.
Empty/unassessed selection emits an explicit explanatory context, never raw items.
All mutating commands retain the existing advisory database lock.

## Backup retention (Backup agent)

Own backups.py, config/backup.yaml, tests/backups/, docs/BACKUPS.md.
Reuse Repository.backup, snapshot, SQLite integrity/schema validation. No duplicate
backup engine. API:

* frozen `RetentionPolicy(keep_last: int = 7, max_age_days: int = 30)` with strict
  config validation; both >=1; `load_retention_policy(path: Path)` YAML backup:{}.
* `rotate_backups(directory: Path, policy: RetentionPolicy, *, apply: bool = False,
  as_of: datetime | None = None) -> dict[str, object]`.
* `create_managed_backup(repository: Repository, directory: Path,
  policy: RetentionPolicy) -> dict[str, object]` creates an exclusive verified
  managed backup via existing backup API, then applies rotation.

Operate only managed filenames `waagd-backup-YYYYMMDDTHHMMSSffffffZ-UUID.sqlite3`
inside one dedicated directory. Refuse/fail safe on symlinks/nonregular files;
never recursively delete; unrelated/manual/pre-restore files are not managed.
Use separate directory advisory lock. Validate all candidates read only, reject
future/unrecognized schemas, include recoverable legacy schemas (validate on
memory snapshot, do not migrate backup files). A corrupt/newer file is reported
and preserved, never counted as a survivor. Keep union of newest keep_last valid
backups AND valid backups within max_age_days, always >=1 verified valid survivor.
If no valid survivor, no deletion. Apply requires explicit CLI --apply; default
rotation is preview. Revalidate identity/valid survivor immediately before deletion.
SIGTERM/IO failures may leave extra files, never delete the last verified backup.
Fsync new backup file and directory before any old deletion. Report safe basenames
and fixed reasons only, no private absolute paths in exported metadata.

CLI lead adds backup-managed --directory PATH, backup-rotate --directory PATH
[--apply] and global --backup-policy PATH (default config/backup.yaml).

## Resilience (Collector agent)

Own additional tests/collectors/test_reliability.py and
tests/security/test_reliability.py, minimal justified http/parser changes if a
reproduced defect requires them. Retry, caching, DNS pinning, bounds and XML safety
already exist. Extend tests for truncated/compressed/chunked bodies, redirect
loops, timeouts mid-stream, 429/503 Retry-After, malformed validators and SIGTERM
cleanup. Report findings before changing shared contracts. Do not duplicate HTTP.

## QA and measurements

Lead owns scripts/benchmark.py and container acceptance extensions. Measure full
fixture collect/parse/store, replay/dedup, filter/re-filter, selected export,
backup/restore time, peak/current RSS, SQLite database/WAL/page/lock behavior on
10,000 unique articles / 30,000 observations under Docker 512MiB and one CPU.
No live external network is required. Migration from real v0.1 backup is tested
on an isolated copy; stable working volume is not used for writes.
Independent QA reviews integrated changes and adversarial boundary/recovery tests
before integration. All legacy scenarios, new tests, Docker arm64/amd64 and CI
must pass before the v0.2.0 tag. Report measured limits without availability claims.
