# Roadmap

Sprint 0: architecture, contracts, agent ownership, locked dependency project, CI.
Sprint 1 / v0.1: RSS/Atom discovery, HTTP limits and conditionals, SQLite migrations,
identity/provenance, CLI, container and offline integration/persistence tests.
Baseline online backup, explicit restore and JSON export are included early.

Sprint 2: persist explainable deterministic preliminary filtering decisions
(title/URL/age/source metadata), source error/backoff tuning, backup retention,
export selection policy and expanded resource/long-running reliability evidence.
The current discovery export is deliberately unfiltered and unverified.

Sprint 3 / v0.2: opt-in configurable scheduler, process shutdown/recovery,
stale-run reconciliation, health monitoring, restart policy and extended soak test.
Do not enable an automatic schedule as part of v0.1.

Sprint 4: separately agreed read-only candidate handoff to the WAAGD agent,
bounded free OpenRouter evaluation, quota/error handling and Telegram integration.
No changes to Shmul or shared OpenClaw state.

Sprint 5: verification, drafts, human review queue, audit/version history. A future
limited publish service and staging rollout require separate approval. Discovery
collection and publication must always remain distinct capabilities.
