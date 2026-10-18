# Patch 0.4.1 — embedded Docker configuration permissions

Prepared 2026-10-18 (Asia/Bangkok). Accepted base: v0.4.0,
`e754455308a4cb9b9409bcaf4be46aaa6a97af2b`. The operator reported a failed
`docker-bridge-smoke.py` on the separate Intel Mac: UID/GID 10001 could not read
`/app/config/filter.yaml`, which was root:root / 0600. Production Discovery and
scheduler were working, SQLite was preserved and automatic export was disabled.
No target Mac, OpenClaw, agent or production access occurred during this patch.

## Cause and minimal fix

The runtime Dockerfile's original COPY assigned no explicit mode/group. Host
0600 YAML files therefore became unreadable root:root / 0600 image files.
Independent QA reproduced the defect for **all five** built-in configurations;
it is not an Intel-specific application or SQLite defect.

The fix is limited to Docker packaging:

```dockerfile
COPY --chown=0:10001 --chmod=0440 config/sources.yaml config/filter.yaml config/backup.yaml config/scheduler.yaml config/handoff.yaml ./config/
```

`/app/config` is explicitly root:root / 0755. Files are root-owned, group 10001,
mode 0440: the application can read them through its unchanged group but cannot
write them or change their mode. The application remains **10001:10001**.
Compose, read-only root, capability drop, no-new-privileges, CPU/memory/PID limits
and mounted directories are unchanged. No world-writable configuration is used.

Only version metadata changes from 0.4.0 to 0.4.1 in application sources. Bridge
code/protocol/schema, storage/migrations/schema 4, collection, scheduler, source
registry and policies are unchanged. Dependencies and their versions are unchanged.

## Regression and independent QA

New `scripts/docker-config-smoke.py` creates an isolated runtime build context
with **all five YAML source files mode 0600**. It never chmods the checkout.
The resulting image is checked under its default UID/GID 10001, networking off,
read-only root, cap-drop ALL and no-new-privileges. Assertions cover exact owners,
groups/modes, regular file type, all five actual policy loaders, raw readability,
write/chmod denial and absence of database/socket access.

CI now builds each AMD64/ARM64 image from that restrictive context, probes the
same image, and runs every previous persistence, scheduler and bridge smoke on it.
The build action's architecture-specific cache is retained. This prevents a
normal-permission image from silently satisfying the regression.

All **691** accepted unit/integration/security tests remain. Independent QA adds
seven offline checks for context byte/mode preservation, exclusion of local
secrets/runtime data, detection of uncovered YAML, rejection of non-0600 fixture
files and symlinks, and refusal to overwrite an existing context. Independent
result: **698 PASS in 10.04 seconds**, coverage **2524/2683 = 94.07%**, Ruff lint
and format **73 files PASS**, strict mypy **27 production files PASS**. The exact
final root run and CI results are attached to the release/operator report.

QA approved Docker/CI/regression commit `263f9cc` before integration. Its source
review, negative old-image control and independent runtime probes for both fixed
architectures are documented in [PATCH_0_4_1_QA_REVIEW.md](PATCH_0_4_1_QA_REVIEW.md).

## Docker and preservation evidence

[PATCH_0_4_1_DOCKER.json](PATCH_0_4_1_DOCKER.json) records the local architecture
matrix: restrictive-source configuration regression, original persistence/
deduplication/export/backup/restore, scheduler restart/SIGTERM/SIGKILL/recovery/
recreation/integrity, and full synthetic automatic export → Bridge smoke.
ARM64 is native on the development Mac; AMD64 is emulated. Neither substitutes
for actual target Intel hardware/operator filesystem acceptance.

The full bridge smoke exports 25 fictional articles, publishes ten candidates,
repeats without rewriting, rejects a wrong ACK, repairs a missing inbox with the
same UUID/payload, accepts an idempotent fake ACK and delivers a disjoint next
batch. Only fake workspace/state and disposable volumes are used. No real agent
consumption, LLM call or automatic publication occurs.

[PATCH_0_4_1_COMPATIBILITY.json](PATCH_0_4_1_COMPATIBILITY.json) records a pinned
v0.4.0 → v0.4.1 → v0.4.0 proof on disposable data. A v0.4.0 database/verified backup
contains three articles, four provenance relations, eight observations and two
filter batches. **All 12 table hashes** remain exact after v0.4.1 opening,
configured export and v0.4.0 reopening. A real v0.4.1 collection on a backup-derived
copy deduplicates four existing observations, exports three candidates and keeps
original history, identities and first-seen times. Expected last-seen refresh and
new audit/observation rows do not remove originals. v0.4.0 reopens the post-cycle
schema-4 database with identical table hashes. All disposable proof volumes were
removed; the operator's working volume was never used.

## Release gate and operator procedure

The annotated v0.4.1 tag resolves the final release commit. Its full SHA and
exact-commit CI link/results are attached to the GitHub Release and Russian
operator report after all four jobs pass. Do not create the tag/release before
that gate. The repository remains private.

Follow [PATCH_0_4_1_UPDATE.md](PATCH_0_4_1_UPDATE.md): retain the existing Compose
project and SQLite volume, record/preserve the working v0.4.0 image/configuration,
stop scheduler, create a unique verified backup and off-device copy, check out
v0.4.1, build/probe/verify, then explicitly recreate the accepted scheduler with
the same mounts. No new migration, restore or reset is needed. Keep automatic
export disabled and the accepted 30-minute interval. Ordinary rollback uses the
recorded v0.4.0 image with the same schema 4; do not rebuild the faulty old
Dockerfile from restrictive sources. Preserve current backup/history first.

Docker COPY permissions apply to **embedded image files**. Read-only Compose
bind mounts replace those files with the operator's host files, whose ownership
and readability remain host-managed. Preserve the currently working deployment's
permissions; do not fix unrelated mount errors by root execution or chmod 777.

No OpenClaw/waagd/Shmul/Telegram/Cloudflare/production changes, no new feature,
Bridge protocol change, publication or LLM invocation. No real target update was
performed remotely. Installation and actual Intel Mac checks belong to the
operator. Sprint 5 remains unstarted.
