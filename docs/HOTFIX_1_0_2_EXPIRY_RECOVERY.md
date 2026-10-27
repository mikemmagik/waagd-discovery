# WAAGD Discovery v1.0.2 expiry-recovery hotfix

## Fixed path

`v1.0.1` introduced the narrow `gateway-stage` protocol, but its Gateway batch
preflight called `validate_input()` before `Controller.process()`. An expired
envelope was therefore rejected before the Controller could run its existing
state-2 `quarantine_expired` procedure.

`v1.0.2` changes only the `gateway-stage` preflight: it still validates the
complete Bridge envelope, protected-delivery rule, future timestamp,
duplicate-candidate rule and candidate limit, but permits expiry to reach the
Controller. The Controller then validates Bridge authority and payload digest,
proves the absence of Gateway attempts, ACKs, PublicationIntents and Site
submissions, and only then asks Bridge to quarantine the expired delivery.

The ordinary `gateway-process` path remains expiry-rejecting. No expired path
may invoke OpenClaw, publish, ACK, retry or replay work. A missing or
inconsistent proof remains `operator_required`.

## Update existing `claw2` runtime

Disable the existing production LaunchAgent first. Preserve every existing
state path and never delete or reset its SQLite/WAL files, Bridge ledger, inbox,
intent, receipt or export.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.2
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

Keep the v1.0.1 stage adapter substitutions in the fixed wrapper:

```text
waagd-bridge-stage ... --schema-version 2
waagd-editorial ... gateway-stage
```

All existing absolute paths, the 600-second Gateway deadline,
`openrouter/openrouter/free` configuration, Site arguments and token boundary
remain unchanged. Validate the wrapper offline and re-enable only the existing
LaunchAgent; do not install a second scheduler or publisher.

## Delivery `e3ebd328-b49f-4927-a394-1c1671e67d05`

First inspect the authoritative Bridge receipt read-only, using the command in
`HOTFIX_1_0_1_STAGE_CONTRACT.md`. Do not run `sync`, `ack` or `dismiss` for this
historical delivery.

If the receipt is still pending and unexpired, use the v1.0.1 editorial-only
continuation: a temporary copy of the fixed cycle wrapper with its Bridge stage
pair removed. It processes the current inbox once without another Bridge sync.

If the receipt is expired, use that same editorial-only continuation once with
the v1.0.2 `gateway-stage` command. It cannot start OpenClaw for an expired
inbox. It completes only when the existing Controller safely quarantines the
proven-unused delivery; otherwise it returns `operator_required` and leaves all
state unchanged for reconciliation.
