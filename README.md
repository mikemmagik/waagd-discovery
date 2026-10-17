# WAAGD Discovery

Independent RSS/Atom discovery for We Are All Gonna Die. Python modular monolith,
Docker Compose and persistent SQLite. Manual collection and an explicitly enabled scheduler; no AI requests or
automatic publication. This repository is private and separate from the website.

```sh
git clone https://github.com/mikemmagik/waagd-discovery.git
cd waagd-discovery
mkdir -p exports backups
docker compose build
docker compose run --rm discovery init-db
docker compose run --rm discovery collect --once
docker compose run --rm discovery filter-report --limit 100
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
docker compose run --rm discovery backup-managed --directory /backups/managed
docker compose run --rm discovery backup-rotate --directory /backups/managed
docker compose run --rm discovery backup-rotate --directory /backups/managed --apply
docker compose run --rm discovery restore --input /backups/manual.sqlite3 --confirm
```

Do not use `docker compose down -v`: it deletes the database volume. A restore
retains a verified pre-restore backup in /data before replacing the database.
Copy backups to a separate device for disaster recovery. Existing backup files
are never overwritten.

Implemented boundaries: RSS 2.0/Atom metadata, bounded public HTTP, conditional
requests, retries, deterministic URL/GUID deduplication, source provenance and
observation history, per-source atomic writes, run history, CLI, atomic bounded
JSON export, backup and explicit restore. v0.2 adds configurable deterministic
filtering, immutable decision audits and safe managed backup retention. Collection
assesses automatically; use `filter` to reconsider existing articles and
`filter-report` to inspect reasons. Exports contain eligible candidates with
matching current assessments; age and source configuration are checked again at
export time. An exported item remains unverified and has no publication approval.

Filtering policy: `config/filter.yaml` (72 hours, title/URL/enabled source, 40
description characters, valid publication date by default; no keyword rules).
Managed retention: `config/backup.yaml` (keep newest seven verified copies plus
all verified copies from the last 30 days). Standalone rotation previews by default;
`backup-managed` verifies a new copy and applies retention as part of that command. Manual,
pre-restore, corrupt and incompatible copies are preserved; only managed filenames
are eligible for pruning, with a verified surviving copy checked before deletion.

Selected export is JSON schema v2; v1 is retained as a historical contract. Run
`docker compose run --rm discovery filter` after upgrading a v0.1 database.
An unassessed or changed article is withheld rather than falling back to raw
discoveries. Reference time can be fixed with `filter --at ISO8601` and
`export --at ISO8601` for reproducible inspection. See [Filtering](docs/FILTERING.md)
and [Backups](docs/BACKUPS.md).

v0.3 adds opt-in fixed-delay scheduling (30 minutes by default), lock-based orphan
run recovery, read-only JSON service diagnostics and filter reason statistics.
Scheduler is excluded from default Compose startup. See [Scheduler operations](docs/SCHEDULER.md)
for explicit START/STOP/RESTART/STATUS/LOGS/HEALTH commands and sleep/restart behavior.
`status`, `health` and `filter-stats` are also available through the one-shot CLI.
A failed feed does not make the service unhealthy. Daily backups remain an explicit
operator task. No permanent operator scheduler was activated during development.
No agent integration or publication exists; Sprint 4 requires separate agreement.

## Development

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
python3 scripts/check-secrets.py
bash scripts/docker-smoke.sh
python3 scripts/docker-scheduler-smoke.py
```

Offline tests use fixtures and mocked networking. Real source availability is
checked separately; live feeds are not required for CI. See
[Architecture](docs/ARCHITECTURE.md), [Contracts](docs/CONTRACTS.md),
[Configuration](docs/CONFIGURATION.md), [Runbook](docs/RUNBOOK.md),
[Security](docs/SECURITY.md), [Decisions](docs/DECISIONS.md),
[Roadmap](docs/ROADMAP.md) and [QA report](docs/QA_REPORT.md).

Sprint 2 evidence: [independent QA](docs/SPRINT_2_QA_REVIEW.md),
[contracts](docs/SPRINT_2_CONTRACTS.md), [Sprint report](docs/SPRINT_2_REPORT.md).
`scripts/benchmark.py` runs a reproducible synthetic workload only on an empty,
disposable data directory. Never run workloads against the operator database.

All rights reserved. No open-source license is granted.
