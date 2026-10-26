# WAAGD Discovery

Independent RSS/Atom discovery for We Are All Gonna Die. Python modular monolith,
Docker Compose and persistent SQLite. The Docker collector remains separate from
the explicitly configured, tool-free editorial Gateway and publication runtime.
This repository is private and separate from the website.

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
v0.4 adds a data-only host bridge and opt-in automatic selected export. The
existing WAAGD agent is unchanged. See [Integration](docs/INTEGRATION.md) and
[operator handoff](docs/BRIDGE_OPERATIONS.md); the separate deployed Intel Mac
is inaccessible here. No real LLM processing or publication is enabled.

v0.5.0-rc.1 adds a separate mock-only Editorial Controller, private Editorial
SQLite, bounded Source Verifier, versioned Bridge v2 and conditional ACK recovery.
V1 remains the default and its pending deliveries are unchanged. Real processing,
LLM calls and automatic ACK remain disabled. This release candidate is for review
on a separate branch; it does not update the working Mac. See
[Editorial architecture](docs/EDITORIAL_ARCHITECTURE.md),
[isolated installation and rollback](docs/EDITORIAL_OPERATIONS.md),
[Sprint 5 report](docs/SPRINT_5_REPORT.md) and
[independent QA](docs/SPRINT_5_QA_REVIEW.md).

v0.5.0-rc.2 corrects a macOS `umask 077` assumption in one Bridge security test;
runtime Bridge behavior and contracts are unchanged. See the
[rc.2 patch report](docs/PATCH_0_5_0_RC2_REPORT.md) and
[independent QA](docs/PATCH_0_5_0_RC2_QA_REVIEW.md).

v0.6.0 completes the accepted Sprint 6 integration without enabling a deployment.
It retains the Discovery engine and schema 4, Scheduler, Bridge v1/v2, Editorial
Controller/Store, Source Verifier, bounded Gateway adapter, trusted PublicationIntent
v1 policy gate, and private recovery-aware handoff. Automatic publication remains
default-deny and requires VERIFIED evidence plus the site's independent kill switch.
The final compatibility target is site commit
`ca112b2ab89a7ec0d82b85b06df07bb86a35b41c`; see the
[final operator handoff](docs/WAAGD_DISCOVERY_STABLE_HANDOFF.md).

v1.0.0 is the production release of the accepted Discovery-to-publication chain.
It retains the Docker Discovery Scheduler, isolated Bridge and Editorial state,
OpenClaw tool-free Gateway restricted to `openrouter/openrouter/free`, Fidelity,
immutable PublicationIntent delivery, ACK reconciliation and the Bangkok daily
coordinator. Production installation, state separation and rollback are specified
in the [v1.0.0 release guide](docs/RELEASE_1_0_0.md).

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
python3 scripts/docker-bridge-smoke.py
python3 scripts/docker-editorial-smoke.py
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

Sprint 3 evidence: [Sprint report](docs/SPRINT_3_REPORT.md), [independent QA](docs/SPRINT_3_QA_REVIEW.md), [contracts](docs/SPRINT_3_CONTRACTS.md), [scheduler operations](docs/SCHEDULER.md).

Sprint 4 interface: [contracts](docs/SPRINT_4_CONTRACTS.md), [integration](docs/INTEGRATION.md), [bridge operations](docs/BRIDGE_OPERATIONS.md).
Acceptance: [Sprint report](docs/SPRINT_4_REPORT.md), [independent QA](docs/SPRINT_4_QA_REVIEW.md),
[Docker matrix](docs/SPRINT_4_DOCKER.json), [native bridge](docs/SPRINT_4_NATIVE_HOST.json),
[v0.3 compatibility](docs/SPRINT_4_COMPATIBILITY.json).

v0.4.1 packaging hotfix: [patch report](docs/PATCH_0_4_1_REPORT.md),
[independent QA](docs/PATCH_0_4_1_QA_REVIEW.md),
[operator update from v0.4.0](docs/PATCH_0_4_1_UPDATE.md).
