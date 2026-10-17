# Roadmap

Sprint 0: architecture, contracts, agent ownership, locked dependency project, CI.
Sprint 1 / v0.1: RSS/Atom discovery, HTTP limits and conditionals, SQLite migrations,
identity/provenance, CLI, container and offline integration/persistence tests.
Baseline online backup, explicit restore and JSON export are included early.

Sprint 2 / v0.2.0 — Reliable Discovery: persist explainable deterministic
preliminary filtering decisions; selected JSON v2; backup retention; additional
network/corruption/interruption tests; measured constrained workload and migration
regressions. No editorial truth/interest scoring or publication approval.

Sprint 3 / v0.3.0 — Automated Discovery: opt-in configurable scheduler, process shutdown/recovery,
stale-run reconciliation, health monitoring, restart policy and extended soak test.
Sprint 2 was accepted on 2026-10-18; Sprint 3 development and isolated tests are
authorized. Permanent operator activation is still a separate approval. See
SPRINT_3_CONTRACTS.md; no new integration or deployment is authorized.

Sprint 4 (not authorized; stop after Sprint 3): separately agreed read-only candidate handoff to the WAAGD agent,
bounded free OpenRouter evaluation, quota/error handling and Telegram integration.
No changes to Shmul or shared OpenClaw state.

Sprint 5: verification, drafts, human review queue, audit/version history. A future
limited publish service and staging rollout require separate approval. Discovery
collection and publication must always remain distinct capabilities.
