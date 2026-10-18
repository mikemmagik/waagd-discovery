# Operator update: v0.4.0 → v0.4.1

Run only on the operator's existing Intel Mac checkout. Development has no access
to that Mac. This patch fixes embedded image configuration permissions; it changes
no Bridge protocol, database schema, collection behavior or security settings.
Do not change OpenClaw/waagd, enable handoff, call an LLM or start Sprint 5.

## Preserve the existing deployment

Use the same checkout, Compose project name and named SQLite volume. Record the
actual project and mounted volume from the existing scheduler before proceeding:

```sh
docker compose ps -a
docker compose config --volumes
docker inspect "$(docker compose --profile scheduler ps -q scheduler)" --format '{{json .Mounts}}'
git rev-parse HEAD
git status --short
docker image inspect waagd-discovery:local --format '{{.Id}}'
docker image tag waagd-discovery:local waagd-discovery:operator-v0.4.0
```

Use another unused preservation tag if that tag already identifies a different
image. Preserve local configuration/source policies separately and resolve edits
deliberately; never discard them to make checkout succeed. Keep handoff disabled
and the accepted 30-minute interval. None of the steps require root application
execution, `chmod 777`, volume deletion, Docker prune or database reset.

## Stop, back up and update

Stop the scheduler, then make a verified backup with the current v0.4.0 image.
Use a new unique filename for every attempt; retain the previous verified copies
and an off-device copy. The following name is an example, not permission to
overwrite a prior file:

```sh
docker compose --profile scheduler stop scheduler
docker compose run --rm discovery backup --output /backups/before-v0.4.1-update-001.sqlite3
docker compose run --rm discovery verify
docker compose run --rm discovery stats
git fetch --tags
git checkout --detach v0.4.1
docker compose config --quiet
docker compose build
```

Keep the recorded Compose project/volume. Re-check `docker compose config
--volumes` and the existing mounts before any application operation. A different
project or directory can select a new empty volume. SQLite remains **schema 4**;
no new migration or automatic restore is needed.

## Verify the image and the existing database

This regression creates an isolated context with all five embedded YAML sources
at mode 0600, builds a separate test image and verifies their permissions/readers.
It does not chmod the operator checkout or mount the working database:

```sh
python3 scripts/docker-config-smoke.py
WAAGD_SMOKE_IMAGE=waagd-discovery:local WAAGD_SMOKE_SKIP_BUILD=1 python3 scripts/docker-bridge-smoke.py
docker compose run --rm discovery verify
docker compose run --rm discovery stats
docker compose run --rm discovery integration-status
```

The bridge smoke uses only synthetic RSS and disposable volumes/workspace; its
automatic export configuration is test-only. It does not enable the real handoff.
Check the regression script's `--help` for image/context options if required.
The new image's embedded `sources.yaml`, `filter.yaml`, `backup.yaml`,
`scheduler.yaml`, `handoff.yaml` must all be **root:10001 / 0440**; the image's
default user remains **10001:10001**, and `/app/config` is root:root / 0755.

Compose's read-only configuration bind mounts replace the embedded files. Image
ownership/modes cannot repair an unreadable host bind mount. The currently working
deployment's mount permissions and policies must be preserved; if those fail,
inspect the actual mount and stop rather than run the application as root or
broaden permissions indiscriminately. This patch covers embedded image files.

## Recreate and check the accepted scheduler

After verification, recreate the existing scheduler from the newly built image:

```sh
docker compose --profile scheduler up -d --no-deps scheduler
docker compose ps -a
docker inspect "$(docker compose --profile scheduler ps -q scheduler)" --format '{{json .Mounts}}'
docker compose run --rm discovery health
docker compose run --rm discovery status
docker compose run --rm discovery integration-status
docker compose --profile scheduler logs --tail 100 scheduler
```

Compare its actual database volume to the recorded pre-update one. A plain
`restart` does not replace the image. Health may need its ordinary startup grace;
inspect diagnostics if it fails. Confirm schema integrity, retained article
counts/provenance, 30-minute schedule and disabled automatic export. No real
integration or editorial processing is part of this update.

## Rollback without losing SQLite

If rollback is needed, stop the scheduler and preserve a new uniquely named
verified backup of the current database first. Preserve any Bridge ledger/inbox
separately; this patch never resets their history.

Check out v0.4.0, retain the same project/configuration/volume, and use the recorded
preserved v0.4.0 image. With the example tag above:

```sh
docker compose --profile scheduler stop scheduler
docker compose run --rm discovery backup --output /backups/before-v0.4.1-rollback-001.sqlite3
docker compose run --rm discovery verify
git checkout --detach v0.4.0
docker image tag waagd-discovery:operator-v0.4.0 waagd-discovery:local
docker compose run --rm discovery verify
docker compose run --rm discovery stats
docker compose --profile scheduler up -d --no-deps scheduler
```

Do not rebuild v0.4.0 from restrictive files: that recreates the original defect.
Both releases use schema 4, so ordinary code rollback needs no database restore.
If separate evidence requires restore, preserve the current database and use the
target version's verified backup/restore procedure; never replace live SQLite by
manually copying a file. Never use `down -v`, remove the volume or prune it.
