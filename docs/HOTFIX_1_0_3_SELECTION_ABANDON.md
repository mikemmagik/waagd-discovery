# WAAGD Discovery v1.0.3 ambiguous-selection recovery hotfix

## Scope

`v1.0.3` adds one explicit operator-only terminal action:
`waagd-editorial abandon-ambiguous-selection`. It is available only with
`--no-simulation`, a specific `--delivery-id`, and `--operator-confirm`.

It is not a selection retry, Gateway recovery, publication recovery, ACK, or
general Bridge dismissal. It never constructs a Gateway adapter or invokes
OpenClaw, an LLM, editorial processing, Site Publisher, ACK, or Bridge sync.

The action requires all of the following before a Bridge change:

- An expired authoritative Bridge v2 receipt with the exact delivery id,
  payload digest, schema version and expiry.
- A matching persisted Editorial envelope and, while Bridge is pending, a
  matching current inbox.
- `deliveries.status=operator_required`, `selection_outcome_unknown`, and
  `ack_status=not_ready`.
- One `selection_runs` record still `in_flight`, with no saved selection.
- Exactly one failed `selection` attempt with `selection_outcome_unknown`, with
  no decision, ACK outbox entry, PublicationIntent, Site job, editorial trace,
  or export trace.

The Store first writes `selection_abandon_pending`. Bridge then atomically
dismisses only the matching expired receipt. A lost response is reconciled by
the read-only receipt on the next explicit operator invocation. The Store then
writes `selection_abandoned`; its original in-flight selection run and failed
attempt are retained unchanged. Any missing or inconsistent proof returns
`operator_required` and leaves the Bridge delivery pending.

## Install on the existing `claw2` runtime

Stop the existing LaunchAgent first. Preserve every state path and do not reset,
edit, copy, or remove SQLite/WAL files, Bridge ledger, inbox, receipts, intents,
or exports.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.3
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

Keep the existing production wrapper, 600-second Gateway deadline,
`openrouter/openrouter/free` route, Docker Discovery Scheduler, Site arguments,
and token boundary unchanged. Re-enable only the existing LaunchAgent after the
operator action below has a reviewed terminal result. Do not install another
Scheduler, Publisher, or LaunchAgent.

## Recovery of `583145d8-ef81-41d1-8ce4-3214b16ca2ad`

First perform this read-only check. Do not run `sync`, `ack`, `dismiss`, a
selection retry, or the normal autonomous wrapper for this delivery.

```sh
/absolute/waagd-production/runtime/bin/waagd-bridge \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --schema-version 2 inspect-delivery \
  --delivery-id 583145d8-ef81-41d1-8ce4-3214b16ca2ad
```

Proceed only if the receipt names that exact delivery, is `pending`, and its
`expires_at` has passed. Then run exactly once, with the same existing absolute
state paths:

```sh
/absolute/waagd-production/runtime/bin/waagd-editorial \
  --inbox /absolute/waagd-production-state/bridge-workspace/waagd-discovery/inbox.json \
  --store /absolute/waagd-production-state/editorial/editorial.sqlite3 \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --no-simulation abandon-ambiguous-selection \
  --delivery-id 583145d8-ef81-41d1-8ce4-3214b16ca2ad \
  --operator-confirm
```

Success is the exact operation status `selection_abandoned` with
`error_code=selection_outcome_unknown_abandoned`; it is not an ACK and it does
not claim a successful selection. Verify it with the same read-only
`inspect-delivery` command: the matching receipt must report `dismissed` with
the same digest, schema version and expiry. The Editorial status must show
`selection_abandoned`, `ack_status=not_ready`, an `in_flight` selection run, and
the retained failed `selection_outcome_unknown` attempt.

If any check yields `operator_required`, an unexpected receipt, an unexpired
delivery, or an error, stop. Do not repeat selection, edit local state, remove
the pending delivery, ACK, publish, or manually dismiss it. A later explicit
read-only reconciliation may safely complete only an already-dismissed Bridge
receipt.
