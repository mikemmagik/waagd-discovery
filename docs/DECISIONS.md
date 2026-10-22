# Decisions

1. Modular monolith, synchronous bounded HTTP and SQLite for one operator and a
   few feeds. No broker or worker service is justified.
2. URL is primary identity, GUID is source-scoped fallback. Retain aliases and
   immutable per-discovery observations. Do not semantically cluster events.
3. Python 3.13 container; uv frozen lock; small HTTP/XML/YAML libraries, no LLM SDK.
4. Manual one-shot collection only. Restart policy is disabled; no artificial
   daemon or public port. Future scheduling requires a separate change.
5. Ship baseline backup/export and conditional HTTP early to make core ingestion
   safe to operate. These do not enable the future agent integration.
6. Proprietary source by default; no open-source license is granted implicitly.
7. Reject HTTPS-to-HTTP redirects. Request identity encoding; accept bounded
   gzip/deflate and reject other encodings. Pin public DNS addresses while
   retaining original Host and TLS certificate/SNI checks.
8. Restoration requires a recognized application schema, not merely a valid
   SQLite container. Preserve the target if schema validation fails.
9. v0.2.0 is Reliable Discovery as requested for Sprint 2; the scheduler moves
   to a separately accepted Sprint 3/proposed v0.3 release. v0.1.0 remains stable.
10. Filter technical quality/freshness only. No keyword/topic gate. Persist all
    checks in immutable complete batches; never delete low-eligibility articles.
    Fingerprints exclude changing discovery timestamps but cover quality metadata
    and provenance configuration. Export requires matching persisted eligibility
    and rechecks current age. Unknown/stale assessments fail closed.
11. JSON schema v2 intentionally changes the export contract. Retain the v1
    schema and all legacy safety scenarios; update only fixtures/schema assertions
    necessary for selected-candidate behavior. Working DB diagnostics/export remain
    read-only, including legacy schema snapshots.
12. Backup retention keeps the union of newest valid copies and the age window.
    Corrupt or incompatible files cannot count as survivors. Manual copies are
    outside automatic management. Rotation requires explicit operator action,
    not a scheduler. Runtime measurement uses isolated fixture volumes only.

13. Sprint 3/v0.3 is explicitly authorized after v0.2 acceptance. Use the same
    modular application and pipeline, with an opt-in Compose scheduler profile.
    Do not activate permanent operator scheduling during development.
14. Separate lifetime scheduler ownership from per-operation database locks.
    Lock acquisition, rather than age or PID, proves running records are orphaned.
    Append interruption audit and preserve source commits; never auto-restore.
15. Fixed delay starts after completion. UTC plus monotonic deadlines support
    wall jumps/sleep with one current cycle and no backlog. Every failure waits
    the ordinary delay. Manual collection remains independent between cycles.
16. Health means scheduler ownership plus readable recognized SQLite schema.
    Upstream failures/streaks are separate diagnostics; synchronous collection
    does not emit background heartbeats. No broker, queue or metrics web server.
17. Backups remain explicit daily verified managed operations, weekly off-device
    copies. Reuse Sprint 2 retention, avoid per-cycle SSD writes. Audit/observation
    growth remains operator-monitored; no automatic pruning of provenance.
18. Live three-feed snapshot retained default filtering: 95 unique articles,
    75 eligible, 20 excluded only by age; no missing date/short-description issue.
    This is bounded evidence, not a guarantee for arbitrary future sources.
19. Sprint 4 deployment/integration requires separate agreement; no OpenClaw,
    WAAGD-agent, Telegram, LLM or publication integration in v0.3.

20. Sprint 4 is authorized as a file-only boundary. The deployed Intel Mac is
    operator-owned and inaccessible here; no agent/gateway/production changes.
    Use the same modular Python project, no new service/API or LLM dependency.
21. Reuse selected JSON v2 and atomic export after a completed successful/partial
    collection assessment. Export errors do not roll back discovery. Opt-in
    handoff configuration keeps rollout separate from the working v0.3 service.
    Schema 4 remains unchanged; diagnostics use a private metadata sidecar.
22. A separate host entrypoint reads only validated bounded JSON, writes only a
    fixed workspace child, and keeps its atomic delivery ledger outside workspace
    and export roots. No DB, Docker, socket or secret capability is handed off.
    Existing agent privileges cannot be revoked by this interface; the operator
    must verify actual read-only access and consumer behavior separately.
23. Stable identity is the canonical article URL hash, not database IDs or mutable
    metadata. Prepare durably before publication; replay the same pending payload
    after interruption. One pending batch, default ten candidates, no automatic
    pagination or acknowledgement. ACK means explicitly reported processed;
    dismiss is a separate terminal resolution. Both preserve deduplication history.
24. Prompt-injection text remains untrusted JSON data, never a command/template or
    filename. Static handling metadata, text/byte ceilings, no tool/network use and
    explicit expiry checks limit exposure; no claim of semantic injection removal
    or exactly-once LLM execution. The unchanged agent does not auto-consume data.
25. State loss/corruption fails closed rather than reset delivery history. Keep
    initialized marker, ledger and lock together in private backups; rollback of
    this state requires replay-risk review. The ledger caps at 10,000 URL keys and
    stops instead of pruning idempotency history. Sprint 5 received separate
    approval after this interface shipped; see decisions 27–31.
26. Patch 0.4.1 makes all five embedded configuration files root-owned, group
    10001, mode 0440 via explicit Docker COPY flags, with /app/config mode 0755.
    Host source modes must not determine application readability. UID/GID 10001,
    read-only runtime and capability restrictions remain unchanged. Root ownership
    prevents the application from changing file modes even on a writable test root.
    Build regression uses isolated 0600 source files on both architectures; host
    bind mounts remain operator-managed and are not changed by image COPY flags.
27. Sprint 5 develops only a mock-only RC on an unmerged branch. The operator's
    stable Mac and protected pending delivery are inaccessible and untouched.
    Keep Controller, verifier, mock subprocess and Store distinct; no broker,
    permanent service, OpenClaw implementation or LLM SDK is needed.
28. Preserve v1 warning and exact pending bytes. Opt-in delivery v2 authorizes
    only a dedicated trusted verifier. New private Bridge state 2 adds durable
    receipts through explicit idle-only forward migration with verified backup;
    old Bridge cannot downgrade it. Discovery stays schema 4.
29. Article retrieval uses a separate pinned-IP transport with actual-peer checks
    before HTTP/after TLS, all-answer public DNS validation, per-hop checks and
    shared wire/time bounds. HTTP success yields PARTIAL, never factual VERIFIED.
    Verified claim support requires trusted independent assessment; this RC only
    tests that path with synthetic attestations.
30. Store decisions and a validated whole-delivery ACK intent transactionally in
    private Editorial schema 1. Reconcile via public authoritative Bridge receipts
    and atomic conditional ACK. Revalidate durable business results on recovery;
    no exactly-once external model promise, automatic reset or expiry dismissal.
31. Real editor/network/ACK CLI paths remain unavailable in the RC. A future
    documented Gateway-backed agent invocation requires installed-version/tool
    isolation validation and separate operator approval. Same-user private paths
    and prompt policy cannot replace process/tool isolation or factual review.
32. PublicationIntent v1 has one closed cross-repository wire form: one
    `publication-intent.json` document with `auto-<candidate_key>-r<revision>`
    identity, delivery-independent editorial source identity, and a canonical
    full-envelope SHA-256. Discovery creates only data; the site journal owns
    Worker credentials, duplicate reconciliation, kill switch and final
    authorization. A private default-deny policy is required to emit automatic
    intents. The assertion is audit data, never a bearer capability. Until a
    governed real independent evidence source passes staging acceptance, the
    policy remains disabled outside fixtures.
33. v0.6.0 freezes the accepted Sprint 5/6 implementation. The final site
    compatibility pin is `ca112b2ab89a7ec0d82b85b06df07bb86a35b41c`; its validator
    defines the one-item `publication-intent.json` document and full-envelope hash.
    Keep automatic publication default-deny and require VERIFIED evidence, trusted
    policy, and the separate site kill switch. A stable release does not authorize a
    deployment or mutation of the protected v0.4.1 Bridge delivery.
34. Patch 0.6.1 accepts the documented additive OpenClaw 2026.10.3 run-stat
    layout while retaining the legacy nested tool summary. New-format replies must
    attest zero bridge calls, zero terminal tool names, an empty advertised tool
    surface and disabled code mode; missing or conflicting attestations fail
    closed. A nonempty advertised surface is rejected even when the observed call
    counters are zero. This patch changes neither editorial/publication contracts,
    Bridge state nor the site interface.

35. Patch 0.6.2 follows the actual Gateway-backed `openclaw agent --json`
    nesting: run telemetry lives in `result.meta.agentMeta` and the advertised
    policy report lives in `result.meta.systemPromptReport`. The adapter accepts
    only documented neutral terminal-receipt provenance fields in addition to its
    zero-tool attestations; unknown fields, malformed neutral values, missing
    attestations, and conflicting duplicates fail closed. This does not change
    PublicationIntent, Bridge, verifier, site or default-deny contracts.

36. Patch 0.6.3 accepts the observed fully tool-free OpenClaw 2026.10.3
    variant that omits `result.meta.agentMeta.bridgeCalls` and
    `result.meta.toolSummary`. It does not infer zero calls: the alternative
    form requires an empty advertised tool surface, an empty successful-tool
    receipt, disabled code mode, and empty optional attempted/failed tool lists.
    Present bridge counters remain exact and zero-only; missing other required
    attestations, unknown fields and conflicts fail closed.

37. Trusted Publisher policy establishes only a bounded publisher-identity route: exact configured RSS source and URL, HTTPS final host, and fetched article evidence. It never produces independently VERIFIED facts, independent evidence, or claim support. The initial allowlist is The Guardian and Ars Technica; sensitive stories fail closed. The existing manual independent-corroboration route remains the sole source of VERIFIED. The trusted route is paused for automatic publication until the site has a versioned route marker and a transactional, durable UTC quota/receipt implementation.

38. v0.7.0 releases the Trusted Publishers route, bounded 0–5 AI selection,
    PublicationIntent v2 production, tool-free Editorial Gateway, model-assisted
    source-fidelity review, and durable recovery. Satire may use clear rhetorical
    exaggeration, irony and provocative framing that preserves the reported event;
    the fidelity gate continues to block material invented events, serious false
    accusations and fundamental factual distortion. Gateway ambiguity stays
    operator-required and cannot automatically replay, ACK or publish. This
    release does not authorize deployment, scheduler activation or automatic
    publication.
