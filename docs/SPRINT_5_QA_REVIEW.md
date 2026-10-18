# Sprint 5 independent QA review

**Implementation review: PASS. Release remains gated by the exact RC branch CI
and the final Docker evidence recorded in the Sprint Report.** This is approval
for the mock-only release candidate, not permission to deploy or process real news.

Reviewed independently from isolated `test/sprint5-qa`, against the approved
v0.4.1 baseline and `docs/SPRINT_5_CONTRACTS.md`. The lead and both implementation
specialists received findings before feature integration; QA owns the independent
regressions, not production contract or component changes.

## Scope and isolation

Tests use synthetic stories, fake DNS/socket/TLS/HTTP data, isolated Editorial
SQLite, Bridge state and workspaces. No real resolver, HTTP source, OpenClaw,
OpenRouter, LLM, publication or production database was used. The production
identifier `dbf6d7a2-395e-40c6-8e91-7866aab89e53` appears only as a forbidden test
value; no production ledger or workspace was opened.

The 698 v0.4.1 tests first passed independently in 8.40 seconds. Later full gates
retain them; no legacy test was deleted or relaxed for this sprint.

## Independent adversarial coverage

QA adds 129 tests across five files:

| Area | Tests | Main observations |
| --- | ---: | --- |
| Editorial contracts | 44 | Source strings stay data; policy is fixed; injected tool/publication fields fail; invented evidence, URLs and dates fail; READY needs verified, fully cited support; schema versions reject booleans/floats. |
| Source transport | 44 | Mixed public/private DNS fails before connect; actual peer is checked before HTTP and again after TLS; original hostname and pinned IP are preserved; environment proxies and secondary DNS cannot redirect the request; unsafe redirects, response encodings, ambiguous headers, oversized/truncated responses, stalled DNS and operation deadlines fail safely. |
| Bridge protocol | 10 | Forged inbox bytes cannot replace the authoritative digest; v1 pending bytes and modification times remain unchanged; inspect is read-only; migration backup/readback and commit failures preserve history; lost ACK remains reconcilable after expiry; dismissal stays distinct from processing. |
| Editorial Store | 15 | Missing detectable state cannot reset history; future/Discovery/negative schemas fail closed; simulation stores remain separate; symlink, hardlink, FIFO and protected roots fail; status is read-only and failed transactions roll back. |
| Recovery/controller | 16 | Five genuine SIGKILL boundaries; missing/malformed/wrong-candidate results cannot ACK; protected and expired deliveries stay pending; an acknowledged batch without Editorial history cannot regenerate; atomic ACK expiry immediately requires an operator; malformed partial requests return a safe error. |

The source address matrix includes loopback, RFC1918, link-local, multicast,
shared address space, Azure metadata, IPv4-mapped IPv6, NAT64, 6to4, Teredo and
zone identifiers. HTML scripts, links, canonical tags and metadata do not add
fetch targets or permissions. HTTP 200 produces PARTIAL, never VERIFIED.

## Genuine process termination

Each recovery test starts a separate process, waits for an fsynced test boundary,
sends SIGKILL and restarts against the same isolated store and Bridge:

1. Verification started, before a durable verification: verification may repeat.
2. Verification committed: verification does not repeat.
3. Editorial adapter called, before a durable decision: adapter may repeat.
4. Decision, candidate state and outbox committed together, before ACK: editorial
   generation does not repeat.
5. Bridge ACK committed, response not received by the controller: the matching
   receipt reconciles the local outbox without repeating generation.

Every recovered SQLite passes `quick_check`; the final decision exists once;
started attempts become interrupted; the outbox becomes acknowledged. This proves
process-crash recovery at these boundaries, not protection against every storage
hardware failure or exactly-once external LLM execution.

## Findings fixed before approval

- Unrelated independent evidence and duplicate source URLs could launder an
  article-only claim into VERIFIED. Support is now checked per claim, with an
  independent/attested item and distinct canonical URLs. Distinct URLs remain a
  necessary structural check, not proof of factual independence.
- READY could omit the independent citations used to justify verification;
  editorial dates could be invented from feed metadata. Both now bind to the
  supplied verification result.
- Invalid timeout policy values could leak arithmetic/type exceptions. They now
  produce the fixed `source_policy_invalid` error.
- Float protocol/private-state versions were accepted by Python equality. Strict
  integer checks now reject them before side effects.
- A surviving store lock with a missing database and initialization marker could
  permit a silent reset. Detectable missing history now fails closed.
- Recovery trusted the outbox without proving all saved decisions and candidate
  identities still existed and validated. Every ACK now rechecks the durable
  manifest, decisions, verification, deduplication rows and unfinished operations.
- A stale acknowledged inbox with a fresh Editorial Store could regenerate work.
  It now requires operator reconciliation before any verifier/editor call.
- A delivery expiring inside Bridge's locked ACK check stayed generically pending.
  Terminal ACK races now immediately require operator action.
- SQLite reference/fixture contexts committed but did not close their connection.
  Explicit closing removes the resource warnings; warning-as-error gates pass.
- Generated macOS temporary paths could include `/var` symlinks. Only the trusted,
  newly created scratch root is canonicalized; external paths retain no-follow
  checks.
- Corrupt partial persisted requests could raise a raw KeyError. Reuse now returns
  the fixed `persisted_result_invalid` error, without regenerating or ACKing.
- Corrupt durable delivery manifests are validated against their trusted SQL row
  identity and digest before recovery. Invalid JSON, empty objects and arrays
  preserve decisions, mark operator_required and never reach ACK.
- Negative Editorial schema versions now fail closed without modifying bytes.
- Docker client termination could leave a smoke container running. Tracked unique
  container names are forcibly cleaned before disposable volumes are removed.

## Component approval checkpoints

QA explicitly approved these original feature commits before lead integration:

- Bridge/contracts: `6c5320b`, `328b990`, `ea10b6a`.
- Source verification: `8076618`, `ac78b04`.
- Controller/store/adapter/CLI: `4b05436`, `0eb3dbd`, `d569eee`, `322582a`,
  `7487d8a`, `46b7f80`, `b61d940`.
- Docker/editorial CI: `7af220f`, `e321d9c`.

Lead cherry-picks have different hashes. The Sprint Report records the final
branch SHA and exact CI evidence. The final independent implementation gate was
**949 passed in 11.56 seconds**, with `ResourceWarning` treated as errors and no
warnings, **92.63% coverage** (4,005 statements; 295 missed), Ruff lint and formatting
for 90 Python files, and strict mypy for 35 production files. This includes every
legacy test, all feature-owned tests and 129 independent QA probes. The final
release-branch CI and Docker evidence remain separately recorded in the Sprint
Report; they must match the final RC runtime sources.

## Limits of approval

The real verifier conservatively retrieves evidence and never creates a VERIFIED
factual attestation. Mock processing returns NEEDS_VERIFICATION and cannot prove
that a general LLM would obey the policy. Structured validation cannot establish
natural-language truth or remove semantic prompt injection.

The CLI refuses real processing and ACK; only trusted test dependency injection
exercises nonsimulation ACK. No real OpenClaw adapter is implemented. Existing
agent instructions, capabilities and sandbox are untouched. Same-user malicious
code is outside the private-path protection model; a real agent requires an
independent operator-reviewed process/tool boundary.

Bridge migration is explicit and blocked while any delivery is pending. Old
Bridge code cannot read the new private state; downgrade/reset is not a recovery
procedure. Discovery remains schema 4; Editorial is a separate schema 1 database.
No installation, persistent process, deployment or Sprint 6 is authorized here.
