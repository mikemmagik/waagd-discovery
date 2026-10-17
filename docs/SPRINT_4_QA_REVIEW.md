# Sprint 4 independent QA review

Review date: 2026-10-18. Review performed by an independent QA agent in
`test/sprint4-qa`, with production implementations brought into a separate Git
worktree before approval. All paths, feeds, consumers, workspaces and databases
used for development acceptance were disposable fixtures. The separate Intel Mac
and its real OpenClaw installation were neither accessed nor changed.

## Decision and reviewed scope

**PASS for the reviewed data-only interface and local acceptance.** Automatic
export commits `15cbf2e`/`bc3793f`, bridge `7b0bc0d`, and the focused infrastructure
changes received explicit independent source approval before lead integration.
The final release remains conditional on the lead's complete Docker architecture
matrix and green CI for the exact release commit. This review does not assert
real OpenClaw consumption, operator-host permissions or LLM safety.

Reviewed contracts and implementation include opt-in automatic selected export,
separate handoff diagnostics, bridge strict input validation and bounded
projection, descriptor-based filesystem access, durable transaction ordering,
private delivery history, explicit acknowledgement/dismissal, and Docker/operator
handoff instructions. SQLite remains schema 4; all original baseline test files
are unchanged.

## Local verification

The independent unmodified baseline passed **543 tests** before Sprint 4 changes.
The final independently integrated suite passed **691 tests in 8.88 seconds**:
543 baseline, 22 automatic-export implementation tests, 53 bridge implementation
tests and **73 independently authored adversarial/recovery checks**. Coverage was
**2,526 / 2,683 statements, 94.15%**. Ruff lint and format checks passed for all 71
Python files; strict mypy passed for all 27 production source files.

The independent tests are `tests/bridge/test_independent_*.py` and
`tests/integration/test_sprint4_independent_handoff.py`. They check:

- Duplicate JSON keys at multiple depths, NaN/Infinity tokens, exponent overflow,
  invalid UTF-8, huge integer policy values, and bounded descriptor reads.
- Stale/future exports; incoherent policy, engine, counts, assessments and checks;
  invalid metadata, credential-bearing and oversized URLs; actual UTC freshness
  despite historical reference times; bridge-owned 72-hour/15-minute ceilings.
- Canonical URL identity across different database IDs, tracking parameters,
  fragments, authority case, default ports, metadata changes and process restart.
- Text/provenance limits and control-character projection. Hostile instructions
  remain untrusted data; test strings are never executed or promoted to policy.
- Symlink ancestors, final symlinks, hardlinks, FIFOs, directories, source
  substitution during read, overlapping private/exposed roots and unsafe modes.
- Read-only status preserving file bytes, inode, modification time and modes;
  absent roots staying absent; existing history remaining visible with no inbox.
- One pending batch, exact inbox repair, unknown acknowledgements, expiry
  backpressure, explicit dismissal distinct from processing, and concurrent
  processes sharing one durable history. Busy kernel locks fail promptly.
- Corrupt or vanished initialized ledger history failing closed, without reset
  or redelivery. Bridge import checks confirm no Discovery SQLite/storage,
  HTTP client or Docker client dependency is loaded.
- Protected automatic export targets throughout the entire private DB root,
  separate first-failure diagnostics and diagnostics without SQLite creation.

## Actual process interruption

Three tests use real child processes, a durable ready-file checkpoint and
**SIGKILL**, verifying child exit `-SIGKILL` rather than a Python exception:

1. After durable PREPARE, before inbox publication: restart restores the same
   delivery UUID and payload even when the source export is absent.
2. After inbox publication, before DELIVERED persistence: both persisted files
   parse correctly and restart reuses the exact prepared payload and URL history.
3. After durable ACK, before empty inbox publication: restart repairs the empty
   snapshot and never redelivers the processed candidate; repeated ACK is safe.

Each restart also proves that the operating system released the killed process's
kernel lock. Separate fault-injection tests cover the same ordering boundaries.
These checks establish process-crash recovery, not survival of physical storage
failure or host power loss.

## Independent restricted-container proof

A separate QA-authored proof ran inside the pinned native ARM64 image
`sha256:5f57e4cb584f35eaaa0561c7fecb9e3e2a0b5f49aa23c3e3323c83a7b78f8052`.
The container ran UID 10001 with networking disabled, read-only root filesystem,
all capabilities dropped, no-new-privileges, 32 MiB temporary filesystem,
512 MiB memory, one CPU and 64 PIDs. **No host paths or Docker volumes were mounted.**
Fake roots and synthetic schema-v2 input existed only in the temporary filesystem.

The proof passed hostile-text non-execution, bounded projection, exact tampered
inbox recovery, stable delivery identity, dismissal without claiming processing,
missing-ledger refusal, no SQLite/httpx imports, and absence of DB/socket access.
Machine-readable evidence is [SPRINT_4_QA_CONTAINER.json](SPRINT_4_QA_CONTAINER.json).
The infrastructure acceptance additionally exercises real synthetic RSS through
collection/filtering/automatic export before a separate bridge-only container
phase; its architecture matrix and runtime probes are reported by the lead.

## Findings fixed before approval

- JSON `parse_constant` alone allowed exponent-overflow infinities. Finite float
  parsing and guarded policy numeric checks now reject them and huge integers.
- A vanished initialized ledger was treated as a new install and could replay
  processed URLs. A durable initialization marker and missing-lock/history
  checks now fail closed; normal interrupted bootstrap remains recoverable.
- Read-only status lost pending history when the inbox directory disappeared.
  It now reads private history independently and reports the missing inbox.
- Automatic-target protection covered only immediate DB-directory children.
  It now protects every descendant of the private database root.
- First-export failure diagnostics disappeared when no export existed. The
  saved attempt is now read independently and retained in the error response.

The lead additionally required bounded lock acquisition and honest dismissal of
unprocessed/expired batches. Independent tests confirm both, with URL history
retained and no automatic acknowledgement.

## Operator handoff limitations

No real agent, gateway, OpenRouter, Telegram, Cloudflare, scheduler service or
publication was activated or modified by this review. The existing agent has not
been taught this consumer protocol. An inbox file does not establish editorial
processing. Existing agent privileges cannot be revoked by this data bridge.

The operator must verify the actual approved workspace, host readability and
ownership, tool permissions, update/rollback on the existing named SQLite volume,
and real integration in the separate deployment chat. Follow
[BRIDGE_OPERATIONS.md](BRIDGE_OPERATIONS.md). Keep private state, exports, SQLite,
backups, Docker and secrets outside consumer exposure. Back up the whole stopped
bridge state coherently; never reset history to clear an error. Physical delivery
is at least once with stable IDs, and consumer execution requires its own durable
deduplication. JSON presentation and a static warning do not remove semantic
prompt injection. Sprint 5 remains unauthorized.
