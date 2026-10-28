# WAAGD Discovery v1.0.4 verification-history recovery hotfix

## Fixed gate

`v1.0.3` required the entire `attempts` history for an ambiguous selection to
contain exactly one row. Real Editorial processing records one completed Source
Verification attempt per candidate before selection, so a safely expired
delivery with that normal history was incorrectly held at `operator_required`.

`v1.0.4` accepts only completed, delivery-bound `verification` attempts (and
the known terminal `source_verification_failed` form). It still requires exactly
one `selection` attempt with `candidate_key=NULL`, `status=failed`, and
`error_code=selection_outcome_unknown`. Any extra selection attempt, unfinished
or unknown verification, editorial/Gateway/Fidelity/export attempt, decision,
ACK, PublicationIntent, Site job, digest mismatch, receipt mismatch, or
unexpired delivery remains fail-closed.

The v1.0.3 two-phase protocol is unchanged: it records
`selection_abandon_pending`, uses Bridge's exact expired-receipt action, then
records `selection_abandoned`. It never changes the verification attempts, the
failed selection attempt, or selection run; it does not invoke Gateway, LLM,
selection retry, publication, ACK or Bridge sync.

## Update `claw2`

Stop only the existing LaunchAgent. Preserve all existing SQLite/WAL files,
Bridge ledger, inbox, receipts, intents and exports.

```sh
cd /absolute/waagd-production
git fetch --tags origin
git checkout v1.0.4
UV_PROJECT_ENVIRONMENT=/absolute/waagd-production/runtime uv sync --frozen --no-dev
/absolute/waagd-production/runtime/bin/waagd-editorial --help
```

Do not change the existing wrapper, Docker Discovery Scheduler, 600-second
Gateway deadline, `openrouter/openrouter/free` route, Site arguments, or token
boundary. Do not start a second Scheduler or Publisher.

## One controlled recovery attempt

With the existing runtime stopped, inspect the authoritative receipt first:

```sh
/absolute/waagd-production/runtime/bin/waagd-bridge \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --schema-version 2 inspect-delivery \
  --delivery-id 583145d8-ef81-41d1-8ce4-3214b16ca2ad
```

Proceed only when it reports that exact delivery as `pending` and expired. Run
this command once, retaining the exact production paths:

```sh
/absolute/waagd-production/runtime/bin/waagd-editorial \
  --inbox /absolute/waagd-production-state/bridge-workspace/waagd-discovery/inbox.json \
  --store /absolute/waagd-production-state/editorial/editorial.sqlite3 \
  --export /absolute/discovery-export/candidates.json \
  --workspace-root /absolute/waagd-production-state/bridge-workspace \
  --state-dir /absolute/waagd-production-state/bridge-state \
  --no-simulation abandon-ambiguous-selection \
  --delivery-id 583145d8-ef81-41d1-8ce4-3214b16ca2ad \
  --operator-confirm
```

Success requires `status=selection_abandoned` and
`error_code=selection_outcome_unknown_abandoned`. Then repeat the same
read-only `inspect-delivery`: the receipt must be `dismissed`, with the same
delivery id, digest, schema version and expiry; Bridge must have no pending
delivery. ACK remains `not_ready`; no PublicationIntent, Site submission or
LLM call is created.

For any other result, including `operator_required`, stop and preserve state.
Do not retry selection, run Bridge sync, ACK, dismiss manually, edit state, or
run a Site Publisher. Re-enable only the existing LaunchAgent after the terminal
result is independently checked.
