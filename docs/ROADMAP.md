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

Sprint 4 / v0.4.0 — Data-only OpenClaw integration interface: opt-in automatic
selected export, bounded host-side JSON bridge, durable delivery/acknowledgement,
fake workspace tests and operator-only update/integration instructions. The actual
Intel Mac/OpenClaw deployment is inaccessible; never modify the existing agent.
No OpenRouter/LLM, Telegram or real editorial processing is authorized.

Sprint 5 / v0.5.0-rc.1 — Editorial Processing & Source Verification: explicitly
authorized after architectural approval, on a separate unmerged branch. Adds a
mock-only controller/adapter, bounded verifier, separate durable store, Bridge v2
and conditional receipt-based ACK, adversarial/crash recovery tests and operator
review instructions. No real LLM, deployment or existing agent changes. See
SPRINT_5_CONTRACTS.md and EDITORIAL_ARCHITECTURE.md. Stop after the release candidate
for architectural review; Sprint 6 and publication remain unapproved. Discovery
collection and publication must always remain distinct capabilities.
