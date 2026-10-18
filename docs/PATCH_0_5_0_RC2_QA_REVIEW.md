# v0.5.0-rc.2 independent QA review

Scope: macOS `umask` compatibility fix for the Bridge private-directory security test. Reviewed in the independent `codex/sprint5-rc2-qa` worktree against the candidate change `7bf342b1ba805b9bc31b303f34727884a3fdb14f`. No production code or Bridge contract was changed by QA.

## Reproduction and fix

Before the patch, `test_qa_private_state_rejects_shared_directory` called `shared.mkdir(mode=0o755)`. With `umask 077`, the resulting mode was `0700`, so the Bridge correctly accepted the directory as private and the test failed. The full baseline suite produced `948 passed, 1 failed` under `umask 077` and `949 passed` under `umask 022`. The sole failure was this test.

The patch calls `shared.chmod(0o755)` after creation and asserts `stat.S_IMODE(shared.stat().st_mode) == 0o755` before checking that the Bridge rejects the shared directory. This makes the hostile fixture explicit and verifies the test precondition. The reviewed diff contains only that test change and the `0.5.0rc2` version changes in `pyproject.toml`, `uv.lock`, and `src/waagd_discovery/__init__.py`; production Bridge v1/v2 code and contracts are byte-for-byte unchanged from rc.1.

## Similar-test audit

Searched test and Docker-smoke sources for `mkdir(mode=...)`, `chmod`, `stat.S_IMODE`, permission assertions, and private-directory rejection. No other security-negative fixture relies on a permissive mode requested only at creation. Fixtures requesting `0700` or `0600` remain private under both `umask 022` and `077`. Tests asserting modes of files or directories created by production code verify its explicit private permissions. The Docker configuration smoke fixture explicitly applies `chmod(0o600)` to its input files before asserting their modes.

## Independent validation

All commands ran against a separate Python 3.14.8 virtual environment installed from the frozen lock in the QA worktree. No live feeds, OpenClaw, operator Mac, or production data were accessed.

| Gate | Result |
| --- | --- |
| Full pytest, `umask 077` | **949 passed**, 12.29 s, 4005 statements / 295 missed (93% rounded coverage) |
| Full pytest, `umask 022` | **949 passed**, 11.81 s, 4005 statements / 295 missed (93% rounded coverage) |
| `ruff check .` | Passed |
| `ruff format --check .` | Passed; 90 files already formatted |
| `mypy src` | Passed; 35 source files |

QA verdict: **PASS** for the source hotfix. Docker AMD64/ARM64 and release/CI gates are separate lead-owned acceptance items; this review does not claim their outcome.
