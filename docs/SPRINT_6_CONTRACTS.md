# Sprint 6: autonomous publishing integration contracts

Authorized by the owner on 2026-10-19. This is a coordinated, isolated development
effort spanning this repository and `mikemmagik/weareallgonnadie`. Neither repository's
`main`, the operator Mac, existing OpenClaw/Gateway, Telegram bot, Cloudflare account,
DNS or production data is a test target. No paid model calls or production deployment
are authorized. A staging integration run needs separate staging credentials and an
operator-selected isolated target; CI uses fake agent, bot, HTTP API and local D1.

## Ownership and existing contracts

- Lead owns this document, cross-repository wiring, versioned acceptance, release
  gate and deployment guide. Shared `models.py`, editorial contracts, JSON schemas
  and website `shared/contracts.ts` / `docs/api.md` change only through lead review.
- Verification/Gateway specialist owns additive `editorial/` implementation and
  focused tests in an independent Discovery worktree. Existing `SourceVerifier`
  PARTIAL/FAILED behavior must remain conservative; no retrieved page or model
  assertion alone becomes VERIFIED.
- Publisher specialist owns additive website `automation/publishing/` code and
  focused tests in an independent website worktree. It uses the existing authenticated
  Worker POST API and `CreatePostInput` v1 without direct D1 access. API changes, if
  strictly necessary, are proposed to the lead before editing shared contracts.
- Telegram specialist owns website `automation/telegram/` additions and focused tests
  in another independent website worktree. Preserve the current poller's authorization,
  offset and unrelated commands. No second bot or competing poller.
- Independent QA reviews integrated code, adds adversarial E2E/recovery tests and
  approves before any release candidate. Specialists do not review their own work.

## Trust and state flow

Discovery schema 4 and Bridge v1/v2 remain unchanged. The Editor consumes a bounded
Bridge candidate and a structured verification result. Only the trusted verifier may
fetch policy-selected public evidence. OpenClaw receives no API token, Telegram token,
SQLite, Docker socket or shell capability; its JSON is treated as untrusted output.
The controller persists every verification and decision in its independent private
store. A publication intent must identify candidate key, decision revision, evidence
IDs, source URL and the exact content digest. It can be accepted for automatic
publication only when verification is VERIFIED and the corresponding decision is
READY_FOR_REVIEW, with no unknown material claims, unresolved factual risks or
unsupported citations. Any other state is a durable hold/reject, never a fallback
publish. Passing schema validation is necessary but does not establish truth.

Publication is a separate trusted capability outside the agent. It accepts only a
validated intent or an authorized owner's explicit `/publish` command. It has an
operator-controlled default-off kill switch, per-cycle/day request and model-spend
limits, one stable identity per source/revision, a durable submission journal and
idempotent retry against the Worker's existing duplicate response. A timeout after
POST is uncertain: reconcile by identity before retrying; never silently generate a
different identity. The publisher holds the Worker bearer token outside model and
workspace processes. The model cannot select API origin, credentials, endpoint,
command, local path or publication mode.

An unattended verification policy may pre-authorize one fixed independent HTTPS
assertion endpoint with only a `{candidate_url}` query placeholder. It must return a
strict JSON object containing that exact canonical URL and one bounded claim; the
claim must also occur verbatim in the candidate article. The candidate cannot choose
the endpoint, host, path or claim. Failed, malformed or redirected assertions remain
PARTIAL. This is the only policy-selected dynamic verification path; all other links
remain untrusted data and never authorize a fetch.

Telegram ingress accepts private messages only from configured numeric owner IDs.
URL, text and already-supported URL-based media references become staged submissions;
`/preview <id>` reads a saved bounded preview and `/publish <id>` is the only manual
publication authorization. Replayed update IDs and repeated commands must return
the existing publication link without a second POST. Unsupported binary media must
fail clearly until a safe persistent media store is specified. Existing `/delete`
behavior remains available. No model text or external source may impersonate a
Telegram command or owner identity.

## Acceptance gate

One automated acceptance command runs Python unit/integration/security tests,
TypeScript validation, local Worker/D1 integration, fake-Gateway/fake-Telegram
cross-repository E2E for both user journeys, duplicate/restart/timeout/kill-switch
cases, and Docker AMD64/ARM64 regressions. CI is primary evidence. Only after
independent QA and exact-commit CI pass may a single Sprint 6 release candidate be
prepared. A single staging run is documented separately; without staging access,
its result remains explicitly unverified. Production, DNS and paid LLM activation
are separate owner decisions.
