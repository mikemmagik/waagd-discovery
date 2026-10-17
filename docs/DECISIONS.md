# Decisions

1. Modular monolith, synchronous bounded HTTP and SQLite for one operator and a
   few feeds. No broker or worker service is justified.
2. URL is primary identity, GUID is source-scoped fallback. Retain aliases and
   immutable per-discovery observations. Do not semantically cluster events.
3. Python 3.13 container; uv frozen lock; small HTTP/XML/YAML libraries, no LLM SDK.
4. Manual one-shot collection only. Restart policy is disabled; no artificial
   daemon or public port. Future scheduling requires a separate change.
5. Ship baseline backup/export and conditional HTTP early to make core ingestion
   safe to operate. These do not enable the future agent integration.
6. Proprietary source by default; no open-source license is granted implicitly.
