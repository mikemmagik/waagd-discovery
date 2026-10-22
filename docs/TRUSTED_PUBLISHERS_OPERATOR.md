# Trusted Publishers editorial route

This route establishes publisher identity, not independent confirmation of a report's material claims. It remains paused and does not enable automatic publication.

## Discovery policy

`config/trusted_publishers.yaml` initially permits only these exact pairs:

| Publisher | trusted RSS | final article host |
| --- | --- | --- |
| The Guardian | `www.theguardian.com/world/rss`, `www.theguardian.com/technology/rss` | `www.theguardian.com` |
| Ars Technica | `feeds.arstechnica.com/arstechnica/index` | `arstechnica.com` |

The verifier requires HTTPS, an exact configured source ID and RSS URL, safe pinned retrieval, and an HTTPS final canonical URL on the publisher's exact host. An unknown source, changed feed, redirect to another host, retrieval error or sensitive story remains `PARTIAL`.

A successful identity check is stored as `TRUSTED_PUBLISHER`, with publisher ID and RSS URL in editorial audit. It contains only fetched article evidence. It does not create independent evidence, claim support or a `VERIFIED` status.

The existing candidate-specific independent-corroboration route remains the only way to produce `VERIFIED`. Generic automatic corroboration endpoints are no longer authorized.

## Editorial and safety boundary

For `TRUSTED_PUBLISHER`, the free Gateway receives the bounded original report as untrusted material and may return only four creative fields. Trusted code owns URL, source, publisher identity, date, evidence and provenance. Model output with URLs, quotations, dates or high-risk language is rejected. Automated handling holds reports involving personal accusations, sexual violence, minors or acute violence. Political topics are not blocked as a category.

The selector accepts only compact candidate identity, headline, summary and publisher fields. It may choose zero through five candidates; malformed or ineligible IDs fail closed. Candidates are deterministically de-duplicated and split into bounded 25-item model turns. The private Editorial Store records the run ID, UTC day, shortlist hashes, selected and rejected status, reason code, editorial completion and export state; it never stores the model reply or prompt. A completed selection is reused after restart. The local five-item limit is a safety ceiling; the Site remains authoritative for its atomic daily quota.

## Selection failure recovery

An empty, attested selection is a completed run with `selected=[]`. A Gateway, envelope, attestation, budget or selection-schema failure is instead recorded as `operator_required` with a bounded `selection_*` error code; shortlisted entries become `selection_failed` and no editorial decision is fabricated.

Do not ACK, reset or alter the delivery to clear such an error. For a completed Gateway response with a known parsing or schema failure, an operator may make one explicitly authorized retry using the normal isolated Gateway configuration and `waagd-editorial retry-selection --delivery-id <delivery-id> --operator-confirm`. The command accepts only a durable `failed` selection with no decisions or ACK, preserves its attempt history and starts a fresh recorded attempt.

`selection_outcome_unknown` is different: the prior Gateway call may have completed after the local process lost its result. It remains `in_flight`; `retry-selection` refuses it. Preserve the delivery and obtain a separate operator reconciliation plan before any new delivery is created.

## Site dependency before automatic publication

The Trusted Publisher route produces **PublicationIntent v2** with the explicit `editorial.review_route=trusted_publisher` and structured publisher provenance. The existing independently `VERIFIED` route remains PublicationIntent v1. The local producer is contract-tested against the Site v2 fixtures at `10717e490309c4c742097095456200ea4ba24dff`.

The Site release and operator acceptance are still prerequisites for automatic delivery. Its Publisher must keep route provenance in its durable receipt, enforce the daily automatic quota atomically in D1, preserve idempotent replay, keep Telegram outside that quota, and leave `auto_paused` enabled by default. Discovery has no Publisher client and cannot enable publication itself.

## Operator prerequisites for a later isolated run

Use an isolated Discovery copy only. Keep the v0.4.1 scheduler, SQLite database, Bridge state and protected delivery `dbf6d7a2-395e-40c6-8e91-7866aab89e53` untouched. Review the allowlist and source IDs, retain the default pause, and use the existing free, tool-free Gateway only after a separate operator plan. Run editorial processing with its explicit Gateway permission; run the separate `waagd-publication-export` command only with an explicitly enabled publication policy and the exact `publication-intent.json` output name. No command here authorizes a live OpenClaw, Cloudflare, Telegram, RSS or Site run.
