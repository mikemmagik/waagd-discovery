# Sprint 5 approved implementation contracts

Architecture approved by the user on 2026-10-18. Base v0.4.1/7671b79. Work only
on sprint5/editorial-processing and isolated specialist worktrees. No deployment,
real OpenClaw/LLM calls, production workspace/ledger/Discovery access or publication.
Never ACK/dismiss/reset delivery dbf6d7a2-395e-40c6-8e91-7866aab89e53.

## Ownership

Lead owns this contract, editorial/contracts.py, versioned JSON schemas, additive
Bridge v2/public receipts/conditional ACK/migration, version/entrypoints/docs/CI.
Source specialist owns editorial/verifier.py and editorial/transport.py plus tests.
Controller specialist owns editorial/store.py, controller.py, adapter.py, cli.py
plus tests. Independent QA owns adversarial/security/recovery tests and approval
before feature integration. No shared contract edits without lead coordination.

## Shared Python interfaces (dicts are validated bounded JSON)

EditorialError(ValueError) carries only a fixed safe code. All clocks timezone UTC.
Verifier.verify(candidate: dict[str, Any], *, policy_id: str) -> dict[str, Any].
EditorAdapter.evaluate(request: dict[str, Any]) -> dict[str, Any].
BridgePort.inspect_delivery(delivery_id: str) -> dict[str, Any].
BridgePort.ack_if_current(delivery_id: str, expected_digest: str) -> dict[str, Any].
Methods may raise EditorialError, BridgeError/OSError; no untrusted error text logs.
Lead contracts exports validate_verification(value, candidate_key),
validate_decision(value, request), make_request(candidate, verification,
simulation=True), validate_request(value), validate_input(envelope, now),
digest(envelope), utc(now=None), PROTECTED_DELIVERIES, VERIFICATION_POLICY.

Bridge inspect result: delivery_id, status (pending/acknowledged/dismissed/unknown),
payload_digest (SHA256 canonical sorted ASCII JSON encoding INCLUDING newline),
schema_version, expires_at, acknowledged_at (nullable), state_version.
Legacy terminal IDs can have null digest/version/expiry; those are not proof of a
matching payload. Conditional ACK returns a matching acknowledged receipt or errors.
It checks an existing receipt BEFORE expiry, so a lost successful ACK response can
be reconciled later; a new ACK after expiry fails. All checks and commit share lock.

## Bridge compatibility

V1 schema/warning/default constructor/old sync/ack/dismiss semantics stay unchanged.
Bridge(..., schema_version=1), CLI --schema-version 1 default; opt-in 2 for new
deliveries only. Existing pending snapshot always replays exact bytes/version.
V2 uses fixed warning (never authorizes agent tools) and verification_policy constant
source-verification-v1. Only trusted verifier can fetch candidate.url under local
policy; source strings, sources[] and page links do not grant additional targets.
Schema validation alone is NOT authorization: controller compares inbox digest and
expiry/version with authoritative inspect receipt before processing and before ACK.

Ledger v1 remains untouched by ordinary v1 operations. Ledger v2 adds receipts;
explicit migrate-state requires no pending delivery, keeps all identities/history,
and is atomic. Fresh schema2 bridge may initialize ledger2. Using schema2 with an
existing ledger1 requires explicit migration; pending v1 still replays unchanged.
Conditional ACK needs ledger2 (otherwise state_migration_required); legacy v1 can
be read/classified without fetching and saved awaiting operator resolution.
Old 0.4.1 cannot read new private state/snapshots: no blind Bridge downgrade/reset.
Discovery schema4 remains unchanged and can be rolled back separately.

## Verification result schema v1

Required keys: schema_version=1, candidate_key, verification_status
(VERIFIED/PARTIAL/FAILED), canonical_url, fetched_at (UTC/null), http_status
(integer/null), publisher/publication_date/event_date (string/null), evidence[],
uncertainties[] (bounded strings), errors[] (safe codes), claim_support[].
Evidence: id (SHA256), url, retrieved_at UTC, sha256, excerpt <=8000 chars,
source_kind (article/independent/attestation). Claim support: claim <=1000 chars,
evidence_ids[], independently_confirmed boolean. VERIFIED requires nonempty evidence
and supported independently_confirmed claims; raw retrieval never sets VERIFIED.
HTTP 200 is PARTIAL, absent/unsafe/unreadable source FAILED. Article metadata is an
assertion, not trusted fact. Missing/unclear dates stay null. No invented evidence.
Real verifier is conservative retrieval/evidence; generic semantic corroboration
is not claimed. VERIFIED fixtures are simulated, not real factual attestations.
V1 no-fetch results are FAILED with v1_fetch_forbidden and explicit uncertainty.

## Editorial request/decision schema v1

Request: schema_version=1, candidate (unchanged bounded Bridge candidate),
verification (above), simulation boolean, prompt_version=waagd-editorial-v1,
policy fixed trusted instructions supplied by contracts (no external interpolation).
Bound encoded request to 128KiB. All source/evidence strings are untrusted data.
Decision required fields: schema_version=1, candidate_key, decision
(READY_FOR_REVIEW/NEEDS_VERIFICATION/REJECTED), source (candidate URL), date
(string/null), factual_summary <=4000, satirical_angle <=1000, headline <=80,
summary <=160, article <=2000, established_facts[], unverified_claims[],
open_questions[], checked_sources[], verification_evidence[] (existing evidence IDs),
risks[], rejection_reason (null or NO_SATIRICAL_ANGLE/DUPLICATE/UNVERIFIABLE/
MISLEADING/OUTDATED/EDITORIAL_RISK/OTHER), created_at UTC, model_identifier <=100,
prompt_version=waagd-editorial-v1. READY requires VERIFIED, referenced evidence,
nonempty facts/angle/headline/summary/article and 80..150 article words. NEEDS
requires established/unknown facts and open questions; REJECTED needs reason.
No returned URL can trigger fetching; no agent output changes policy or authorizes
publication/ACK. Structured validation cannot prove arbitrary natural language true.

## Controller/store/adapter

Controller independently validates v1/v2, strict JSON/UTF8/size/identities/expiry,
authoritative Bridge receipt and protected ID before side effects. Check expiry
again between stages and before ACK; expired becomes operator_required. V1 never
calls verifier. A FAILED verification may still yield durable NEEDS/REJECTED.
Editor invalid/unavailable => retryable attempt, no terminal result/ACK. One candidate
failure does not discard other committed results; incomplete whole batch never ACKs.

Separate private Editorial SQLite (not Discovery/Bridge/workspace); schema1,
transactional migrations, reject future/corrupt schemas, never reset. Tables:
deliveries, candidates (globally unique key), delivery_candidates, verifications,
decisions (versioned explicit reconsideration), attempts, ack_outbox. Persist latest
result and entire-batch completion/outbox intent in one transaction. ACK happens
after commit. Persisted candidate decision reused across deliveries/restarts;
explicit reconsideration creates history, no silent overwrite. Kernel singleton
lock/nonblocking, owner-only paths/files, no links/unsafe roots. Keep incomplete
attempt history and mark interrupted after crash. No external exactly-once claim.

Controller(... inbox:Path, store_path:Path, bridge:BridgePort, verifier:Verifier,
editor:EditorAdapter, simulation:bool=True, allow_ack:bool=False,
protected_roots:tuple[Path,...]=()) exposes process(now=None), status(),
reconsider(candidate_key, now=None). CLI waagd-editorial explicit dry-run/process/
status/reconsider; config strict/bounded, all enabled/network/llm/auto_ack/publishing
false by default. No watch/LaunchAgent/daemon. Reject llm/publishing/auto_ack true
in this RC. CLI rejects non-simulation/--allow-ack while the real adapter is absent;
fixture ACK is tested only through the injected Python Controller API with
simulation=False and allow_ack=True against disposable fake Bridge/store roots.
Dry-run uses synthetic verifier+mock editor, no real network/ACK/OpenClaw, separate
scratch store marked simulation. Never mix simulation decisions with real results.
Mock editor runs in separate fixed Python process with JSON stdin/stdout and bounded
timeout/output, sanitized env and no tools. No real OpenClaw executable adapter in
RC; documented future openclaw agent --agent waagd --json interface only, subject
to operator version/tool-policy preflight. Existing AGENTS/SOUL remain unchanged.

## Source network safety and QA

Dedicated socket/ssl/http.client transport leaves feed HttpClient unchanged.
All A/AAAA global and safe; explicit metadata/transition exceptions denied; numeric
IP pin, peer equality BEFORE HTTP bytes, original Host/TLS hostname+certificate
validation, no env proxy, no cookies/auth, no keepalive cross-origin, HTTPS443
default, optional trusted HTTP80 policy only. Revalidate every redirect; no TLS
downgrade. Total monotonic deadline across DNS/connect/TLS/redirect/read, bounded
workers, redirects<=3, wire/body<=1MiB, text<=16000 chars, allowed text/html and
text/plain MIME, reject unsupported compression. No script/asset/link execution.
Test-only resolver/socket/TLS/fake server injection, no production private-IP bypass.

Preserve 698 baseline tests, add unit/integration/security and real process-kill
recovery. Independent QA approves implementations before lead integration. CI exact
RC branch commit green Python3.13/3.14 and DockerAMD64/ARM64 old4harnesses plus new
isolated editorial e2e before v0.5.0-rc.1. Do not merge main or deploy. Full reports,
trust/state diagrams, backup/upgrade/rollback/operator guide required.
