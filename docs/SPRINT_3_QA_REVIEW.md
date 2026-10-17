# Sprint 3 independent pre-integration review

Date: 2026-10-18. Reviewed in the isolated `test/sprint3-qa` worktree against
lead changes through `1e67016` and scheduler `eab7ada`, before scheduler feature
integration into main. The reviewer made no production source changes. Fixes were
made by the owning agents and independently retested.

**Scheduler, shared collection, recovery, diagnostics, schema-4 and infrastructure
verdict: PASS; no unresolved blocking defect remains in the reviewed changes.**
Infrastructure evidence is recorded below. Release readiness additionally requires the lead's
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

- **543 tests PASS in 6.43 seconds**, no warnings: 472 legacy, 39 scheduler-owned,
  five lead-owned operator tests and 27 independent adversarial cases.
- Coverage: **1,819 / 1,901 statements (95.69%)**.
- Ruff lint with cache disabled: PASS.
- Ruff formatting: PASS, 55 Python files including the lifecycle scripts.
- Strict mypy: PASS, 22 production source files.

Legacy scenario assertions remain in place. The reviewed changes to their
HTTP-client injection paths follow the shared orchestration module; they do not
relax source, storage, interruption, export or security expectations.

## Infrastructure gate

Independently reviewed infrastructure commits `0d6d65c` and `391b311`: Compose,
Dockerfile, CI, offline lifecycle fixture and operator documentation. Default
`docker compose config --services` contains only `discovery`; the scheduler requires
an explicit profile. The same image and database volume retain UID 10001, read-only
root, dropped capabilities, no-new-privileges, one CPU, 512 MiB, 64 processes,
32 MiB temporary filesystem, bounded logs, no public port or Docker socket.
Scheduler adds `unless-stopped`, CLI healthcheck and a 30-second graceful timeout.

The reviewer independently ran the actual offline Docker lifecycle script twice
with unique disposable volumes. The final run used both reviewed commits and
passed **all 13 checks in 26.746 seconds**: repeated cycles, deduplication, singleton,
manual-lock exclusion, SIGTERM, restart, explicit Docker SIGKILL/restart, actual
Python-process SIGKILL with Engine automatic restart, orphan recovery, recreation,
health, hardening and SQLite integrity. The two fixture articles remained eligible
after every lifecycle transition. Final counts were two articles, 22 observations,
12 runs (ten success and two interrupted), one immutable crash audit and no running
orphan. All test containers and volumes were removed; no operator volume was used.

The final recreated scheduler cgroup peak was **55,341,056 bytes (52.78 MiB)**,
below the 536,870,912-byte bound, with no OOM. This is a small lifecycle proof,
not a throughput or full-run peak claim. The release coordinator separately records
the realistic workload and final image/architecture/CI measurements.

Review requested and verified explicit Mac sleep/off and Docker Desktop availability
instructions, checkout of the reviewed release during UPDATE, and exact v0.2
ROLLBACK commands using the older binary and matching schema-3 backup. Rollback
returns to manual operation. One fixture originally used a fixed future date;
`391b311` now emits current UTC and asserts two eligible candidates at the initial
and final lifecycle gates, strengthening the collection-to-filter proof.

**Infrastructure pre-integration verdict: PASS.** The persistent operator scheduler
remains inactive. Both architecture final builds/smokes and exact-commit CI remain
separate release gates; no result from an older image substitutes for those gates.

## Follow-up: emulated CI lifecycle timing and failure evidence

The initial remote ARM64 lifecycle job timed out while waiting for two cycles.
The preceding one-shot smoke passed; individual CLI invocations under QEMU took
approximately seven seconds. The lifecycle harness had used health probes every
two seconds with a five-second timeout inside the same one-CPU cgroup, and a
60-second phase deadline. Its failure path retained only one Docker log stream,
without inspect or database state, so the original cause cannot be established
from that run. Import-heavy probes competing with startup are a plausible cause,
not a confirmed application defect.

Independently reviewed harness-only fix `708c96b`. Probes now use the production
30-second cadence, an explicit 30-second start interval/period, and a test-only
20-second probe timeout to accommodate emulation. Phase waits are bounded at
180 seconds, singleton CLI startup at 60 seconds, and polling runs once per second.
Production Compose, application code, one-CPU/512-MiB limits and all functional
assertions are unchanged. Production health timeout remains ten seconds.

Before cleanup, failure diagnostics now retain selected container inspect data
(including exit, OOM, health results, restart count and limits), both Docker log
streams and best-effort database state. The independent reviewer exercised this
function against a deliberately failed, isolated container: exit 23, OOM false,
restart count zero, both synthetic stdout/stderr markers and the empty database
state were captured correctly. Its temporary container and volume were removed.

On the exact revised tree, independent native quality checks passed: **543 tests
in 8.76 seconds**, coverage **1,819/1,901 (95.69%)**, Ruff lint/format for 55 files
and strict mypy over 22 source files. The owner supplied complete actual lifecycle
results, inspected by QA: native ARM64 **59.629 seconds**, local AMD64 emulation
**66.647 seconds**, all 13 checks passing. Both retained two eligible articles,
50 observations, 26 runs (24 success, two interrupted), one crash audit and no
running orphan. Final recreated-session cgroup peaks were 55,758,848 and
90,558,464 bytes respectively, below the unchanged memory limit.

A separate disposable wrapper copy delayed its first startup by 65 seconds,
exceeding the former phase deadline. The full revised lifecycle passed in
126.726 seconds with two eligible articles. The tracked fixture and image were
unchanged, and all test resources were removed. This validates the larger bounded
startup allowance; it does not reproduce the original unobserved CI cause.

The revised harness is approved for integration. Final corrected-commit remote CI
remains mandatory; local emulation and extended deadlines do not by themselves
establish the cause of the original remote failure or satisfy that release gate.
