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

The selector accepts only compact candidate identity, headline, summary and publisher fields. It may choose zero through five candidates; malformed or ineligible IDs fail closed. Its log records only counts and reason codes, never raw model responses. It is not a factual or publication authorizer.

## Site dependency before automatic publication

PublicationIntent v1 can carry an honest route marker in existing editorial metadata, but Site v0.1.1 does not persist a structured route or atomically enforce a daily automatic-publication budget. Discovery therefore intentionally continues to export only independently `VERIFIED` decisions.

The Site owner must agree and implement a versioned Publisher change before this route can publish:

1. Add `editorial.review_route` to a new PublicationIntent version, with closed values `independently_verified` and `trusted_publisher`.
2. Persist that route in the durable receipt and publication audit record.
3. In the same D1 transaction that creates an automatic post, count successful automatic receipts for the UTC day and reject the sixth distinct intent. Exact replay returns its existing receipt and consumes no slot.
4. Keep Telegram outside the count, preserve `auto_paused` by default, and apply the transaction to retries and concurrent requests.

Until that Site change is released and accepted, do not export a `TRUSTED_PUBLISHER` decision as a PublicationIntent or enable automatic publication.

## Operator prerequisites for a later isolated run

Use an isolated Discovery copy only. Keep the v0.4.1 scheduler, SQLite database, Bridge state and protected delivery `dbf6d7a2-395e-40c6-8e91-7866aab89e53` untouched. Review the allowlist and source IDs, retain the default pause, and use the existing free, tool-free Gateway only after a separate operator plan. No command here authorizes a live OpenClaw, Cloudflare, Telegram, RSS or Site run.
