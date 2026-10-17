# Independent Sprint 0–1 QA review

Reviewed on 2026-10-17 against the integrated collector, storage, CLI and
container changes. The final release evidence is maintained in `QA_REPORT.md`.
This document records the independent security and operational review.

## Findings and disposition

The initial restore implementation accepted empty SQLite files, unrelated
databases carrying the current `user_version`, and application databases with a
required table removed. These cases could replace a working database with an
empty or unusable one. All three were reproduced before the fixes.

The integrated implementation now rejects unversioned restore inputs and checks
the complete expected schema, including constraints, indexes and immutable
history triggers. Independent regression cases verify that rejection preserves
both input and destination bytes, creates no pre-restore backup, and removes
temporary staging files. Future schemas also fail closed.

No unresolved production defect was found in the reviewed Sprint 0–1 scope.

Verification against integrated main `5682157` plus this review's tests:
222 offline tests passed on macOS/Python 3.14.8 with no warnings, 96% statement
coverage (1,020/1,065 covered statements). Ruff lint without its cache, formatting
checks and strict mypy over all 15 source files passed.

## Added independent regression coverage

`tests/integration/test_qa_operations.py` and
`tests/security/test_qa_boundaries.py` add 27 offline cases:

- Legacy-schema stats, verification and dry-run operate on an upgraded memory
  snapshot while preserving every original file and the on-disk schema version.
- Export rejects the database, registry, WAL, shared-memory and lock paths,
  including symlink aliases, before opening the database. Replacing a separate
  hardlink export alias preserves the actual database.
- Invalid, empty, incomplete and future restore inputs preserve the working
  database and clean staging files.
- A database failure after an earlier item in a feed rolls back the entire
  source transaction, preserves committed sources, and permits a later source
  to succeed without orphaned aliases or advanced failed-source validators.
- A real subprocess receives SIGTERM during the second source. It exits 130,
  finalizes the run as interrupted, and retains the first source's committed
  article, observation and validator.
- A same-host redirect revalidates DNS and rejects a newly private address
  before sending another request. IPv6 pinning preserves the original TLS
  hostname and explicit Host port.
- Gzip trailing data and concatenated members fail closed; error responses are
  never consumed. XML encodings do not bypass DTD rejection. A publication date
  whose UTC conversion overflows remains invalid metadata without failing its
  source.

The existing storage and collector suites were also inspected for URL/GUID
conflicts, per-source GUID scope, immutable identities and provenance, tracking
removal without query reordering, decompression bounds, XML limits, DNS pinning,
TLS verification and source transaction rollback.

## Limits

These added tests require no live feeds and make no publication, LLM, article
crawling, scheduling or production-service calls. Container architecture and
live-feed results belong to the coordinator's release evidence. Export remains
unfiltered discovery metadata in this sprint; editorial filtering is deferred
to Sprint 2. Advisory locking protects supported CLI mutators; external SQLite
tools must coordinate separately as documented.
