# WAAGD Discovery v1.0.6 OpenClaw attestation-format hotfix

## Fixed route contract

OpenClaw 2026.10.x places the Free Router route receipt in
`result.meta.agentMeta.terminalReceipt`, but places the other required
attestations in sibling metadata:

- `result.meta.executionTrace.fallbackUsed` must be the boolean `false`.
- `result.meta.terminalReply.modelRouteChange` must exactly equal
  `Model route changed: openrouter/openrouter/free → openrouter/{responseModel}.`

The configured requested and effective pairs remain exactly `openrouter` /
`openrouter/free`; `terminalReceipt.rerouted` must be `true`; and the dynamic
`responseModel` must be a syntactically valid `:free` model. The transition is
checked against that response model, not against a fixed provider model. Any
missing, duplicate-conflicting, paid, malformed or mismatched attestation is
rejected. Route metadata never substitutes for an independently reported cost
when a Gateway cost budget is configured.

Tool-free, terminal-receipt, advertised tool surface and code-mode checks are
unchanged. No OpenClaw configuration, editorial instruction, model route,
Website, Publisher, Scheduler or Telegram integration changes in this release.

## Install on `claw2`

Stop only the existing LaunchAgent. Preserve the existing state directories,
SQLite/WAL files, Bridge ledger, inbox, receipts, intents and exports.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.6
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

Before enabling any new cycle, run the parser only against the already saved
OpenClaw JSON with an offline test or local verification command; do not invoke
OpenClaw, a model, Bridge Sync, Site Publisher or the Scheduler for that check.

## Recovery of `48214d57-728a-48dc-9d56-784ddd48827b`

Use the existing v1.0.5 terminal recovery only after the receipt's expiry. First
inspect the exact Bridge receipt read-only:

```sh
/absolute/waagd-production/runtime/bin/waagd-bridge \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --schema-version 2 inspect-delivery \
  --delivery-id 48214d57-728a-48dc-9d56-784ddd48827b
```

Only when the exact receipt is `pending` and expired, with the existing runtime
stopped, run this single operator-confirmed action:

```sh
/absolute/waagd-production/runtime/bin/waagd-editorial \
  --inbox /absolute/waagd-production-state/bridge-workspace/waagd-discovery/inbox.json \
  --store /absolute/waagd-production-state/editorial/editorial.sqlite3 \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --no-simulation abandon-selection-attestation \
  --delivery-id 48214d57-728a-48dc-9d56-784ddd48827b \
  --operator-confirm
```

Success requires `selection_attestation_abandoned` with
`selection_attestation_missing_abandoned`; repeat the read-only inspection and
confirm the same receipt is `dismissed` with no pending delivery. For every
other result, stop and preserve state. Never replay selection, ACK, manually
dismiss, edit SQLite/WAL or a ledger, or start a Publisher or Scheduler.
