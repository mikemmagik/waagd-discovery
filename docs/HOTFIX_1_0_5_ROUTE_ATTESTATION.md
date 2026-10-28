# WAAGD Discovery v1.0.5 route-attestation and recovery hotfix

## Fixed route contract

OpenClaw 2026.10.x reports the required route as two fields:
`requested.provider=openrouter` and `requested.model=openrouter/free`.
`v1.0.4` compared the model field to the combined configuration route
`openrouter/openrouter/free`, incorrectly rejecting a valid Free Router turn.

`v1.0.5` requires both requested and effective pairs to be exactly
`openrouter` / `openrouter/free`. It additionally requires `rerouted=true`,
`fallbackUsed=false`, and a matching `modelRouteChange` from
`openrouter/openrouter/free` to the independently reported dynamic
`effective.responseModel` ending in `:free`. The accepted observed route is
`openrouter/dots-studio/dots-3-note-preview:free`. Any missing, conflicting or
non-free model metadata, another provider/model, a fallback, or a mismatched
route transition is rejected. Existing terminal receipt, cost, tool-free
surface, no-tool-use and code-mode attestations are unchanged.

The receipt encodes that transition as the exact object
`{"from":"openrouter/openrouter/free","to":"openrouter/dots-studio/dots-3-note-preview:free"}`.

## Install on `claw2`

Stop only the existing LaunchAgent. Preserve existing state directories,
SQLite/WAL files, Bridge ledger, inbox, receipts, intents and exports.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.5
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

Keep the existing `openrouter/openrouter/free` configuration, empty fallback
configuration, Docker Discovery Scheduler, Gateway timeout, Site arguments and
token boundary. Do not start a new Scheduler, Site Publisher, Bridge sync or
selection for the historical delivery.

## Recovery of `8a92b5fb-4a6c-41f2-a559-37f954d507d6`

First inspect the authoritative receipt read-only:

```sh
/absolute/waagd-production/runtime/bin/waagd-bridge \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --schema-version 2 inspect-delivery \
  --delivery-id 8a92b5fb-4a6c-41f2-a559-37f954d507d6
```

Proceed only when that exact receipt is `pending` and expired. With the
existing runtime stopped, run this one operator-confirmed action exactly once:

```sh
/absolute/waagd-production/runtime/bin/waagd-editorial \
  --inbox /absolute/waagd-production-state/bridge-workspace/waagd-discovery/inbox.json \
  --store /absolute/waagd-production-state/editorial/editorial.sqlite3 \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --no-simulation abandon-selection-attestation \
  --delivery-id 8a92b5fb-4a6c-41f2-a559-37f954d507d6 \
  --operator-confirm
```

Success requires `status=selection_attestation_abandoned` and
`error_code=selection_attestation_missing_abandoned`. Then repeat the same
read-only `inspect-delivery`: it must report `dismissed` for the exact same
id/digest/schema/expiry, and Bridge must have no pending delivery. No ACK,
PublicationIntent, Site job or LLM turn is created.

For any other result, including `operator_required`, stop and preserve state.
Do not retry selection, run Bridge sync, ACK, manually dismiss, edit state, or
start the Site Publisher. Re-enable only the existing LaunchAgent after the
terminal result is independently checked.
