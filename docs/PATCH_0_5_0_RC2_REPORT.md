# v0.5.0-rc.2 — macOS umask compatibility patch

Baseline: `v0.5.0-rc.1` at `c8d795137a5801ef86a358008bfa11938a0a1abd`.
This patch corrects one security test fixture and updates the prerelease version
to `0.5.0rc2`. It does not change Bridge v1/v2, Source Verifier, Editorial
Controller, storage, Dockerfile, Compose, CI workflow, schemas or configurations.
The working Intel MacBook, Discovery v0.4.1 and `main` are untouched. The rc.1
tag remains at its original commit. The exact rc.2 SHA and completed CI URL are
published with the new prerelease after all gates pass.

## Defect and minimal correction

`tests/bridge/test_independent_io.py::test_qa_private_state_rejects_shared_directory`
created an expected shared directory with `shared.mkdir(mode=0o755)`. On macOS,
`umask 077` masked it to `0700`. The Bridge correctly accepted that genuinely
private directory; the test's expected `BridgeError` was false. At `umask 022`,
the directory was `0755` and the test passed.

The fixture now calls `shared.chmod(0o755)` and asserts
`stat.S_IMODE(shared.stat().st_mode) == 0o755` before testing rejection. This
makes its adverse permission precondition explicit under either `umask`. The
application's permission checks have no observed defect and were not changed.
The package, lockfile and `__version__` advance from rc.1 to rc.2; locked
third-party dependency versions remain the same.

An audit of test and Docker smoke sources searched permission-sensitive directory
and file creation, `chmod`, `stat.S_IMODE` and negative permission assertions.
No other security-negative fixture depended on a permissive mode requested only
at creation. `0700`/`0600` private fixtures remain private under both modes;
production-created private objects are checked for their exact modes. The Docker
configuration smoke explicitly changes every source YAML file to `0600` before
asserting permissions. Independent findings and baseline reproduction are in
[PATCH_0_5_0_RC2_QA_REVIEW.md](PATCH_0_5_0_RC2_QA_REVIEW.md).

## Verification

| Gate | Result |
| --- | --- |
| Before fix, `umask 022` | 949 passed |
| Before fix, `umask 077` | 948 passed, exactly the affected test failed |
| Root full pytest after fix, `umask 077` | **949 passed**, 11.53 s; `-W error` |
| Root full pytest after fix, `umask 022` | **949 passed**, 11.82 s; `-W error` |
| Independent QA, isolated frozen environment | **949 passed** at each mode; 12.29 s (`077`), 11.81 s (`022`) |
| Ruff lint / format | PASS, 90 Python files |
| Strict mypy | PASS, 35 source files |
| Locked dependencies, tracked source scan | PASS |
| Local Docker AMD64/ARM64 | **5/5 smoke gates PASS** on each image; all disposable containers and volumes removed |
| Exact release-branch CI | Four green jobs required before creating rc.2 tag; URL and timings in release assets |

The local Docker matrix builds each image from an isolated context where all
five embedded YAML inputs are `0600`, then runs the five existing offline gates
on that image: restricted configuration loading, persistence/backup,
Scheduler recovery, Bridge and Editorial/ACK recovery. The complete results,
image IDs, limits, timing and disposal evidence are in
[PATCH_0_5_0_RC2_DOCKER.json](PATCH_0_5_0_RC2_DOCKER.json).

| Local image | ID | Scheduler recovery | Editorial recovery |
| --- | --- | ---: | ---: |
| ARM64 native | `sha256:e340c638a9885ad355b9320549aaadfcaf3e21c43dbd2f38531d91f6505382bc` | 57.115 s / 56,348,672 B | 5.273 s / 53,407,744 B |
| AMD64 emulated | `sha256:46ffe3fb253e6d75587c3261bf74d57f2275f3eeb98e3a6a588958dc5b447ac6` | 65.115 s / 91,488,256 B | 10.469 s / 90,693,632 B |

Memory is a measured cgroup peak for the named scheduler or Editorial phase,
not a whole-matrix load benchmark. Both runtime images report `0.5.0rc2`.

The full Sprint 5 architecture and limitations remain as recorded in the
[rc.1 Sprint Report](SPRINT_5_REPORT.md). This patch does not enable real LLM,
OpenClaw processing, real editorial ACK or publication.

## Operator use and rollback

The rc.2 candidate is for a **separate review checkout**. Verify its annotated
tag target, GitHub release assets and SHA-256 manifest before testing.
Run the full offline suite with both `umask` values to reproduce this acceptance
gate. The existing [Editorial operations guide](EDITORIAL_OPERATIONS.md) applies
to isolated fixture data and backup/rollback review; its example tag `rc.1`
describes that historical candidate and must be replaced with `rc.2` when
checking this patch. Do not overlay the running v0.4.1 checkout/container or use
its SQLite/Bridge volumes for tests.

Rollback of a review checkout is simply returning to the unchanged rc.1 tag.
There is no Discovery or Editorial database migration in this patch, and Bridge
state/protocol bytes are unchanged. The protected pending real delivery remains
unprocessed and is not a test fixture. Deployment and any operator decision about
that delivery require separate approval.
