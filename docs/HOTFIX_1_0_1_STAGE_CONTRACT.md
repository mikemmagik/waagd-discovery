# WAAGD Discovery v1.0.1 autonomous stage-contract hotfix

## Cause and boundary

`waagd-autonomous-cycle` accepts an upstream success only as the exact object
`{"status":"completed"}`. This is intentional: a syntactically valid child
response is not evidence that a side-effecting operation completed.

The public `waagd-bridge sync` command instead prints its operator summary. A
successful new inbox therefore prints `{"status":"delivered", ...}`, which
v1.0.0 correctly treated as unconfirmed and stopped before editorial work. The
same generic composition would also reject the public
`waagd-editorial gateway-process` results such as `acknowledged` and
`inbox_empty`.

v1.0.1 adds two opt-in adapters without changing either public CLI contract:

- `waagd-bridge-stage` calls `Bridge.sync()` and emits `completed` only for a
  structurally complete `idle` summary or a durable, unexpired `delivered`
  summary. It rejects `prepared`, `pending_expired`, malformed summaries and
  every other status.
- `waagd-editorial ... gateway-stage` runs the same Controller operation and
  emits `completed` only for an acknowledged Bridge receipt, an empty/missing
  inbox, or a proven expired-delivery quarantine. Gateway, Fidelity, ACK,
  publication and unknown outcomes stay `operator_required`.

Neither adapter retries a Gateway request, creates an ACK, alters an intent or
normalizes arbitrary child stdout.

## Update on `claw2`

Run as the dedicated production service user while the production LaunchAgent
is disabled. Do not remove or recreate any state directory, SQLite/WAL file,
Bridge ledger, inbox, intent, receipt or export.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.1
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-bridge-stage --help
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

In the existing fixed cycle wrapper, make only these two command substitutions:

```text
waagd-bridge ... --schema-version 2 sync
=> waagd-bridge-stage ... --schema-version 2

waagd-editorial ... gateway-process
=> waagd-editorial ... gateway-stage
```

Keep all existing absolute paths, timeouts, the 600-second Gateway deadline,
the `openrouter/openrouter/free` isolated Gateway configuration, Site arguments
and token-file boundary unchanged. The complete current wrapper is in
`RELEASE_1_0_0.md`; only its stage command names differ after this hotfix.
Validate the wrapper syntax and ownership/modes offline, then re-enable the
existing LaunchAgent. Do not install a second Scheduler, Publisher or
LaunchAgent.

## Recovery of delivery `e3ebd328-b49f-4927-a394-1c1671e67d05`

First use only the read-only authoritative Bridge receipt:

```sh
/absolute/waagd-production/runtime/bin/waagd-bridge \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --schema-version 2 inspect-delivery \
  --delivery-id e3ebd328-b49f-4927-a394-1c1671e67d05
```

If it reports this exact delivery as `pending` with a future `expires_at`, run
one **temporary copy of the fixed cycle wrapper with its Bridge stage option
pair removed**. Leave the editorial `gateway-stage` and all final Site-delivery
arguments in place. That one-shot continuation processes the existing inbox and
does not invoke Bridge sync or create a second delivery. Stop if it reports
`operator_required`; do not retry a Gateway or Site call manually.

If the receipt is expired, do not ACK or dismiss it through the public Bridge
CLI. Run the same editorial-only continuation once. The Controller performs the
existing state-2 recovery procedure: it first proves the absence of a Gateway
attempt, ACK, PublicationIntent and Site submission, then calls
`Bridge.quarantine_expired` under the Bridge lock. If any proof is missing or
ambiguous, it remains `operator_required` and no delivery is replayed.

If the receipt is not the stated pending delivery, has an unexpected digest or
schema, or the state cannot be read, stop and preserve the state for operator
reconciliation. Never run `sync`, `ack`, `dismiss`, a database reset or an
outbox cleanup as recovery for this historical delivery.
