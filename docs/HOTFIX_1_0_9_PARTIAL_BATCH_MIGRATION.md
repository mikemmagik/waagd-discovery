# WAAGD Discovery v1.0.9 partial-batch migration recovery

## Fix

The v1.0.8 partial-batch command opened Editorial Store read-only before its
normal v5-to-v6 migration. A real schema-v5 Store correctly rejected that open
with `editorial_schema_migration_required`.

v1.0.9 first performs only a shared-lock, schema-v5-compatible proof of the
persisted envelope, exact expired Bridge receipt and the absence of downstream
effects. It then opens the ordinary writable Store, which migrates v5 to v6
atomically, rechecks the receipt and repeats the complete recovery proof before
the terminal transition. No model, publisher or ACK path is reachable.

## Install on `claw2`

Do not run recovery again on the currently blocked runtime. Stop only the
existing Discovery LaunchAgent and preserve the existing recovery marker,
SQLite/WAL files, Bridge ledger, inbox, receipt, decisions, attempts, intents
and exports.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.9
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

Keep `openrouter/openrouter/free`, OpenClaw, Website, Publisher, Scheduler,
Telegram and Cloudflare configuration unchanged.

## Separate one-time recovery procedure

This is for a later operator-approved maintenance window, not for an immediate
replay. Retain the old marker. Before any migration, create a distinct,
owner-only marker outside the production state directories and take a SQLite
online backup while the Discovery runtime is stopped. Abort if that new marker
already exists.

```sh
umask 077
mkdir -p /absolute/waagd-production-maintenance/v1.0.9
test ! -e /absolute/waagd-production-maintenance/v1.0.9/f178-recovery.started
: > /absolute/waagd-production-maintenance/v1.0.9/f178-recovery.started
sqlite3 /absolute/waagd-production-state/editorial/editorial.sqlite3 \
  ".backup '/absolute/waagd-production-maintenance/v1.0.9/editorial-before-recovery.sqlite3'"
```

Read the exact Bridge receipt first. Proceed only if it is the matching expired
pending receipt. Then run this command once:

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

Success is exactly `partial_editorial_batch_abandoned`, Editorial schema 6 and
a Bridge receipt of `dismissed` with no pending delivery. Any other result,
including `operator_required`, means stop and preserve the marker and backup.
Never replay Gateway work, acknowledge or dismiss manually, alter SQLite/WAL
or ledgers, or start Publisher or Scheduler during this procedure.
