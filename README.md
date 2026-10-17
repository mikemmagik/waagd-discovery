# WAAGD Discovery

Independent RSS/Atom discovery for We Are All Gonna Die. Python modular monolith,
Docker Compose and persistent SQLite. Manual collection only; no AI requests or
automatic publication. This repository is private and separate from the website.

```sh
git clone https://github.com/mikemmagik/waagd-discovery.git
cd waagd-discovery
mkdir -p exports backups
docker compose build
docker compose run --rm discovery init-db
docker compose run --rm discovery collect --once
docker compose run --rm discovery stats
docker compose run --rm discovery verify
docker compose run --rm discovery export --output /exports/candidates.json --limit 50
```

Docker Desktop must be running. On Linux give container UID10001 write access to
the dedicated exports/backups directories; see [Deployment](docs/DEPLOYMENT.md).
Python dependencies are inside the image, not globally installed on the host.

Commands output JSON, diagnostics are JSON on stderr. Exit codes: 0 success,
1 failed or partial collection, 2 invalid arguments/configuration, 130 interrupted.
Global `--db` and `--sources` options precede the command.

```sh
docker compose run --rm discovery collect --once --source guardian_world
docker compose run --rm discovery collect --once --dry-run
docker compose run --rm discovery backup --output /backups/manual.sqlite3
docker compose run --rm discovery restore --input /backups/manual.sqlite3 --confirm
```

Do not use `docker compose down -v`: it deletes the database volume. A restore
retains a verified pre-restore backup in /data before replacing the database.
Copy backups to a separate device for disaster recovery. Existing backup files
are never overwritten.

Implemented boundaries: RSS 2.0/Atom metadata, bounded public HTTP, conditional
requests, retries, deterministic URL/GUID deduplication, source provenance and
observation history, per-source atomic writes, run history, CLI, atomic bounded
JSON export, backup and explicit restore. An exported item is an unverified
discovery; it has not been shortlisted or approved.

Future work: deterministic preliminary filtering, richer operations, scheduler,
then separately approved WAAGD integration. No agent workspace is mounted.

## Development

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
python3 scripts/check-secrets.py
bash scripts/docker-smoke.sh
```

Offline tests use fixtures and mocked networking. Real source availability is
checked separately; live feeds are not required for CI. See
[Architecture](docs/ARCHITECTURE.md), [Contracts](docs/CONTRACTS.md),
[Configuration](docs/CONFIGURATION.md), [Runbook](docs/RUNBOOK.md),
[Security](docs/SECURITY.md), [Decisions](docs/DECISIONS.md),
[Roadmap](docs/ROADMAP.md) and [QA report](docs/QA_REPORT.md).

All rights reserved. No open-source license is granted.
