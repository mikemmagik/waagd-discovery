# Operations

Run these commands from the repository directory with Docker Desktop running.
The v0.2 service is a one-shot job; there is no continuously running scheduler.

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
3. `git pull --ff-only`, then `docker compose build`.
4. `docker compose run --rm discovery init-db`, then `verify` and `stats`.
5. Run one collection and inspect its JSON result and stderr logs.

Rollback the application by checking out the recorded previous commit and
rebuilding. An older app refuses a newer database schema. If schema rollback is
needed, retain the current data backup and explicitly restore the matching
pre-update backup first. Never reset the database or delete volumes to update.

## Failures

Partial/failed result: inspect per-source error_code, retry the manual job later.
Successful sources are already committed. 304 means unchanged metadata; it does
not create new article observations. No network means no new discoveries but
previous data remains. database_busy means another supported write command owns
the advisory lock. Abrupt SIGKILL may leave a run in running state; prior committed
source data remains and SQLite recovers WAL on next open. Scheduler recovery and
stale-run reconciliation are deferred to Sprint 3.

## Sprint 2 filtering and retention

After upgrade, metadata correction or registry/policy change:

```sh
docker compose run --rm discovery filter
docker compose run --rm discovery filter-report --limit 100
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

An interrupted assessment rolls back the entire batch. On restart, `verify`,
`filter-report`, a fresh manual `filter` and selected export are safe recovery
steps. Already committed article/provenance transactions remain. There is no
automatic restart job, stale-run rewriting or scheduled collection in v0.2.
