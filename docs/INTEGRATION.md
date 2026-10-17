# Discovery to WAAGD data interface

Sprint 4 adds a data-only boundary:

`Discovery → deterministic filtering → candidate export v2 → host bridge → JSON inbox`

The separate Intel Mac is operator-owned and inaccessible to development. This
release does not change `waagd`, create an OpenClaw agent, call a gateway or LLM,
activate OpenRouter, send Telegram messages or publish articles. An inbox does not
mean the existing agent knows how to consume it. Consumer adoption and limits
require separate operator review.

## Why a host bridge

The same locked Python project supplies a small host process. It checks one
export, maintains a private delivery ledger and writes one fixed inbox. Discovery
never mounts an OpenClaw workspace. The consumer needs only that inbox, with no
database, export directory, ledger, Docker socket or credential mounts. There is
no HTTP listener, plugin install, OpenClaw API assumption or new service account.

The bridge cannot revoke the existing agent's preexisting permissions. The
operator must independently verify the agent's tools and filesystem access.
Writing bounded JSON cannot prevent an otherwise privileged agent from accessing
SQLite, Docker or secrets.

## Automatic export is opt-in

The release ships `config/handoff.yaml` with `enabled: false`. Once enabled by the
operator, successful or partial cycles with completed filtering write the current
schema-v2 candidate export. Fully failed, interrupted and dry-run cycles do not
publish. Handoff errors are separate from ingestion and do not stop later cycles.
Writes reuse schema validation, same-directory temporary files, atomic replacement
and fsync. SQLite remains schema 4; original news and provenance remain intact.

```yaml
handoff:
  enabled: false
  output: /exports/candidates.json
  limit: 50
```

Container configuration is read-only. Compose mounts no workspace or bridge
state. An enabled automatic output is reserved for automatic writing; use another
path for manual investigations. `waagd-discovery integration-status` reports the
integration separately from scheduler health. A healthy process can have a stale
or invalid export.

## Input and bounds

`waagd-bridge` requires explicit export, existing workspace-root and private
state-directory paths. State must be outside both workspace and export directory;
roots cannot overlap. There is no home scanning or OpenClaw path discovery. Only
`WORKSPACE/waagd-discovery/inbox.json` is published. Initial tests use fake roots.

Input is strict UTF-8 JSON, at most 8 MiB, rejecting duplicate keys and nonfinite
numbers, validated against candidate-v2 schema. It must be generated within two
hours, no more than five minutes into the future, with coherent eligible decisions
and passed checks. Publication freshness is checked against actual UTC and the
export's filtering policy. Historical reference times cannot bypass this check.

Default delivery is 10 candidates, configurable from 1 to 50. Projection caps
titles at 300 characters, descriptions at 1000, publishers at 150, provenance at
five sources with names at 100, and URLs at 2048. Long URLs are rejected, never cut
into another identity. The whole inbox is at most 128 KiB. Original Discovery
texts/provenance remain unchanged. The ledger holds at most 10,000 URL identities
and fails closed at capacity rather than forgetting delivery history.

## Delivery protocol

Candidate identity is SHA-256 of the canonical article URL, independent of row ID,
assessment UUID or metadata edits. New candidates have no ledger identity;
delivered candidates belong to a pending batch; processed candidates were
explicitly acknowledged. Restarts and metadata edits do not make them new.

There is at most one pending batch. The exact delivery UUID and payload are
persisted before publishing the inbox. Retry after interruption uses that same
UUID and bytes. Repeated sync avoids rewriting an unchanged inbox; missing or
changed inbox files are repaired from trusted pending state. Ack persists
processed state before publishing an empty/replacement snapshot, allowing recovery
when interrupted. Wrong IDs fail; retrying the same completed ack is safe.

Expired pending batches block new batches. Expiry never means processed or new.
Resolve explicitly using the trusted ID after checking consumer state. External
source strings and workspace receipt files cannot acknowledge anything.

This provides at-least-once physical publication with stable identifiers, not
exactly-once editorial/LLM execution. A consumer must persist candidate-key
deduplication before work, check expiry and acknowledge only after durable
completion. Tests use fake-consumer ack; the existing agent is unchanged.

## Envelope and untrusted content

`bridge-envelope-v1.schema.json` requires `bridge_schema_version: 1`, a delivery
UUID (null for empty), UTC `created_at`/`expires_at`, static
`trust: untrusted_external_news`, fixed `handling_policy` and bounded candidates.
Each candidate contains `candidate_key`, original article ID, URL, title,
description, publisher, publication time and bounded provenance.

Article/source strings are untrusted data. They never become filenames, shell
commands, tool calls, URL fetches, templates or trusted instructions. JSON encoding,
control-character handling and a fixed warning preserve presentation boundaries;
they cannot prove semantic prompt-injection removal. A consumer must keep these
strings separate from trusted instructions and must not follow or execute news
text. The phrase “ignore previous instructions” in an article remains news data.
No keyword sanitizer can guarantee safety.

Filesystem checks reject symlink ancestors, unsafe object types and hardlinks;
reads are bounded by descriptor and checked for substitution. Private state uses
locking, atomic replacement and file/directory fsync. Corrupt state fails closed;
resetting it would lose durable deduplication. See
[BRIDGE_OPERATIONS.md](BRIDGE_OPERATIONS.md) for operator procedures.
