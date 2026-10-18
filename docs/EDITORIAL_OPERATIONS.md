# Sprint 5 RC operator guide

This guide is a review and isolated installation procedure for `v0.5.0-rc.1`.
Deployment on the working Intel MacBook is a separate operator decision. Nothing
here starts a service, changes OpenClaw or enables real editorial processing.
The stable Discovery release remains v0.4.1. Use a separate checkout, private
environment and test paths; do not replace the running checkout or its image tag.

## Preserve the working installation

Before any subsequently authorized rollout, record the current Git SHA, Docker
image ID, Compose configuration, mount names, schema version and read-only health
report. Do not print environment variables, credentials or article contents into
the report. Keep an off-device copy of verified backups and record their checksums.

Use the existing verified Discovery backup procedure in [BACKUPS.md](BACKUPS.md).
If copying a volume instead, stop every writer and copy the complete volume,
including any SQLite journal, without deleting the original. Verify a copy using
the matching application version. Never run benchmarks or smoke tests on it.

For Bridge, stop all cooperating Bridge processes and copy the **whole private
state directory**, including ledger, initialization marker and lock. Preserve
owner-only modes, verify the copy and retain the original. A copied inbox or export
does not replace the private history. The real pending delivery
`dbf6d7a2-395e-40c6-8e91-7866aab89e53` must remain pending: do not process, ACK,
dismiss, reset, migrate around it or rewrite its snapshot in this sprint.

Editorial Store is new and separate. Back it up with all controllers stopped by
copying its complete private directory (database, marker, lock and any journal).
Verify the copy read-only with the same version and matching simulation mode.
Keep coherent Bridge/Editorial backup timestamps and receipts: restoring an old
copy can otherwise reintroduce delivery history already acknowledged elsewhere.
Never reset a corrupt store or replace only its marker. No automated Editorial
backup/retention service is installed by this RC.

## Verify and install an isolated review checkout

Verify the release tag, commit SHA, CI result and downloaded asset hashes against
the release report. Use a new directory, not the running v0.4.1 checkout. The
following commands are for that new checkout, with Python 3.13+ and `uv` installed:

```sh
git clone --branch v0.5.0-rc.1 --single-branch https://github.com/mikemmagik/waagd-discovery.git waagd-editorial-review
cd waagd-editorial-review
git rev-parse HEAD
UV_PROJECT_ENVIRONMENT=.editorial-venv uv sync --frozen
.editorial-venv/bin/waagd-editorial --help
```

Compare HEAD to the published report before proceeding. Do not reuse the Bridge
production environment or change its installed package. No OpenClaw account,
model key, agent file or actual workspace is needed.

For container acceptance on Intel, build a **unique test image** and use disposable
fixture volumes created by the harness. The harness refuses no production path
because it never accepts one; do not modify it to add production mounts.

```sh
docker build --platform linux/amd64 -t waagd-discovery:sprint5-review-amd64 .
WAAGD_SMOKE_IMAGE=waagd-discovery:sprint5-review-amd64 WAAGD_SMOKE_SKIP_BUILD=1 DOCKER_DEFAULT_PLATFORM=linux/amd64 python3 scripts/docker-editorial-smoke.py
```

The smoke creates a fake workspace, simulated stories and separate private
stores, tests crash recovery and removes its own containers/volumes. Its injected
test-only ACK authorization does not enable real ACK in the CLI. For full image
regression, run the configuration, persistence, scheduler and Bridge harnesses
with the same test image as documented in CI; do not use production Compose
volumes or the `waagd-discovery:local` tag.

## Configuration and manual mock commands

All controls default to false. An optional strict YAML file has these fields:

```yaml
editorial:
  enabled: false
network:
  enabled: false
llm:
  enabled: false
bridge:
  auto_ack: false
publishing:
  enabled: false
```

No additional embedded configuration or default service is installed. To persist
mock assessments in an isolated fixture only, change `editorial.enabled` to true
in a **test copy** of this file. Leave all other controls false. Unknown fields,
duplicate YAML keys and non-boolean controls are rejected. Enabling LLM, automatic
ACK or publication fails closed. Network-enabled manual processing is unavailable
in this mock-only RC; no setting activates a real editor.

The following paths are placeholders that the operator must replace with absolute
paths in a newly created synthetic review fixture. The harness above is the
repeatable fixture generator; never point these commands at the real workspace.
The fixture must already contain a valid Bridge-generated pending delivery and
its matching private state. Do not hand-edit or forge an inbox to test authority.

```sh
.editorial-venv/bin/waagd-editorial \
  --inbox /ABSOLUTE/REVIEW/workspace/waagd-discovery/inbox.json \
  --store /ABSOLUTE/REVIEW/editorial/editorial.sqlite3 \
  --export /ABSOLUTE/REVIEW/exports/candidates.json \
  --workspace-root /ABSOLUTE/REVIEW/workspace \
  --state-dir /ABSOLUTE/REVIEW/bridge-private dry-run
```

Dry-run needs no enabled configuration, uses a temporary simulation store, does
not persist results in the requested store and never fetches HTTP or ACKs. It
still validates identity, expiry and authoritative Bridge receipt. An expired,
protected or forged delivery is not a usable dry-run fixture.

Use the same explicit paths with `--config /ABSOLUTE/REVIEW/editorial.yaml process`
instead of `dry-run` for one persisted mock assessment. Options precede the
subcommand. Repeat `process` to prove saved results are reused; `status` reads
diagnostics without creating or modifying a store. `reconsider --candidate-key
KEY` explicitly adds a decision revision while preserving history. It cannot
extend expired authorization. Simulation records cannot be promoted into real
records or mixed with a non-simulation store.

The RC CLI deliberately refuses `--allow-ack` and non-simulation processing.
Read-only `--no-simulation status` is supported for a matching store. Real ACK is
exercised only by injected fake-data tests, not an operator rollout command.

## Protocol migration and compatibility

Discovery SQLite remains schema 4, with no migration or rewrite. Existing export
schema 2 and v1 Bridge warning remain supported. V1 never fetches article URLs.
Selecting Bridge `--schema-version 2` only prepares new v2 deliveries; a pending
v1 delivery replays the exact old UUID and bytes. Switching modes cannot skip the
single pending slot.

Private Bridge state 2 is a separate, explicit forward migration required for
v2 and conditional receipt-based ACK. On a **fresh fake fixture**, the public
Bridge API/CLI can initialize v2 or migrate an idle state with `migrate-state`.
Use `waagd-bridge --help` for global path arguments and the contract tests as an
executable fixture. Migration refuses **any** pending delivery, preserves URL
history and creates a verified owner-only pre-migration ledger copy before
committing. That single file does not replace the full coherent backup above.
There is no permission to migrate the working Mac's pending state in Sprint 5.

After separately approved future rollout, inspect status and pending history
before considering migration. Existing terminal records without receipts cannot
be treated as proof of a matching completed payload. Operator reconciliation is
required for expired deliveries, missing state, digest conflicts, a previously
ACKed batch without Editorial history or malformed durable results. The RC never
silently regenerates those results, automatically dismisses expiry or resets
deduplication. Preserve evidence and stop processing the affected delivery.

## Rollback

Stop any newly authorized Editorial invocation first; this RC creates no permanent
process to uninstall. Preserve its entire store and logs. Revert the separate test
checkout/environment or remove only its disposable fixture after retaining needed
evidence. The running v0.4.1 installation needs no change for this review.

For a future Discovery rollback, retain the recorded v0.4.1 image and existing
schema-4 volume; stop the new writer before restarting the old one. Never delete
volumes or use `docker compose down -v`. Test evidence proves v0.4.1 can reopen
the RC's Discovery data and original history remains present.

**Bridge state 2 is not readable by v0.4.1 Bridge.** Keep the newer Bridge stopped
or retain its compatible version while rolling Discovery back independently.
Do not downgrade by deleting state or blindly restoring an old ledger: that can
redeliver acknowledged items. A complete pre-migration backup may be restored
only after an explicit operator reconciliation of all subsequent deliveries and
Editorial receipts. Editorial schema 1 is separate; older Discovery ignores it.
Unknown future/negative/corrupt schemas fail closed rather than downgrade.

## Future real integration gate

No real OpenClaw adapter is implemented. The planned supported surface is the
[Gateway-backed agent CLI](https://docs.openclaw.ai/cli/agent), using the existing
`waagd` agent and structured output, after operator validation against the installed
version. Do not use session files, an undocumented API, `--local`, delivery to a
messaging channel or a new agent. Existing agent AGENTS.md/SOUL.md remain unchanged.

A later approval must establish actual per-invocation tool/process isolation,
model limits and timeouts, candidate budgets, permissions and human review. The
current agent's workspaceOnly/read/web tools are insufficient evidence of an OS
sandbox. A same-user Python process can access same-user private files; private
paths and fixed JSON policy are not a hostile-code sandbox. Trusted API integrators
must explicitly supply every Discovery data, export, Bridge-state and workspace
root in `protected_roots`; the abstract Bridge port intentionally exposes no host
paths. Do not provide these paths, credentials, Docker sockets or database handles
to a future editor process. Real source verification, semantic corroboration,
real ACK and model calls remain separate operator decisions.
