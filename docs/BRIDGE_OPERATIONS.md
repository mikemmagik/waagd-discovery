# Bridge operations and Intel Mac handoff

These are operator instructions, not development actions. The v0.3.0 scheduler
runs on an inaccessible, separate Intel Mac. Do not discover workspace paths,
contact that Mac, change agents/gateway/Telegram/Shmul/Cloudflare/production or
activate LLM/OpenRouter processing. Complete mock acceptance first.

## Update the existing container from v0.3.0

Use the existing checkout, Compose project name and named volume. Changing the
project name or checkout directory may select a different empty volume. Record
the current project/volume and commit/image before stopping. Do not run
`down --volumes`, `volume rm`, Docker prune, manually copy live SQLite or reset it.
Use a unique backup name and preserve an off-device copy.

```sh
docker compose ps -a
docker compose config --volumes
git rev-parse HEAD
docker image inspect waagd-discovery:local --format '{{.Id}}'
docker image tag waagd-discovery:local waagd-discovery:operator-v0.3.0
docker compose --profile scheduler stop scheduler
docker compose run --rm discovery backup --output /backups/before-v0.4-update-001.sqlite3
docker compose run --rm discovery verify
git status --short
git fetch --tags
git checkout --detach v0.4.0
docker compose build
docker compose run --rm discovery init-db
docker compose run --rm discovery verify
docker compose run --rm discovery stats
docker compose run --rm discovery integration-status
```

Resolve local edits deliberately before checkout; do not force-discard them.
Preserve the accepted source registry/policies and 30-minute scheduler interval.
Verify the new handoff configuration exists, is mounted read-only and remains
`enabled: false`. No new SQLite migration is required beyond existing schema 4.
Restart the accepted scheduler only after verification:

```sh
docker compose --profile scheduler up -d scheduler
docker compose ps -a
docker compose run --rm discovery health
docker compose run --rm discovery status
docker compose --profile scheduler logs --tail 100 scheduler
```

`up -d scheduler` recreates from the built image; plain `restart` retains the old
image. Scheduler health does not prove export freshness or integration readiness.

## Install the native bridge without starting a service

From the reviewed v0.4.0 checkout, with uv/Python 3.13+ installed:

```sh
UV_PROJECT_ENVIRONMENT="$PWD/.bridge-venv" uv sync --frozen --no-dev --python 3.13
./.bridge-venv/bin/waagd-bridge --help
```

The same lockfile pins existing dependencies. No API key, LLM dependency or
OpenClaw package is required. Keep the checkout/environment, exports, private
ledger and backups outside the real workspace. Do not expose them through agent
tools or mounts. The operator supplies the actual approved workspace path; this
document guesses none. Verify host export readability and private-root ownership.
Stop on permission errors; do not bypass checks with root or world permissions.
The export may belong to container UID 10001 if it is host-readable, regular,
single-linked and not group/world-writable. Workspace/state ownership must match
the host bridge user; state/inbox directories are private 0700 and files 0600.

## Offline fake-workspace acceptance

Run the isolated acceptance proof against the reviewed image:

```sh
WAAGD_SMOKE_IMAGE=waagd-discovery:local WAAGD_SMOKE_SKIP_BUILD=1 python3 scripts/docker-bridge-smoke.py
```

It creates random disposable volumes, runs real collection/filter/auto-export
with synthetic RSS and networking disabled, then invokes the installed bridge in
a separate restricted container/process. The bridge phase mounts only read-only
export plus private fake workspace/state: no DB, registry, backup or Docker socket.
It checks bounded delivery, repeat sync without rewrite, restart, missing-inbox
repair, wrong ack rejection, explicit fake ack and a disjoint next batch. Host
Docker orchestrates the test only; the deployed native bridge needs no Docker
access. The test does not establish real OpenClaw consumption or target permissions.
Other tests cover hostile inputs and interrupted publication. CI runs both arches.

For a native mock, use only current synthetic schema-v2 data from an isolated test
Discovery, with three explicitly chosen sibling roots:

```sh
mkdir -p work/bridge-mock/workspace work/bridge-mock/state work/bridge-mock/export
chmod 700 work/bridge-mock work/bridge-mock/workspace work/bridge-mock/state work/bridge-mock/export
```

Place the validated synthetic `candidates.json` in mock export. Do not copy a
working SQLite or secrets. Never edit timestamps to disguise stale data. Run:

```sh
./.bridge-venv/bin/waagd-bridge --export "$PWD/work/bridge-mock/export/candidates.json" --workspace-root "$PWD/work/bridge-mock/workspace" --state-dir "$PWD/work/bridge-mock/state" --limit 10 sync
./.bridge-venv/bin/waagd-bridge --export "$PWD/work/bridge-mock/export/candidates.json" --workspace-root "$PWD/work/bridge-mock/workspace" --state-dir "$PWD/work/bridge-mock/state" --limit 10 status
```

Inspect only the fixed mock inbox, repeat sync and confirm the same delivery UUID.
For the fake consumer test only, acknowledge its exact displayed UUID:

```sh
./.bridge-venv/bin/waagd-bridge --export "$PWD/work/bridge-mock/export/candidates.json" --workspace-root "$PWD/work/bridge-mock/workspace" --state-dir "$PWD/work/bridge-mock/state" ack --delivery-id UUID_FROM_MOCK_INBOX
```

Ack asserts all items were processed. Never acknowledge real data merely to clear
a queue. This release performs no real editorial processing.
For an unwanted/expired batch that was not processed, use the explicit
`dismiss --delivery-id UUID` command instead. Dismissed is a separate terminal
state; it preserves delivery deduplication and never claims editorial completion.

## Enable automatic export separately

After update and mock acceptance, the operator can edit trusted
`config/handoff.yaml` to `enabled: true`, preserving `/exports/candidates.json`
and an approved limit, normally 50. Restart for configuration, wait for a
successful/partial cycle and inspect:

```sh
docker compose --profile scheduler restart scheduler
docker compose run --rm discovery integration-status
docker compose run --rm discovery status
test -r exports/candidates.json
```

Check age/schema/count and host readability without widening permissions. If
handoff fails, committed discoveries remain; fix disk/permissions/configuration
and let a later cycle retry. Do not delete SQLite or substitute arbitrary JSON.

## Real integration is a separate operator check

Before real paths, confirm the exact allowed root, existing agent tool privileges,
private state location outside workspace/export, candidate/text limits and lack
of DB/Docker/socket/secrets exposure. This release does not teach an existing
agent the consumer protocol. Real LLM/OpenRouter processing remains disabled until
separately agreed.

Command shape: `waagd-bridge --export ABSOLUTE_EXPORT --workspace-root
OPERATOR_CONFIRMED_ROOT --state-dir PRIVATE_EXTERNAL_STATE --limit 10 sync`.
`status` inspects the bridge separately from Discovery health. Optional foreground
`watch --interval 60` polls with the same one-pending-batch rules; it never
acknowledges. No LaunchAgent, cron, login item or persistent bridge is installed
by this sprint. Target activation belongs to the separate operator handoff.

## Ledger backup and recovery

Stop watch before maintenance. Copy the whole private state directory coherently
while no bridge process runs, keep owner-only access and an off-device copy, and
never commit it or place it in the workspace/export directory. It includes exact
prepared payloads and processed canonical URL identities.
Include `initialized.json`, the ledger and `bridge.lock`; the initialization
marker prevents treating a vanished ledger as a fresh installation. Do not copy
only the ledger or remove the marker to bypass a missing-state error.

Discovery's separate private `discovery.sqlite3.handoff.json` diagnostic sidecar
records automatic-export status/last success. It stays beside SQLite, outside
all bridge/consumer mounts, and is not a delivery ledger or article database.
The SQLite backup command backs up the database; the external bridge ledger
needs its own coherent stopped-process backup as described above.

After ordinary interruption, rerun sync: PREPARE/publish and ACK/publish recovery
uses trusted ledger state. Missing/tampered inboxes can be repaired; missing or
corrupt ledger history cannot be guessed safely from an inbox. Stop on corruption,
preserve evidence and restore a verified coherent ledger backup while stopped.
A stale backup may omit processed identities; reconcile with the consumer before
resuming. Never reset/delete the ledger to clear an error. Capacity exhaustion
needs explicit reconciliation, not automatic history pruning.

Expiry, zero candidates, missing inbox and process restart do not prove processing.
Resolve expired pending batches explicitly with the trusted UUID and consumer
state. If work preceded an interrupted ack, dedupe candidate keys before retrying.
Publication is at least once with stable IDs; external execution is not exactly once.

## Disable or roll back

Stop bridge/watch, preserve its ledger, set handoff `enabled: false` and restart
scheduler. Preserve pending inbox/consumer state for reconciliation; deletion is
not acknowledgement. Both v0.4 and v0.3 use SQLite schema 4. For rollback stop
scheduler, make a new verified uniquely named backup, record state, check out
v0.3.0, build, verify/stats and explicitly start its scheduler. Preserve the v0.4
ledger/inbox offline; v0.3 does not consume them. Code rollback normally needs no
restore for this same-schema upgrade. If restore is required, preserve current
data first and use the target release's restore command with the verified backup.
No step authorizes Sprint 5 or publication.
