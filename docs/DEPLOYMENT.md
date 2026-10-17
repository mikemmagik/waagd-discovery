# Deployment and manual operation

v0.1 is a one-shot command, with no background scheduler. Use Docker Desktop on
the MacBook or Docker Engine with the Compose plugin on Linux. Docker provides
Python; a global Python installation is unnecessary. Nothing in these commands
connects to OpenClaw, Telegram, Cloudflare or the existing WAAGD website.

## First installation

Clone the private repository using your existing GitHub authentication:

```sh
git clone https://github.com/mikemmagik/waagd-discovery.git
cd waagd-discovery
mkdir -p exports backups
docker compose build
```

The image uses UID/GID `10001:10001`. The named database volume inherits its
directory ownership from the image. The two host bind directories must also be
writable by UID 10001. Prepare only these dedicated directories before use:

```sh
sudo chown 10001:10001 exports backups
sudo chmod 0700 exports backups
```

On Docker Desktop, host permission translation can differ from Linux. If export
or backup reports a permission error, check those two bind mounts rather than
running discovery as root. Host access to files owned by UID 10001 may require
`sudo`. Keep the directory ownership consistent across upgrades.

The registry file must exist at `config/sources.yaml`; Compose mounts it read-only.
No `.env` file, API key or other credential is needed. `.env.example` documents
native CLI paths; Compose sets its own fixed paths. Run from the repository root:

```sh
docker compose config --quiet
docker compose run --rm discovery init-db
docker compose run --rm discovery verify
docker compose run --rm discovery stats
```

The default `docker compose run --rm discovery` command is `stats` and exits.
No ports are published. The runtime has a read-only root filesystem, a writable
32 MiB temporary filesystem, no Linux capabilities, no privilege escalation and
no Docker socket. Only `/data`, `/exports`, `/backups` and `/tmp` are writable.
Compose limits each command to 512 MiB memory, one CPU and 64 processes. These
are initial safety bounds; inspect representative workloads before changing them.

## Manual collection and output

This command explicitly fetches the enabled public RSS/Atom feeds:

```sh
docker compose run --rm discovery collect --once
docker compose run --rm discovery stats
docker compose run --rm discovery verify
```

To limit a run, append `--source SOURCE_ID` using an ID from the registry. To
inspect collection without persisting changes, append `--dry-run`. Registry edits
are host file edits and become visible on the next command; only RSS and Atom
collection are supported. Feed content is untrusted data.

Use a fresh output name for each export and backup:

```sh
docker compose run --rm discovery export --output /exports/candidates-001.json --limit 50
docker compose run --rm discovery backup --output /backups/manual-001.sqlite3
```

Host files appear under `exports/` and `backups/`. Backups use SQLite's online
backup API and receive an integrity check. A file copied directly from a running
WAL database is not an adequate backup. Copy verified backups to separate
operator-controlled storage; a backup beside the database does not protect
against losing the MacBook. These files must remain outside Git.

CLI results go to stdout and structured application logs to stderr. For a local
record without retaining a stopped container:

```sh
mkdir -p work
docker compose run --rm -T discovery collect --once >work/collect.json 2>work/collect.log
```

Inspect the exit status as well as the result: 0 is success, 1 is failure or a
partial run, 2 is a usage/configuration error, and 130 is an interrupted run.
Logs for an ongoing or retained container can also be read with
`docker compose logs discovery`; containers started with `--rm` disappear at exit.

## Stop, restore and update

There is no resident process to stop after a one-shot run. During an attached run,
Ctrl-C interrupts collection. For an active Compose container in another terminal:

```sh
docker compose stop --timeout 30 discovery
```

Normal stops and `docker compose down` retain the named database volume. Never
use `docker compose down --volumes` against the working installation. The smoke
test below deletes only its own newly created, explicitly named test volume.

Before restoring, ensure no collection is active and preserve a separate verified
backup of the current state. Restore requires an explicit confirmation flag and
creates an additional pre-restore backup of an existing database:

```sh
docker compose run --rm discovery backup --output /backups/before-restore-001.sqlite3
docker compose run --rm discovery restore --input /backups/manual-001.sqlite3 --confirm
docker compose run --rm discovery verify
docker compose run --rm discovery stats
```

Do not overwrite a working database manually or remove it to repair a migration.
An unknown newer schema is rejected. Keep the last working code revision and its
pre-upgrade backup together; an older application cannot read every newer schema.

For an update, back up with the current image first, then fetch and build the new
revision. No force update or automatic database reset is part of this procedure:

```sh
docker compose run --rm discovery backup --output /backups/before-update-001.sqlite3
git pull --ff-only
docker compose build
docker compose run --rm discovery verify
docker compose run --rm discovery stats
```

## Reproducibility and verification

The Dockerfile pins Python 3.13.16 on Debian bookworm and uv 0.13.1 by multiarch
image index digest. Both indexes include Linux amd64 and arm64. Runtime packages
are installed with `uv sync --frozen --no-dev`, and the final application is
installed without an editable source checkout. The uv binary, development tools,
Git metadata and host files are not copied into the runtime image. The build
context allowlist admits only source, the package manifest/lock and registry.

Digest updates must be reviewed and rebuilt. Check upstream registry metadata:

```sh
docker buildx imagetools inspect python:3.13.16-slim-bookworm
docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.13.1
```

See the upstream [Python image sources](https://github.com/docker-library/python)
and [uv Docker integration guidance](https://docs.astral.sh/uv/guides/integration/docker/).

Run the container acceptance check locally:

```sh
sh scripts/docker-smoke.sh
```

It builds the image, disables networking for every runtime container, parses an
embedded RSS fixture, ingests it in two separate containers against one temporary
volume, checks deduplication and persistence, invokes CLI verification/export/
backup, and verifies the resulting backup in another container. It also asserts
the nonroot UID, read-only root, no effective capabilities, no-new-privileges and
absence of the Docker socket. Its disposable volume and files are cleaned up even
on failure. Set `WAAGD_SMOKE_IMAGE` to an existing local image and
`WAAGD_SMOKE_SKIP_BUILD=1` to test that exact image without rebuilding.

CI runs lint, formatting, strict types and offline tests on Python 3.13 and 3.14,
with an 85% package coverage minimum. Independent amd64 and arm64 jobs then build
the pinned image and run the same offline smoke; arm64 uses QEMU on the runner.
No container image is pushed automatically. GitHub Actions are pinned to commit
SHAs and receive read-only repository access. Dependabot opens proposed dependency,
image and action updates for review.

`scripts/check-secrets.py` scans Git-tracked source for runtime data, private keys,
credential URLs and recognizable provider token formats. It reports locations
and rule names without printing values. This targeted check cannot prove the
absence of every secret format or remove already committed history; any real leak
requires credential revocation and separate history review.

Public-feed smoke checks are deliberately separate from CI and require an
explicit manual collection command. A green offline smoke does not establish
that every external publisher is currently reachable. There is no scheduler,
health endpoint, restart loop, LLM integration or publication service in v0.1.
