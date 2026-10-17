# Operations

Run these commands from the repository directory with Docker Desktop running.
The default service is a one-shot job. Sprint 3 adds an explicitly selected scheduler
profile; the persistent operator service must not be activated without permission.

Start collection: `docker compose run --name waagd-manual discovery collect --once`.
Logs: `docker logs waagd-manual`. Stop: `docker stop waagd-manual` (SIGTERM).
Clean up the stopped container: `docker rm waagd-manual`.
Restart a new manual run: `docker compose run --rm discovery collect --once`.
Status: `docker compose run --rm discovery stats` and `docker compose ps -a`.
Integrity: `docker compose run --rm discovery verify`.
DEBUG logs: `docker compose run --rm discovery --log-level DEBUG collect --once`.

## Backup and restore

`docker compose run --rm discovery backup --output /backups/before-update.sqlite3`
creates a consistent SQLite API backup and verifies it. Use a new filename each
time. Copy the file off this machine/volume; manual backups are outside managed retention.

Restore: `docker compose run --rm discovery restore --input /backups/before-update.sqlite3 --confirm`.
Stop other jobs first. Restore checks the input, stages and validates the result,
backs up the previous working database as /data/pre-restore-*.sqlite3, checkpoints
WAL and replaces the closed database. It refuses missing/corrupt/newer-schema
inputs or busy targets. Interrupted collection does not trigger automatic restore.

Verify restored state: `docker compose run --rm discovery verify`, then `stats`.
Test restoration periodically using a separate Compose project/volume; never
experiment on the only working database. Do not copy an actively written SQLite
file manually; use the backup command.

## Update and rollback

1. Complete/stop active collection and create a uniquely named backup.
2. Record `git rev-parse HEAD` and current image ID.
3. `git fetch --tags`, then select the reviewed release with
   `git checkout --detach v0.3.0` and run `docker compose build`.
4. `docker compose run --rm discovery init-db`, then `verify` and `stats`.
5. Run one collection and inspect its JSON result and stderr logs.

Rollback the application by checking out the recorded previous commit and
rebuilding. An older app refuses a newer database schema. If schema rollback is
needed, retain the current data backup and restore the matching pre-update backup
with the target older application's restore command. A newer application's
restore may migrate a legacy backup to its own schema. Never reset the database
or delete volumes to update.

## Failures

Partial/failed result: inspect per-source error_code, retry the manual job later.
Successful sources are already committed. 304 means unchanged metadata; it does
not create new article observations. No network means no new discoveries but
previous data remains. database_busy means another supported write command owns
the advisory lock. Abrupt SIGKILL may leave a run in running state; prior committed
source data remains and SQLite recovers WAL on next open. The next shared collection acquires the ordinary exclusive lock before marking
orphan running rows interrupted and recording the interruption audit. Source
transactions already committed remain intact; no automatic restore occurs.

## Sprint 2 filtering and retention

After upgrade, metadata correction or registry/policy change:

```sh
docker compose run --rm discovery filter
docker compose run --rm discovery filter-report --limit 100
docker compose run --rm discovery filter-stats
docker compose run --rm discovery export --output /exports/candidates.json --limit 50
```

Ordinary successful/partial collection filters automatically. A fully failed or
interrupted collection preserves the prior filter batch. Export is read-only and
does not create missing assessments; its JSON selection context explains empty
results. Changed inputs/policy, unavailable assessments or aged-out articles are
withheld. Inspect reasons and explicitly rerun `filter`; do not erase articles
to resolve a failed check. Technical eligibility is not editorial verification.

Managed backup and explicit rotation:

```sh
docker compose run --rm discovery backup-managed --directory /backups/managed
docker compose run --rm discovery backup-rotate --directory /backups/managed
docker compose run --rm discovery backup-rotate --directory /backups/managed --apply
```

The first command creates/verifies a new unique backup, then applies the configured
retention. Standalone rotation previews unless --apply is given. A blocked
rotation returns exit1 with fixed reasons; a newly created verified backup may
still exist when later rotation is blocked. Keep it and inspect the result.
No valid survivor means nothing is deleted. Do not put the working SQLite in a
managed backup directory. Keep off-device copies and test restoration separately.

Restore inputs are placed in the writable /backups directory. A closed backup
may retain a WAL-mode header; ordinary SQLite restore reads can need temporary
WAL/SHM bookkeeping beside it. Copy a known closed standalone archive from a
read-only medium into the dedicated writable directory before restoring; never
substitute a raw copy of an actively written database. The original archive bytes
can be checked with a hash before/after staging. Managed retention verification
has a separate guarded immutable path and does not require writable backup files.

An interrupted assessment rolls back the entire batch. On restart, `verify`,
`filter-report`, a fresh manual `filter` and selected export are safe recovery
steps. Already committed article/provenance transactions remain. The opt-in Sprint 3 scheduler uses the same recovery and collection pipeline;
filter interruption still rolls back its entire assessment batch.


## Scheduler operator commands

Run these only after approval to start the persistent operator service. They are
provided for the handoff; development tests use a separate disposable project.

**START** builds/reuses the same image and explicitly selects only the scheduler:

```sh
docker compose --profile scheduler up -d scheduler
```

**STOP** allows 30 seconds for graceful collection interruption. The data volume
remains and `unless-stopped` respects the explicit stop:

```sh
docker compose --profile scheduler stop scheduler
```

**RESTART** explicitly starts a fresh session, including after a configuration edit:

```sh
docker compose --profile scheduler restart scheduler
```

**STATUS**, **LOGS**, and **HEALTH**:

```sh
docker compose ps -a
docker compose run --rm discovery status
docker compose --profile scheduler logs --tail 100 --follow scheduler
docker compose run --rm discovery health
```

A healthy process can still have source failures. Inspect `last_success_at`,
consecutive failures, per-source errors and filter reasons in `status`. A stopped
scheduler deliberately returns a nonzero health exit status. Do not infer process
liveness from a stale heartbeat or the existence of a lock file.

**BACKUP** remains explicit, daily: create/verify a managed copy and review its
rotation result; copy a verified backup off-device weekly. No per-cycle backup is
created. See [BACKUPS.md](BACKUPS.md).

```sh
docker compose run --rm discovery backup-managed --directory /backups/managed
```

**UPDATE**: stop the scheduler first, save a unique verified backup, record the
current commit/image and fetch the reviewed release. Do not invoke `up` until
verification succeeds and restarting the persistent service is approved:

```sh
docker compose --profile scheduler stop scheduler
docker compose run --rm discovery backup --output /backups/before-v0.3-update-001.sqlite3
git rev-parse HEAD
docker image inspect waagd-discovery:local --format '{{.Id}}'
git fetch --tags
git checkout --detach v0.3.0
docker compose build
docker compose run --rm discovery init-db
docker compose run --rm discovery verify
docker compose run --rm discovery status
```

Restart after an update with `docker compose --profile scheduler up -d scheduler`
so Compose recreates the container from the new image. A plain `restart` retains
the old image/configuration. Schema 4 is forward-only and preserves prior data.

**ROLLBACK**: stop the scheduler; make a new verified backup of the current state;
check out the recorded earlier release and rebuild it. Older binaries refuse schema
4. Restore the matching pre-upgrade backup using that older release's restore
command, then verify it before explicitly starting a service. Do not restore a
schema-3 backup with the new binary and expect a schema rollback: it migrates
forward. Preserve both backups and never delete/reset the volume as a workaround.
For a return to the accepted v0.2.0 checkpoint using its verified schema-3 backup:

```sh
docker compose --profile scheduler stop scheduler
docker compose run --rm discovery backup --output /backups/before-rollback-from-v0.3-001.sqlite3
git fetch --tags
git checkout --detach v0.2.0
docker compose build
docker compose run --rm discovery restore --input /backups/before-v0.3-update-001.sqlite3 --confirm
docker compose run --rm discovery verify
docker compose run --rm discovery stats
```

The selected input must be the schema-3 backup made before the v0.3 upgrade.
v0.2.0 is manual-only; do not issue a scheduler start/restart after rollback.
If an older tagged revision does not define the scheduler service, stop it using
the current Compose configuration before checking out that revision, as above.

## Disk and growth checks

Discovery keeps original articles, observations and filter audit batches. Frequent
cycles increase the observation/audit history even when article deduplication works;
there is no automatic article/history deletion in Sprint 3. Check host free space,
Docker volume allocation, DB/WAL and backup sizes regularly, especially after
changing the interval or registry:

```sh
df -h .
docker system df -v
docker compose run --rm --entrypoint python discovery -c 'from pathlib import Path; p=Path("/data"); print({x.name: x.stat().st_size for x in p.iterdir() if x.is_file()})'
du -sh backups exports
```

Do not run Docker prune, volume deletion or manual WAL removal to fix low disk
space. Stop collection, preserve verified off-device copies and review growth.
Managed rotation protects the last valid managed copy; it cannot free observation
history or guarantee host capacity. Test restore regularly on a disposable volume.
