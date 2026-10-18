# v0.4.1 independent QA review

Review date: 2026-10-18 (Asia/Bangkok). Scope: the minimal embedded Docker
configuration permission hotfix. Review used the independent
`test/hotfix-v041-qa` worktree and synthetic/disposable local fixtures only.
No target Intel Mac, real OpenClaw workspace, existing agent or production
service was accessed.

## Decision and reviewed changes

**Source changes approved before integration** at infrastructure commit
`263f9cc28542768998f2b7fad2dbe3e20ba3ff4f`. No blocking finding remains in the
reviewed Dockerfile, CI or regression harness. The actual release remains gated
on the lead's final exact-commit CI and complete Docker acceptance results.

The Dockerfile explicitly sets `/app/config` to root:root / 0755 and all five
embedded YAML files to root:10001 / 0440. The runtime identity remains
10001:10001. Root ownership prevents the application from changing trusted file
permissions; group read permission permits all five actual policy readers.
No world-writable permission, root application execution, Bridge protocol,
SQLite migration, dependency change or weakened Compose restriction is present.
Version metadata changes from 0.4.0 to 0.4.1 do not change runtime dependencies.

The CI Docker jobs build both architectures from an isolated context whose five
source policies are 0600. The permission regression and legacy persistence,
scheduler and Bridge smoke tests then use that exact built image. The harness
copies an explicit runtime-input allowlist and never changes the operator's
checkout permissions.

## Independent reproduction and fixed-image checks

The original defect was independently reproduced using a derived accepted
v0.4.0 ARM64 image with the original COPY semantics and five 0600 source files.
All five embedded files became root:root / 0600 and were unreadable as the
normal application UID/GID 10001. This was a faithful targeted permission
reproduction, not a complete rebuild of the old release.

The fixed ARM64 and AMD64 images were independently checked under the default
UID/GID 10001, network none, read-only filesystem, cap-drop ALL and
no-new-privileges. All five files had exact ownership/mode root:10001 / 0440;
`sources.yaml`, `filter.yaml`, `backup.yaml`, `scheduler.yaml`, `handoff.yaml`
were each read successfully by their actual application loader. Attempts to
write or chmod them were denied. `/app/config` was root:root / 0755, effective
capabilities were zero, and no Docker socket or working SQLite was mounted.

Exact image identities and observed results are recorded in
[PATCH_0_4_1_QA_CONTAINER.json](PATCH_0_4_1_QA_CONTAINER.json). ARM64 was native;
AMD64 was exercised through the local Docker Desktop architecture emulation.
These checks do not claim a physical Intel Mac validation.

## Offline regression results

The accepted baseline independently passed **691 tests in 8.12 seconds** with
94.15% application coverage. With the hotfix and seven independent adversarial
fixture checks, **698 tests passed in 10.04 seconds**, with measured coverage
**94.07%** (2,683 statements, 159 missed; the required threshold is 85%).
Ruff lint and formatting passed for 73 Python files; strict mypy passed for all
27 production source files. The previous scenarios were retained.

The seven new offline checks ensure that the build regression:

- Preserves the source file bytes and modes while excluding local `.env`, tokens,
  SQLite and exports from its fixture context.
- Fails closed if a new YAML policy would escape the explicit all-files check.
- Rejects source fixtures at 0644, 0400 or 0666 instead of falsely claiming a
  restrictive 0600 build was exercised.
- Rejects a symlinked fixture policy.
- Refuses to overwrite an existing build context.

These checks are in `tests/security/test_hotfix_build_context.py`; they do not
need Docker, feeds, credentials or any network service.

## Complete local Docker acceptance

Infrastructure's complete machine-readable execution evidence was independently
reviewed. Both exact fixed images passed all four runtime harnesses: restrictive
0600 build/read regression, persistent SQLite/backup/restore smoke, scheduler
restart/recovery/hardening smoke, and the full automatic-export/Bridge smoke.
The Bridge proof exported 25 synthetic candidates, delivered a bounded batch of
10, rejected a wrong acknowledgement, retained the delivery identity on restart,
avoided duplicate rewrites, accepted repeated matching acknowledgements and
prepared a disjoint next batch. Its runtime had no database or Docker socket.

The scheduler proofs took 57.549 seconds (native ARM64) and 70.766 seconds
(emulated AMD64). Measured peak memory was respectively 56,152,064 and 91,762,688
bytes, with a 536,870,912-byte limit and CPU limit 1. These peaks cover only the
final recreated scheduler cgroup, not the whole harness or Bridge workload.
Temporary smoke containers/labelled volumes were reported removed. Exact-commit
remote CI remains a separate release gate.

## Database and operator procedure review

The lead's actual disposable v0.4.0 -> v0.4.1 -> v0.4.0 compatibility execution
and JSON evidence were independently reviewed. Schema 4 and all 12 table hashes
were preserved across backup, new-image opening, configured export and old-image
reopening. A real synthetic new collection cycle preserved article identities,
first-seen values and existing history, added four observations, and exported
three candidates; the old image reopened the resulting database with identical
post-cycle table hashes. Integrity/foreign-key checks passed and temporary
volumes were removed. No operator SQLite was used.

The update/rollback procedure was independently reviewed: it preserves the
Compose project and existing volume, stops before making a unique verified
backup, records the old image and recreates the scheduler only after checks.
It deliberately avoids rebuilding v0.4.0 with restrictive files on rollback.

The scope of the fix is embedded image files. Compose bind mounts replace these
files, so their host permissions must remain readable by UID 10001 independently.
The operator instructions state this limitation and keep handoff disabled.

No actual agent consumption, LLM operation, publication, persistent new service
activation or Sprint 5 work was included or authorized by this review.
