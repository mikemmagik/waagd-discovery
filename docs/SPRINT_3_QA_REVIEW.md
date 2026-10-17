# Sprint 3 independent pre-integration review

Date: 2026-10-18. Reviewed in the isolated `test/sprint3-qa` worktree against
lead changes through `1e67016` and scheduler `eab7ada`, before scheduler feature
integration into main. The reviewer made no production source changes. Fixes were
made by the owning agents and independently retested.

**Scheduler, shared collection, recovery, diagnostics and schema-4 verdict: PASS;
no unresolved blocking defect remains in the reviewed code.** Infrastructure has
its own review gate below. Release readiness additionally requires the lead's
exact-commit CI, both architecture Docker checks, migration and resource evidence.
This review does not activate the persistent operator service or authorize Sprint 4.

## Findings resolved before integration

1. Existing `pipeline.collect` handles `KeyboardInterrupt` by returning an
   `interrupted` summary. The initial scheduler stopped only when an exception
   escaped, so a SIGTERM during fetch could have scheduled another cycle. The
   scheduler now treats an interrupted result as terminal, preserves its summary,
   records `collection_interrupted` and persists `stopped` without another wait.
   Independent injected-result and actual child-process SIGTERM tests both pass.
2. All operation and scheduler locks now use the canonical database path. Initial
   export protection derived sidecars from the original `--db` spelling. With a
   symlink database alias, export could atomically replace the canonical operation
   or lifetime lock file, invalidating kernel-lock exclusion; canonical WAL/SHM
   paths were also unprotected. Four independent regressions reproduced the
   failure. Lead fix `1e67016` protects both canonical and original alias sidecars.
   All four cases now pass. Two additional tests hold actual locks in child
   processes and verify that rejected export preserves the inode and live lock.

## Independent regression evidence

The reviewer added 27 offline cases, separate from feature-owned tests:

- Missing/future-schema status and health fail safely without creating directories,
  lock files, rewriting database bytes or exposing tracebacks.
- A genuine schema-3 fixture is inspected without disk migration or orphan recovery;
  in-memory migration and explicit open preserve all existing rows. Schema-4
  interruption records reject UPDATE/DELETE, and service state rejects non-singleton
  IDs.
- Actual child-held operation locks distinguish active manual runs from orphans and
  prevent recovery/second collection. After lock release, one recovery is recorded;
  another collection does not duplicate it. Database symlinks use the same lock.
- Actual SIGKILL after the first source commit leaves the second source incomplete.
  Read-only status identifies the orphan. The next locked collection records one
  interruption and preserves the article, GUID, URL alias, observation and original
  committed source outcome. Integrity and foreign keys remain valid.
- Actual CLI scheduler SIGTERM during the second fetch exits successfully, retains
  the first source, finishes the run as interrupted, persists stopped state, releases
  both locks and starts no further cycle.
- A live singleton lock with three source HTTP 429 failures remains healthy even
  when the prior persisted service state says stopped; release of the kernel lock
  makes health fail. Process liveness and source availability are separate.
- Scheduler interruption/failure boundaries release the lifetime lock and redact
  exception text. A second session through a database symlink cannot enter a cycle.
- A 30-day wall-clock jump in either direction causes one fresh cycle followed by
  a rebased ordinary delay. Monotonic time prevents backwards-clock starvation.
  Slow failed cycles wait a full interval after completion; busy errors never spin.
- Restart retains the previous full-success timestamp; a partial collection does
  not replace it. No-new-material success remains a successful collection.
- Export through a database alias cannot replace canonical operation/lifetime locks,
  WAL or SHM files, including while the actual kernel lock is held.

All child processes, database files and locks are temporary fixtures. No stable
operator volume, external feed, LLM, publication or external integration is used
by these tests. Controlled clocks remove external-time dependencies.

## Verification

Reviewed combined tree on macOS/Python 3.14.8:

- **543 tests PASS in 6.71 seconds**, no warnings: 472 legacy, 39 scheduler-owned,
  five lead-owned operator tests and 27 independent adversarial cases.
- Coverage: **1,819 / 1,901 statements (95.69%)**.
- Ruff lint with cache disabled: PASS.
- Ruff formatting: PASS, 53 Python files.
- Strict mypy: PASS, 22 production source files.

Legacy scenario assertions remain in place. The reviewed changes to their
HTTP-client injection paths follow the shared orchestration module; they do not
relax source, storage, interruption, export or security expectations.

## Infrastructure gate

Review of Compose, Dockerfile, CI, offline lifecycle fixture and operator docs is
in progress separately. The lead must not treat the code verdict above as evidence
of an infrastructure acceptance run. Final architecture, lifecycle and CI results
belong in the release report.
