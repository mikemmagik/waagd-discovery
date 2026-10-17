# Sprint 4 — data-only integration contracts

Authorized 2026-10-18. Accepted v0.3.0/e6cddfb is deployed on a SEPARATE Intel
Mac, inaccessible here. Never discover/access local or remote OpenClaw paths,
modify agents/gateway/Telegram/LLM/production, or install/start an operator bridge.
Use fake workspace roots and disposable data only. Sprint 5 is not authorized.

## Ownership

Lead owns automatic export integration/config, CLI/scheduler wiring, diagnostics,
existing export protection, version/release, architecture and final report.
Bridge specialist owns bridge package/CLI, delivery envelope schema and tests.
Infrastructure owns Docker/Compose/CI, bridge Docker simulation acceptance and
operator installation/update documentation. Independent QA owns adversarial tests
and pre-integration review in its own worktree. No Discovery DB schema change is
needed: SQLite remains schema 4; bridge state is separate private JSON.

## Automatic export

config/handoff.yaml: handoff: {enabled: false, output: /exports/candidates.json,
limit: 50}. Strict bounded YAML; limit1..500, boolean enabled, absolute/path config
trusted. Global --handoff-config / WAAGD_HANDOFF_CONFIG. Integration is opt-in for
operator rollout. When enabled, successful or partial collection + completed
assessment automatically reuses existing schema-v2 export while holding the
operation lock. Failed/interrupted collection and dry-run never publish. Export
errors are separate result/log diagnostics, never roll back ingestion or stop
future scheduler cycles. Existing file is atomic/schema-validated; generated_at
exposes staleness. Reject DB/config/lock targets and files in DB directory; protect
the configured auto-export path from manual export replacement while enabled.
No LLM and no automatic delivery acknowledgement. Integration diagnostics are
separate from scheduler liveness; CLI integration-status reports auto-export
configuration/latest export validity, age/count/error without modifying data.

## Host bridge

Same Python project, separate `waagd-bridge` entrypoint imports no Discovery SQLite,
Docker or HTTP client. Dependencies stay frozen; jsonschema is reused offline.
Explicit required paths: --export PATH, --workspace-root PATH, --state-dir PATH.
No path autodetection/home scanning; workspace root must already exist. Only fixed
workspace child `waagd-discovery` and fixed `inbox.json` are written. State directory
MUST be outside the entire workspace AND export directory, with no overlapping
roots. No live OpenClaw API/tool calls. Host venv via locked project install;
optional explicit `watch` polling60s (bounded10..3600) or one-shot `sync`. No install
or persistent start in development. All state mutations serialize with flock.
`status` reads only, `ack --delivery-id UUID` explicitly acknowledges ALL candidates
of current pending delivery as processed by the consumer/operator. Wrong/stale IDs
fail safely. No filesystem receipt supplied by external text is auto-trusted.

Input is strict bounded duplicate-key/nonfinite-rejecting UTF-8 JSON, <=8MiB,
validated against existing candidate-v2 schema including formats. Only current
eligible/checks-passed dated records with coherent selection context are considered;
recheck publication freshness/future tolerance against actual UTC, regardless of
manual reference_time. Export generated_at TTL2h, future tolerance5min. Malformed,
stale/future/incoherent exports fail closed. No fetching URLs. Candidate identity
is SHA256 canonical article URL, independent of local DB IDs, assessment UUIDs and
metadata updates. Already delivered/processed URLs do not become new candidates.

Default transfer limit10 (configurable1..50), title300 chars, description1000,
publisher150, <=5 provenance sources with names100, URLs <=2048; whole envelope
<=128KiB. Oversized URLs rejected (not truncated into another URL); textual limits
are applied only to bridge projection, original Discovery data/provenance remains.
No automatic looping through pages: at most ONE pending batch until explicit ack.
Ledger max10000 URL keys, pending max50; fail closed on capacity rather than prune
idempotency history. No unrestricted inbox backlog or automatic article deletion.

Envelope schema `bridge-envelope-v1.schema.json`: bridge_schema_version1,
delivery_id UUID (or null for empty), created_at/expires_at UTC, trust static
`untrusted_external_news`, handling_policy static trusted data-only warning,
candidates bounded fields including candidate_key (64 lowercase hex), original
article id/url/title/description/publisher/published_at and bounded provenance.
Exact final schema owned by bridge specialist; document it. News content never
becomes filenames, shell/HTTP/tool commands, templates or trusted instructions.
JSON encoding/plain text/control-character treatment helps presentation but CANNOT
prove semantic prompt-injection removal. Consumer MUST treat all article/source
strings as untrusted, check expiry and durably dedupe candidate keys before work.
Do not modify the existing agent to install these rules in this sprint.

Delivery transaction: durably PREPARE exact envelope+identities in private atomic
JSON ledger BEFORE publishing inbox, then record delivered. Retry after any crash
uses the SAME prepared delivery id/payload, not a second new delivery. Idempotent
sync avoids rewriting unchanged inbox; missing/tampered inbox can be restored only
from trusted pending ledger. ACK persists processed state first, then publishes
empty/replacement snapshot; recovery repairs publication after a crash. Same-ID
ack can be idempotently accepted after completion; unknown IDs never accepted.
A pending expiry must not make it newly deliverable or silently processed; block
new batches and require explicit ack/operator resolution. Empty/expired inbox
metadata must never imply processed. Document at-least-once physical publication,
not exactly-once LLM execution. Current agent is unchanged and no editorial
processing occurs in tests, only explicit fake consumer acknowledgements.

Secure file operations: no symlink ancestors/final files, hardlink/FIFO/device
inputs or mutable-state targets; bounded descriptor reads, file identity checks,
private ownership/modes, atomic same-directory replace+file/directory fsync,
secure fixed names and directory confinement. State corruption must fail closed,
not reset and redeliver. Workspace users must not get bridge ledger/exports/DB/
Docker/socket/secrets mounts or paths. Existing agent's preexisting privileges
cannot be revoked by a data bridge; operator must verify those separately.

## Acceptance

All543 baseline tests, new unit/integration/security, independent QA before
integration, ARM64/AMD64 legacy scheduler/container smoke + isolated fake workspace
bridge smoke. Check auto-export success/failure/dry-run, atomicity, duplicate and
restart delivery, PREPARE/publish/ACK interruption recovery, bounds, hostile JSON/
text, symlink/hardlink/FIFO/path substitution and stale/incoherent exports. Document
native host bridge install, opt-in watch/manual transfer, state backup/recovery,
Intel Mac container update from v0.3 without replacing SQLite volume, rollback,
and explicit operator-only real integration test. Exact final commit green CI
before v0.4 tag/release. No Sprint5, no actual target Mac or agent access.

Review clarification: explicit `dismiss --delivery-id UUID` resolves an unprocessed
(including expired) pending batch as dismissed, preserving URL history separately
from processed. No automatic expiry acknowledgement. Bridge lock contention must
fail promptly with bridge_busy, including status, rather than wait indefinitely.
Auto-export must reject every descendant of the private DB directory. Failed first
export diagnostics must expose the saved last_attempt even when no export exists.
