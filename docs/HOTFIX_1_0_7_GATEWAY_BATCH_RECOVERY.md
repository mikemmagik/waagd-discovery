# WAAGD Discovery v1.0.7 Gateway diagnostics and partial-batch recovery

## Incident scope and diagnostic evidence

The historical delivery `f178cc68-8be2-49d7-8ebf-1e1f9c5ff941` retains only
the aggregate `editorial_gateway_attestation_invalid` code. That code can
represent a model-route, cost, tool-attestation, terminal-receipt or envelope
failure. It is not enough evidence to identify one of those causes, so this
release does not assert a historical root cause.

For future failed Gateway attempts, schema 6 stores only bounded scalars:
operation, delivery/candidate references, safe failure category, child exit
status, elapsed milliseconds, terminal-receipt presence, route/cost/tool
attestation states, and terminal disposition. It never stores prompts, raw
OpenClaw JSON, article content, credentials or session identifiers.

## Install on `claw2`

Stop only the existing LaunchAgent. Preserve every existing state directory,
SQLite/WAL file, Bridge ledger, inbox, receipt, intent and export.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.7
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

Keep `openrouter/openrouter/free`, the current Gateway configuration, Website,
Publisher, Scheduler, Telegram and Cloudflare configuration unchanged.

## Terminal recovery of `f178cc68-8be2-49d7-8ebf-1e1f9c5ff941`

This recovery preserves the completed Fidelity-passed decision as history but
does not create a PublicationIntent, Site submission or ACK for it. It never
replays the failed Editorial Gateway turn. First read the exact Bridge receipt:

```sh
/absolute/waagd-production/runtime/bin/waagd-bridge \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --schema-version 2 inspect-delivery \
  --delivery-id f178cc68-8be2-49d7-8ebf-1e1f9c5ff941
```

Proceed only if it is the exact pending receipt and expired. With the existing
runtime stopped, run once:

```sh
/absolute/waagd-production/runtime/bin/waagd-editorial \
  --inbox /absolute/waagd-production-state/bridge-workspace/waagd-discovery/inbox.json \
  --store /absolute/waagd-production-state/editorial/editorial.sqlite3 \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --no-simulation abandon-partial-editorial-batch \
  --delivery-id f178cc68-8be2-49d7-8ebf-1e1f9c5ff941 \
  --operator-confirm
```

Success is exactly `partial_editorial_batch_abandoned`; then repeat the
read-only receipt inspection and require `dismissed` with no pending delivery.
Any other result, including `operator_required`, requires stopping with state
unchanged. Do not replay a model call, ACK, dismiss manually, edit SQLite/WAL
or ledgers, or start Publisher or Scheduler during recovery.

After terminal dismissal, the next normal scheduled delivery may run as a new
independent batch. This is the minimal isolation mechanism: it does not retry
or reinterpret the old Gateway turn and preserves its audit history.
