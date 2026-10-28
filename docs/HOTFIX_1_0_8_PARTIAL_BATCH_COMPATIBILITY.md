# WAAGD Discovery v1.0.8 partial-batch recovery compatibility

## Scope

v1.0.7 was too narrow for delivery
`f178cc68-8be2-49d7-8ebf-1e1f9c5ff941`: it rejected every completed
`editorial_gateway` attempt outside the two selected candidates. The confirmed
batch contains six nonselected, completed deterministic policy outcomes with
delivery- and revision-bound `NEEDS_VERIFICATION` decisions.

v1.0.8 accepts only that exact shape. Each such attempt must be exactly one,
completed with no error, associated with a valid persisted
`NEEDS_VERIFICATION` result from the PARTIAL-verification trusted-policy path.
Any failed, repeated, started, unknown or inconsistent nonselected attempt
remains `operator_required`. The selected candidates still require one
Fidelity-passed `READY_FOR_REVIEW` decision and one failed
`editorial_gateway_attestation_invalid` attempt with no decision.

The recovery keeps all nine decisions and all attempts as history. It creates
no PublicationIntent, Site submission or ACK and never calls an LLM.

## Install on `claw2`

Stop only the existing Discovery LaunchAgent. Preserve all existing state
directories, SQLite/WAL files, Bridge ledgers, inboxes, receipts, intents and
exports.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.8
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

Do not change OpenClaw, `openrouter/openrouter/free`, the Website, Publisher,
Scheduler, Telegram or Cloudflare configuration.

## One terminal recovery attempt

First inspect the exact Bridge receipt read-only. Proceed only if it remains
the matching expired pending receipt for the stated delivery. With the existing
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

Success is exactly `partial_editorial_batch_abandoned`. Then inspect the Bridge
receipt again and require `dismissed` with no pending delivery. Any other
result, including `operator_required`, means stop and leave state unchanged.
Do not replay Gateway work, acknowledge or dismiss manually, modify SQLite/WAL
or ledgers, or start Publisher or Scheduler during recovery.
