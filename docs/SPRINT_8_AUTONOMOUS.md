# Sprint 8 autonomous daily integration

Sprint 8 is an owner-authorized development addition to v0.7.0. It does not
change historical Sprint 5–7 evidence and does not authorize a deployment,
production publication, Gateway change, or access to the operator's existing
v0.4.1 Discovery state.

The Site importer contract is pinned to `mikemmagik/weareallgonnadie`
`ffa34ce097fbdb34161bdd3b9782f798d1dc7936`. It is not an ancestor of Site
`main` at the time of this document. An operator must install that exact reviewed
Site revision (or a reviewed descendant) before a canary.

## Runtime shape

`waagd-autonomous` is a small private SQLite calendar journal. A user LaunchAgent
wakes it every five minutes; it decides whether one configured local-time cycle
is due. It holds a singleton flock, records the cycle before invoking anything,
allows three bounded retries, and never replays a completed or
`operator_required` local date. On boot/wake it runs one due cycle at most; it
does not drain historical backlog. Asia/Bangkok and `08:00` are the defaults.

The configured `waagd-autonomous-cycle` process runs fixed absolute upstream
commands (normally Bridge sync then `waagd-editorial gateway-process`) in a
sanitized environment. It subsequently invokes the Site importer in a separate
process. The Site environment file and ingress-token file are never passed to
the Gateway/editorial command.

`--gateway-timeout-seconds` defaults to `120` and accepts only finite values
from `5` through `600`. The staging example uses `360` to give a slow but live
Gateway turn a longer parent deadline; its subordinate OpenClaw timeout remains
two seconds shorter. This does not guarantee completion if OpenClaw or its
provider applies an independent limit. A timeout or nonzero child exit remains
an ambiguous, operator-required result: it is not retried automatically and
cannot produce an ACK or publication.

Publication jobs live only in the independent Editorial Store, schema 4. Each
job stores the exact immutable intent bytes, identity and content hash before the
intent file is exposed. `created` and `duplicate` are terminal delivery receipts.
`uncertain` is operator-required and is never resent. `held` retains the same
intent for a later Site resume; it does not bypass a quota or pause. `failed` is
also held for classification rather than retried automatically.

## One-time staging installation

These commands are a template for an isolated staging user. Substitute only
absolute service-user paths. Keep every state/config directory `0700` and every
file below `0600`; none belongs in a Git checkout.

1. Create separate roots for the Editorial Store, autonomous journal, export
   file, Bridge state/workspace, and Site `WAAGD_STATE_DIR`. Preserve a verified
   backup of the existing v0.4.1 Discovery volume and exact Bridge bytes; do not
   mount that volume in the new worktree.
2. Configure the existing collector to export candidate-v2 data into a new,
   private path. The existing v0.4.1 scheduler remains the sole collector.
3. Build two fixed absolute commands: a Bridge `sync` command and an editorial
   `gateway-process` command. The latter receives only its editorial config,
   Bridge paths, trusted-publisher/corroboration policy and Gateway allowlist.
4. Create the cycle command, with the Site credentials confined to the final
   importer process:

```sh
/absolute/venv/bin/waagd-autonomous-cycle \
  --stage-command '/absolute/venv/bin/waagd-bridge --export /absolute/export/candidates.json --workspace-root /absolute/bridge-workspace --state-dir /absolute/bridge-state sync' \
  --stage-command '/absolute/venv/bin/waagd-editorial --config /absolute/private/editorial.yaml --inbox /absolute/bridge-workspace/waagd-discovery/inbox.json --store /absolute/editorial/editorial.sqlite3 --export /absolute/export/candidates.json --workspace-root /absolute/bridge-workspace --state-dir /absolute/bridge-state --no-simulation --allow-ack --allow-gateway --gateway-executable /absolute/openclaw --gateway-timeout-seconds 360 --gateway-max-candidates 5 --gateway-max-cost-usd 0.10 --trusted-publishers /absolute/private/trusted_publishers.yaml gateway-process' \
  --editorial-store /absolute/editorial/editorial.sqlite3 \
  --publication-output /absolute/publication-export/publication-intent.json \
  --publication-policy /absolute/private/publication.yaml \
  --site-executable /absolute/node \
  --site-argument=--env-file=/absolute/private/waagd.env \
  --site-argument=/absolute/site/automation/cli.ts \
  --site-working-directory /absolute/site \
  --ingress-token-file /absolute/private/discovery-ingress.token
```

5. Install the calendar journal disabled, then load the LaunchAgent and enable
   only after the staging canary has verified Site receipt, audit and rendering:

```sh
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/autonomous \
  --timezone Asia/Bangkok --daily-time 08:00 \
  --cycle-command '/absolute/bin/run-waagd-autonomous-cycle' install

launchctl bootstrap gui/"$(id -u)" /absolute/Library/LaunchAgents/com.waagd.autonomous.plist
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/autonomous enable
```

Create `/absolute/bin/run-waagd-autonomous-cycle` as a service-user-owned `0700`
wrapper which executes the fixed cycle command above and prints only its bounded
JSON. It must not source an interactive profile or add Site credentials to the
editorial environment. Then create this user-owned LaunchAgent at
`/absolute/Library/LaunchAgents/com.waagd.autonomous.plist`, replacing each
`/absolute/...` value before loading it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.waagd.autonomous</string>
  <key>ProgramArguments</key><array>
    <string>/absolute/venv/bin/waagd-autonomous</string>
    <string>--state-dir</string><string>/absolute/autonomous</string>
    <string>--timezone</string><string>Asia/Bangkok</string>
    <string>--daily-time</string><string>08:00</string>
    <string>--cycle-command</string><string>/absolute/bin/run-waagd-autonomous-cycle</string>
    <string>run-due</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>/absolute/autonomous/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/absolute/autonomous/launchd.err.log</string>
</dict></plist>
```

Use a user LaunchAgent with `RunAtLoad`, `StartInterval` 300, and absolute
`ProgramArguments` invoking `waagd-autonomous ... run-due`. The periodic wake is
not a repeated editorial run: the durable journal permits at most one due cycle
per local date. It requires an active logged-in macOS user and Docker Desktop
available after login; it cannot run while the Mac sleeps or is powered off.

## Operations and staging canary

`status`, `pause`, `resume`, `disable`, and `uninstall` are exposed through
`waagd-autonomous`; uninstall only disables and retains the journal. The Site
kill switch remains independent:

```sh
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/autonomous status
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/autonomous pause
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/autonomous resume
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/autonomous disable
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/autonomous uninstall
```

For the single staging canary, leave autonomous scheduling disabled and Site auto
publication paused. Use fresh approved source material and a five-item Site quota
with only one selected candidate. Validate the created/duplicate receipt, Site
state/audit and rendered staging post. Then restore the Site pause. A separate
operator decision is required to resume Site auto publication and enable the
LaunchAgent.

Rollback pauses the Discovery journal and Site auto policy, unloads the
LaunchAgent, preserves the Editorial/auto/Site state directories, and returns to
the backed-up independent staging checkout. Never reset an SQLite journal or
replay an uncertain intent.

```sh
/absolute/venv/bin/waagd-autonomous --state-dir /absolute/autonomous pause
launchctl bootout gui/"$(id -u)" /absolute/Library/LaunchAgents/com.waagd.autonomous.plist
# Keep all SQLite journals and immutable intent files for receipt reconciliation.
```
