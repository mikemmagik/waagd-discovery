# WAAGD Discovery v1.0.0 production release

## Release boundary

`v1.0.0` is the owner-authorized production release of the accepted Discovery,
verification, selection, editorial, Fidelity, publication and acknowledgement
chain. The final staging acceptance completed one delivery, Site publication and
Bridge ACK on 2026-10-26. This release does not deploy production, alter the
existing `claw2` installation, modify the Site repository, or change OpenClaw
or Telegram.

The only permitted Gateway request route is
`openrouter/openrouter/free`. The production CLI requires that exact requested
model in a detailed terminal receipt. OpenRouter may report the free route's
effective backing model separately; a paid or different requested route is
rejected before a decision, ACK or publication can be accepted.

The staging wrapper remains hard-bound to its staging origin. Do not copy,
rename or repurpose `waagd-staging` configuration for production. Production
uses the generic `waagd-autonomous-cycle` composition below with a separately
reviewed production Site importer.

## Required state separation

The existing Docker Discovery volume is the production collector's authoritative
history. Do not recreate it, run `docker compose down --volumes`, copy its SQLite
files while writers run, or mount it in the editorial runtime.

A production runtime needs new, owner-only (`0700`) directories outside the
checkout for each of the following:

- Bridge workspace and Bridge state/ledger;
- Editorial Store SQLite database and its WAL/SHM sidecars;
- autonomous coordinator journal and logs;
- immutable publication-intent output directory;
- OpenClaw `HOME`, config and state; and
- Site state and the Site environment/token files.

Staging and production must use different directories for every item above. Do
not import, replay, ACK, dismiss or otherwise migrate an unfinished staging
delivery, Editorial attempt, publication job, intent, Site receipt or Bridge
ledger into production.

The same Docker Discovery export may be read by production only as an immutable,
atomic read-only input: the collector remains its sole writer and production
copies it into its own Bridge flow. During cutover, pause the staging coordinator
before enabling production so a new candidate is not independently processed by
both runtimes. Sharing a source export never authorizes sharing any consumer
state.

## Configuration and secret contract

The Docker collector uses only these non-secret path settings (Compose supplies
them by default): `WAAGD_DB`, `WAAGD_SOURCES`, `WAAGD_FILTER`,
`WAAGD_BACKUP_POLICY`, `WAAGD_SCHEDULER_CONFIG`, and
`WAAGD_HANDOFF_CONFIG`.

The separate production editorial runtime requires private files or directories
for: the trusted-publisher/corroboration policy, publication policy, production
Bridge state, Editorial Store, OpenClaw config/state/home, Site environment file
and Site ingress-token file. Keep files `0600`; never commit them.

Secrets required by the deployment boundary are:

- the OpenRouter credential held only by the isolated OpenClaw configuration or
  its credential store, configured for `openrouter/openrouter/free` only;
- the production Site publication API credentials, held only in the Site-owned
  environment file; and
- the production ingress token file passed only to the Site import command.

Discovery does not define or read the Site application's internal environment
variable names. Infrastructure must use the reviewed Site deployment contract
for that file, without adding those values to the LaunchAgent, Gateway process,
Docker Compose environment, logs or this repository. Telegram credentials are
not part of this runtime.

The Gateway child receives an allowlisted environment only: `PATH`, `LANG`,
`LC_ALL`, `HOME`, `OPENCLAW_CONFIG_PATH`, `OPENCLAW_STATE_DIR`, and (only when
the isolated Gateway requires them) `OPENCLAW_GATEWAY_URL` and
`OPENCLAW_GATEWAY_TOKEN`. The Site process is a separate child and receives its
own Site environment-file argument and ingress-token file; neither reaches the
Gateway process.

## `claw2` installation procedure

Run as the dedicated `claw2` user. Use an isolated checkout and runtime; do not
replace the staging checkout or its environment.

```sh
git clone --branch v1.0.0 --single-branch https://github.com/mikemmagik/waagd-discovery.git /absolute/waagd-production
cd /absolute/waagd-production
git rev-parse HEAD
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
```

Confirm that `git rev-parse HEAD` is the published release commit before any
configuration is created. Copy no state from staging. Configure the existing
Docker Scheduler to continue writing its normal atomic export; do not create a
second collector or a second Docker volume.

Create a production-only wrapper owned by `claw2`, mode `0700`, which invokes
the fixed absolute command below. Substitute only production absolute paths and
the already-reviewed Site CLI path; do not add a shell profile or credentials.

```sh
/absolute/waagd-production/runtime/bin/waagd-autonomous-cycle \
  --stage-command '/absolute/waagd-production/runtime/bin/waagd-bridge --export /absolute/discovery-export/candidates.json --workspace-root /absolute/waagd-production-state/bridge-workspace --state-dir /absolute/waagd-production-state/bridge-state --schema-version 2 sync' \
  --stage-timeout-seconds 900 \
  --stage-command '/absolute/waagd-production/runtime/bin/waagd-editorial --config /absolute/waagd-production-private/editorial.yaml --inbox /absolute/waagd-production-state/bridge-workspace/waagd-discovery/inbox.json --store /absolute/waagd-production-state/editorial/editorial.sqlite3 --export /absolute/discovery-export/candidates.json --workspace-root /absolute/waagd-production-state/bridge-workspace --state-dir /absolute/waagd-production-state/bridge-state --no-simulation --allow-ack --allow-gateway --gateway-executable /absolute/openclaw-production/bin/openclaw --gateway-agent waagd --gateway-timeout-seconds 600 --gateway-max-candidates 5 --gateway-max-cost-usd 0.10 --trusted-publishers /absolute/waagd-production-private/trusted_publishers.yaml gateway-process' \
  --stage-timeout-seconds 10800 \
  --cycle-timeout-seconds 15600 \
  --editorial-store /absolute/waagd-production-state/editorial/editorial.sqlite3 \
  --publication-output /absolute/waagd-production-state/publication/publication-intent.json \
  --publication-policy /absolute/waagd-production-private/publication.yaml \
  --site-executable /absolute/site-production/bin/node \
  --site-argument=--env-file=/absolute/waagd-production-private/site.env \
  --site-argument=/absolute/site-production/automation/cli.ts \
  --site-working-directory /absolute/site-production \
  --ingress-token-file /absolute/waagd-production-private/discovery-ingress.token
```

`waagd-autonomous-cycle` accepts a Site success only when the Site command emits
the reviewed exact `{"outcome": ...}` protocol. A timeout, malformed output,
nonzero unknown result, missing receipt, or failed Gateway attestation remains
`operator_required`; it is never automatically resent.

Create a production LaunchAgent which executes:

```sh
/absolute/waagd-production/runtime/bin/waagd-autonomous \
  --state-dir /absolute/waagd-production-state/autonomous \
  --timezone Asia/Bangkok --daily-time 08:00 \
  --cycle-timeout-seconds 15900 --inner-cycle-timeout-seconds 15600 \
  --cycle-command '/absolute/waagd-production-private/run-autonomous-cycle' \
  run-due
```

Install the journal disabled first, validate the private configuration and
LaunchAgent with `plutil -lint`, then bootstrap it. Enable only after
Infrastructure has reviewed modes, distinct paths, the tool-free OpenClaw
attestation and the production Site receipt contract.

```sh
/absolute/waagd-production/runtime/bin/waagd-autonomous \
  --state-dir /absolute/waagd-production-state/autonomous \
  --timezone Asia/Bangkok --daily-time 08:00 \
  --cycle-timeout-seconds 15900 --inner-cycle-timeout-seconds 15600 \
  --cycle-command '/absolute/waagd-production-private/run-autonomous-cycle' install
```

## Cutover and rollback

1. Preserve verified Docker and private-state backups, including SQLite WAL/SHM,
   Bridge ledger, Editorial Store, coordinator journal, intents and Site receipts.
2. Pause the staging coordinator; do not alter its data or submit its pending work.
3. Verify the production runtime with offline fixtures and private path/mode
   checks only. Do not run a live LLM cycle as a release check.
4. Bootstrap the production LaunchAgent disabled, then enable it for the first
   scheduled 08:00 Asia/Bangkok cycle after Infrastructure approval.
5. Monitor bounded operational codes and Site receipts. A successful ACK proves
   only that production's own delivery completed; it does not resolve staging.

For rollback, pause/disable the production LaunchAgent and preserve every state
directory unchanged. Return only the production application checkout/runtime to
the prior reviewed release after recording its commit. Never downgrade an unknown
newer SQLite schema, delete a WAL/ledger/intent, reset a journal, or resend an
uncertain Gateway or Site operation. Resume requires explicit reconciliation of
the preserved production state.
