# Sprint 6 architecture audit

Audit date: 2026-10-19. This is an engineering inventory, not a release or
production authorization.

## Development state

- Stable baseline: `v0.5.0-rc.2`, `ec9568d6f86b9662733412a893027fa6f5dbdb3f`.
- Discovery development branch: `codex/sprint6-autonomous-publishing`.
- Audited Discovery HEAD: `32913159387b38594677699c3afd8031ed054fec`.
- Site integration branch: `codex/sprint6-site-e2e`.
- Audited site HEAD: `bc1f301d33643ed9d72c318bd549aeeefa15a8e1`.
- The target Mac, its schema-4 SQLite data, scheduler, Bridge ledger, and the
  pending delivery `dbf6d7a2-395e-40c6-8e91-7866aab89e53` were never opened or
  changed. No ACK, dismissal, reset, or migration was performed.

Relative to the stable tag, Discovery adds 18 files / 1,994 inserted lines before
the final cross-repository acceptance commit. All additions are under
`editorial/`, `scripts/`, and Sprint 6 documentation, except for CLI entry points.
There is no Discovery SQLite migration in this work.

## Implemented Discovery-owned components

| Component | State | Notes |
| --- | --- | --- |
| Source verifier | Implemented | Bounded article retrieval; optional private corroboration policy; dynamic assertion endpoint is fixed by trusted policy and rejects redirects. |
| Editorial controller/store | Existing Sprint 5 base, extended | Durable independent store, recovery, conditional ACK, and protected delivery checks remain separate from Discovery SQLite. |
| Gateway WAAGD adapter | Implemented with mock-Gateway tests | Fixed executable/argv, allowlisted environment, timeout, bounded JSON, tool-use rejection, and per-run candidate/cost limits. No real model call was made. |
| Publication eligibility/export | Implemented | Emits only validated VERIFIED + policy-eligible decisions. It cannot write a website database or call an HTTP publisher. |
| Host handoff | Implemented | Validates and atomically copies a fixed JSON batch between private directories. |
| Acceptance | Implemented | Local Python/site/Docker checks and a fake-site journal E2E. |

## Components that are mocks or remain unconnected

- Gateway calls are tested only through a fake executable; no installed OpenClaw
  Gateway version has been exercised.
- The verification assertion endpoint is an interface and offline fixture, not an
  operated verification service.
- Site publishing uses a fake authenticated Worker API in acceptance. No Cloudflare
  account, D1 database, or production token was used.
- The host stages are one-shot commands. They are not attached to the operator's
  scheduler and must not be represented as a deployed autonomous service.

## Responsibility-boundary findings

No prohibited website component was introduced into `waagd-discovery`:

| Searched component | Finding |
| --- | --- |
| Telegram bot/poller/authentication | Absent from `src/`; only the separate site checkout owns Telegram workflow code. |
| Cloudflare Worker, D1, public API, frontend, media hosting | Absent from Discovery. |
| CMS or publisher with site credentials | Absent. `publication.py` creates a data-only candidate-to-intent projection; `publication_bridge.py` copies a file. |
| Competing RSS pipeline | Absent; Sprint 6 uses the existing collector/storage/filtering pipeline. |

The following remain website-owned and must stay there: Telegram transport and
owner authorization, public API, Worker/D1 persistence, frontend rendering, media,
and the HTTP journal that holds the Cloudflare credential.

## PublicationIntent v1 contract gap

The current handoff object is named a publication intent but does **not** yet
implement the corrected contract. It lacks `publication_id`, `delivery_id`,
`publisher`, `published_at_source`, `editorial_status`, `editorial_risks`, and the
requested explicit `origin` field. The website parser currently consumes an older
website-specific envelope. Therefore the current code is not a mutually agreed
PublicationIntent v1 and must not be described as autonomous publishing.

The proposed shared v1 fields, pending site-contract approval, are:

`publication_id`, `candidate_key`, `delivery_id`, `canonical_url`, `source_url`,
`publisher`, `published_at_source`, `headline`, `summary`, `article`, `references`,
`verification_status`, `editorial_status`, `editorial_risks`, `content_hash`, and
`origin`.

Discovery must produce this object only after a separate trusted publication policy
permits it. A `READY_FOR_REVIEW` decision alone is insufficient. The site must use
`publication_id` and `content_hash` for its durable idempotency journal. Discovery
delivery ACK remains independent of a site publication result.

## Verification evidence

The latest local combined acceptance before this audit reported:

- 984 Discovery Python tests passed, 91.06% coverage.
- 111 site/automation tests passed, including the Python-envelope to fake-site
  journal test; 41 local Worker/D1 integration tests passed.
- Docker AMD64 and ARM64 smoke, Bridge, editorial recovery, and configuration
  permission checks passed using temporary data only.

The remote GitHub branches were pushed, but GitHub CLI was not authenticated in this
environment, so this audit does not claim a remote CI result. Independent QA found
and drove fixes for the Gateway-policy, budget, envelope, autonomous-verification,
and redirect cases. Its final follow-up turn could not complete because the QA
agent reached an account usage limit; independent QA approval is therefore pending.

## Required next decision

Freeze the data-only boundary and approve PublicationIntent v1 jointly with the
site contract before changing either producer or consumer. Do not tag a Sprint 6
release candidate or enable autonomous publication until that contract, independent
QA approval, and remote CI are complete.
