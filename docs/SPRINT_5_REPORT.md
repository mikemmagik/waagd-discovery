# Sprint 5 report — Editorial Processing & Source Verification

**Outcome:** mock-only `v0.5.0-rc.1` review candidate from the accepted v0.4.1
baseline (`7671b79bd139a9ec237147a15b555da61fc37b37`). Work is on the
unmerged `sprint5/editorial-processing` branch. The release tag and exact commit
are identified in the GitHub release once the exact branch CI passes. The Intel
MacBook deployment and its protected pending delivery were not accessed.

## Implementation and contracts

- Discovery remains schema 4. The collector, filtering, scheduler, Compose
  service, five embedded configurations and existing selected-export schema 2
  were not changed. The CLI package version is `0.5.0rc1`; `waagd-editorial`
  is a new, explicit one-shot entrypoint.
- Bridge v1 is the default. Existing v1 warning, schema and pending bytes retain
  their original meaning. Opt-in Bridge v2 adds a fixed trusted-verifier policy,
  private state version 2, authoritative read-only delivery inspection and an
  atomic ID/digest/expiry conditional ACK. A v1 pending delivery blocks an
  upgrade around it. State migration is explicit, idle-only, and makes a
  verified owner-only pre-migration ledger copy. Old v0.4.1 Bridge cannot read
  state 2; this is documented as a separate rollback boundary.
- Source Verifier uses dedicated pinned-IP article transport. It checks every
  A/AAAA address, actual connected peer before HTTP and after TLS, original
  hostname certificate/SNI, each redirect, metadata/transition addresses,
  MIME, headers, body, total time and shared wire bytes. It ignores environment
  proxies and cannot derive new fetch targets from story text or page links.
  HTTP 200 yields PARTIAL with evidence and uncertainties, never factual VERIFIED.
- Editorial Controller validates the inbox against a public Bridge receipt,
  expiry and protected IDs before work. It orchestrates verification and a
  bounded JSON request to a separate fixed mock editor process. Structured
  results record READY_FOR_REVIEW, NEEDS_VERIFICATION or REJECTED with source,
  dates, claims, evidence, risks, decision and versioned history. READY requires
  a structurally independent VERIFIED attestation; only synthetic fixtures
  exercise it in this RC. No generic real factual corroboration is claimed.
- Separate Editorial SQLite schema 1 owns delivery/candidate identities,
  verifications, decision revisions, attempt history and an ACK outbox. It uses
  owner-only files, strict schema/integrity checks, FULL synchronous transactions,
  an exclusive cooperating process lock and no automatic reset. Results and
  whole-batch ACK intent commit before a conditional Bridge ACK. On restart,
  completed decisions are reused and pending receipts reconciled. Missing or
  corrupt durable state, stale acknowledgements and expiry require operator
  resolution; a delivery is not regenerated or silently dismissed.
- All enable, network, LLM, auto-ACK and publication controls default false.
  The RC CLI supports offline dry-run and explicitly enabled persisted mock
  processing, status and reconsideration. It refuses real processing and ACK.
  Only isolated dependency-injection fixtures test non-simulation ACK. There is
  no real OpenClaw adapter, model call, new agent, service, scheduler activation
  or publication capability.

Architecture, trust boundaries, state machine and migration rationale are in
[EDITORIAL_ARCHITECTURE.md](EDITORIAL_ARCHITECTURE.md) and
[SPRINT_5_CONTRACTS.md](SPRINT_5_CONTRACTS.md). The proposed future supported
agent surface is the documented Gateway CLI, subject to a separate operator
security review. The installed `waagd` agent and its instructions/tools are
unchanged.

## Verification and regression

| Gate | Result |
| --- | --- |
| Existing v0.4.1 regression set | All 698 tests preserved and passing. |
| Full local Python gate | 949 passed in 12.32 s; 4,005 statements, 297 missed (92.58%); no warning with `-W error`. |
| Independent QA gate | 949 passed in 11.56 s; 4,005 statements, 295 missed (92.63%); 129 independent adversarial tests. |
| Ruff / mypy | Lint and format on 90 Python files, strict mypy on 35 source files: PASS. |
| Locked dependencies / tracked secret scan | `uv lock --check` and `scripts/check-secrets.py`: PASS. |
| Native host mock fixture | Installed console entrypoint, real synthetic Bridge v2 receipt, two candidates, default-off, dry-run, persisted replay, read-only status, denied real ACK/processing, private modes: PASS. |
| Discovery v0.4.1 compatibility | Disposable schema-4 volume and verified backup; 12 table hashes unchanged across RC open/configured export/old-image reopen; new collection deduplicated 4 observations, exported 3 and retained identities/history; old image reopened new database: PASS. |
| Docker ARM64 and AMD64 | All five smoke gates passed for each final runtime image; disposable containers and volumes removed. |
| Exact branch CI | Four successful jobs on the tagged commit are required before the RC release; the release report asset records their run URL and timings. |

Docker builds use isolated source copies with all five embedded YAML files chmod
0600 before build, then verify their readability under UID/GID 10001. Each image
runs the original four smokes (configuration, persistence, scheduler, Bridge) and
the new editorial smoke. The editorial process uses disposable data, separate
input/workspace/Bridge/Editorial volumes, no network, read-only root and export,
no Docker socket, dropped capabilities, no-new-privileges, CPU/PID/memory bounds
and no Discovery data mount. It exercises selected auto-export, v1 no-fetch,
protected and expired deliveries, durable results before ACK, crash/restart,
lost ACK response and receipt reconciliation after expiry. Exact Docker evidence:
[SPRINT_5_DOCKER.json](SPRINT_5_DOCKER.json); independent ARM64 repeat:
[SPRINT_5_QA_DOCKER.json](SPRINT_5_QA_DOCKER.json). Native and compatibility
evidence: [SPRINT_5_NATIVE_HOST.json](SPRINT_5_NATIVE_HOST.json),
[SPRINT_5_DISCOVERY_COMPATIBILITY.json](SPRINT_5_DISCOVERY_COMPATIBILITY.json).

The local ARM64 image ID is
`sha256:b3ca41178ea1f17fc0adc7184736bfc7868d9af90e5e6d0d2be3928ad06b8557`;
AMD64 under emulation is
`sha256:c3a23150955e97ab874a2af866d1d64fc58b0cdc05cc0a38cb0441deff3a9de3`.
The editorial smoke took 5.016 s and 9.581 s respectively. Its largest measured
phase-container memory peaks were 52,490,240 and 90,206,208 bytes under a
536,870,912-byte limit. These are bounded smoke observations, not a throughput or
full-process load benchmark. Scheduler restart/recovery smoke also passed in
57.713 s and 66.611 s with its SQLite integrity and history checks intact.

Independent QA approved the component commits before integration and reviewed
the assembled implementation and operator guide. It verified five genuine
SIGKILL boundaries, SSRF/prompt-injection attacks, strict contract binding,
private path/schema failures and no duplicate decision or early ACK. Findings
and exact scope: [SPRINT_5_QA_REVIEW.md](SPRINT_5_QA_REVIEW.md).

## Limits and operator handoff

This candidate does not semantically establish that a retrieved story is true.
The production verifier returns PARTIAL/FAILED; VERIFIED and READY are tested
with synthetic attestations only. Structured JSON validation does not prove the
truth of generated prose or remove semantic prompt injection from a future LLM.
The mock process has no tool API, but same-user private paths are not an OS
sandbox for an arbitrary future agent. Real OpenClaw tool isolation, model costs,
rate limits, prompts, human review and true article verification need separate
operator approval and tests. External LLM invocation cannot be promised exactly
once; durable results and conditional ACK provide local idempotency.

The protected real v1 delivery
`dbf6d7a2-395e-40c6-8e91-7866aab89e53` (10 candidates, processed 0) was not
read, modified, ACKed, dismissed, reset or migrated. The RC cannot bypass its
pending slot or use v1 to fetch URLs. There is no automatic conversion or
deployment on the Intel Mac. The operator should retain v0.4.1 as the working
release and review [EDITORIAL_OPERATIONS.md](EDITORIAL_OPERATIONS.md) for isolated
installation, coherent backup, migration gate, diagnostics and rollback.

No Sprint 6, automatic publication, real LLM, OpenClaw change or production
activation is part of this result.
