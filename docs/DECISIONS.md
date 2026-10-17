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
