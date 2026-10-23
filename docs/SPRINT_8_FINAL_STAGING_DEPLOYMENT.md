# Sprint 8 final staging-only daily deployment

This package is for the `claw2` user's Intel Mac staging environment only. It
does not alter Docker Discovery, the main OpenClaw installation, Telegram,
Cloudflare, DNS, production, or the completed canary's data. The package is
pinned to Discovery `bbc710c5c94761ac9b24a41aef4b7e52f5ca03a7` and Site
`ffa34ce097fbdb34161bdd3b9782f798d1dc7936`.

The checked-in artifacts are [the private JSON template](../deployment/staging/waagd-staging.json.template)
and [the LaunchAgent template](../deployment/staging/com.waagd.staging-autonomous.plist.template).
Copy them outside Git. The JSON is data, not shell source: the trusted
`waagd-staging` wrapper accepts only its exact schema, absolute paths, and the
one staging origin. It makes a validated, immutable source-export snapshot per
daily cycle, then runs Bridge and editorial through separate minimal
environments. The OpenClaw process receives only its dedicated HOME,
`OPENCLAW_CONFIG_PATH`, `OPENCLAW_STATE_DIR`, and system PATH. The Site process
alone receives the Site environment-file and ingress-token paths.

Every private directory must be `0700`, every config/token/environment file
`0600`, and every executable root-owned or `claw2`-owned and non-group-writable.
The Site environment file must name a new `WAAGD_STATE_DIR`; do not point it at
the completed canary state. Its Site revision must still be the pinned commit.

## Installation and preflight

After copying and replacing only `/absolute/...` paths in both templates, save
the JSON as `/absolute/private/waagd-staging/waagd-staging.json` and the plist
as `/absolute/Library/LaunchAgents/com.waagd.staging-autonomous.plist`.

Run this one consolidated command as `claw2`; it initializes state disabled,
validates the private configuration, and validates the plist without loading it:

```sh
/absolute/venv/bin/waagd-staging --config /absolute/private/waagd-staging/waagd-staging.json status && \
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/private/waagd-staging/autonomous --timezone Asia/Bangkok --daily-time 08:00 --cycle-timeout-seconds 15900 --inner-cycle-timeout-seconds 15600 --cycle-command '/absolute/venv/bin/waagd-staging --config /absolute/private/waagd-staging/waagd-staging.json cycle' install && \
plutil -lint /absolute/Library/LaunchAgents/com.waagd.staging-autonomous.plist
```

It neither loads nor enables the LaunchAgent. `status` reports `site_active:
false` until the separate activation command below. No secrets, prompts, raw
articles, or model output are printed by these commands.

## Controlled acceptance

With Site publication still paused, run one controlled, synthetic acceptance
cycle (use a fresh private test export rather than the finished canary export):

```sh
/absolute/venv/bin/waagd-staging --config /absolute/private/waagd-staging/waagd-staging.json cycle --cycle-id 11111111-1111-4111-8111-111111111111 --local-date 2099-01-01
```

It validates and snapshots the export but supplies `--site-paused`; no Site
import is possible. Inspect the private snapshot, Bridge ledger, Editorial
Store, and autonomous state. Confirm a malformed Gateway result, expired Bridge
delivery, interrupted gateway/site invocation, or receipt mismatch is retained
as `operator_required` and never re-run automatically. Do not use the canary's
source, intent, state, or identity.

## Explicit activation and daily scheduling

Only after the controlled acceptance and independent staging receipt/audit
review, activate Site delivery and load the disabled LaunchAgent:

```sh
/absolute/venv/bin/waagd-staging --config /absolute/private/waagd-staging/waagd-staging.json activate && \
launchctl bootstrap gui/"$(id -u)" /absolute/Library/LaunchAgents/com.waagd.staging-autonomous.plist && \
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/private/waagd-staging/autonomous enable
```

The journal uses `Asia/Bangkok` internally, so macOS's system timezone does not
change the logical date. The five-minute LaunchAgent wake is a wake check, not a
new cycle: a durable singleton journal permits one completed or
operator-required local date only. Its five-item delivery limit is therefore
preserved over service restarts and duplicate wakes. A retryable pre-execution
failure is bounded by the journal; any unknown Gateway or Site work is
operator-required.

## Operations, pause, and rollback

```sh
# Status or immediate publication stop
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/private/waagd-staging/autonomous status
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/private/waagd-staging/autonomous pause
/absolute/venv/bin/waagd-staging --config /absolute/private/waagd-staging/waagd-staging.json deactivate

# Resume the calendar only; Site stays paused until `activate` is run again.
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/private/waagd-staging/autonomous resume

# Disable/unload without deleting evidence.
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/private/waagd-staging/autonomous disable
launchctl bootout gui/"$(id -u)" /absolute/Library/LaunchAgents/com.waagd.staging-autonomous.plist
```

Never delete or reset the autonomous journal, snapshots, Bridge ledger,
Editorial Store, Site state, or immutable intents. Retain them for receipt
reconciliation. `uninstall` only disables the journal; it intentionally keeps
all history. Logs remain in the private autonomous directory and should be
rotated manually by the operator after copying required diagnostics.

Limitations: this repository cannot inspect the operator's existing Docker
export, secrets, OpenClaw state, Site state, or launchd session. The staging
Site commit pinned for this package emits an exact `{"outcome": ...}` import
response. Its reviewed import implementation enforces the staging origin and
private-file boundary; Discovery validates that exact response and retains all
non-success outcomes as uncertain. It does not provide an identity-bearing
receipt ID, so cross-system receipt correlation remains the Site state/audit
review step. A future reviewed Site receipt contract may use
`StagingReceiptOutcomeParser`, which requires matching intent, origin, state,
and receipt IDs. The wrapper also cannot inspect the secret Site environment
file to prove its configured state directory is a new non-canary directory;
the operator must verify that during acceptance. Do not invoke
`waagd-staging cycle` repeatedly outside `waagd-autonomous run-due`: the durable
five-item/day guarantee is enforced by the calendar journal's one logical cycle
per Bangkok date, not by a standalone manual cycle command. Docker and real
Gateway/Site acceptance remain operator-run.
