# Shared contracts — v1

These original collection/storage contracts remain supported. Sprint 2 additions
and the versioned selected export are defined in SPRINT_2_CONTRACTS.md and
FILTERING.md. Raw Repository.candidates is a storage primitive; the public CLI
export now uses persisted eligible decisions and schema v2.

`models.py` is authoritative. Datetimes are aware UTC. Missing or invalid
publication dates remain null with date_status; discovery time is actual UTC.
Source IDs match `[a-z0-9_]+`; only configured HTTP(S) feeds are fetched.

## Collector

`HttpClient(settings: HttpSettings = HttpSettings())` is a context manager.
`fetch(source: Source) -> FetchResult` sends conditional headers; bounds retries,
size, redirects and network times. Raises DiscoveryError with safe fixed code.
Every redirect is validated. No full article fetches. For tests an httpx MockTransport
may be injected and DNS validator/sleep patched. Public-address validation remains
enabled in normal operation.

`collectors.rss.parse_feed(body: bytes, source: Source, max_items: int = 1000)
-> ParseResult`. RSS 2.0 and Atom only, safe XML, reject DTD/entities. Missing or
invalid URL is skipped, missing title is retained as null. Bound text fields:
title 1000, description 4000, publisher 300, GUID/URL 4096. Description is plain
text. Oversized item count fails rather than silently acknowledging partial data.

## Storage

`normalization.normalize_url(url: str) -> str` raises ValueError for invalid
HTTP(S) URLs. Idempotent, lower-case IDNA hostname, fragment/tracking removal,
preserve meaningful query order/duplicates and path semantics including slash.

`Repository(path: str | Path)` context manager opens/migrates. Methods:

* `sync_sources(sources: list[Source]) -> None`; disabled registry entries retained.
* `get_source(source_id: str) -> Source`; includes stored conditional validators.
* `start_run() -> str` (UUID).
* `save_source(run_id: str, source: Source, items: list[ArticleCandidate],
  fetched: FetchResult, skipped: int = 0) -> tuple[int, int, int]` returns
  added/existing/failed, records source success, validators and observations.
  Validation errors in individual items count as failed; unexpected DB errors
  roll back the source transaction. 304 changes no article last_seen timestamps.
* `fail_source(run_id: str, source: Source, code: str) -> None`.
* `finish_run(run_id: str, status: str) -> dict[str, object]`; aggregates source
  outcomes; statuses success/partial/failed/interrupted. Counts include skipped.
* `stats() -> dict[str, object]` includes articles, sources, runs, observations.
* `candidates(limit: int = 50) -> list[dict[str, object]]`; JSON-compatible current
  article metadata incl id, title, url, publisher, published_at, description,
  date_status and `sources` list of objects with id/name/url. Export filtering is
  coordinator-owned; return recent articles ordered first_seen_at DESC, id ASC.
* `backup(destination: Path) -> None`; fail if destination exists; integrity check.
* `verify() -> None`; integrity_check + foreign_key_check.

URL wins over source-scoped GUID; if these point at different existing articles,
do not merge/destructively rewrite: count item as failed and retain both. A GUID
with a changed URL aliases to the existing article. Canonical URL/first_seen_at
are immutable. Latest observed mutable metadata updates the current article;
every sighting retains original per-source metadata in observations. Exact
repeats in one feed may count existing; uniqueness prevents duplicate articles.
Schema uses versioned transactional migrations; refuses newer schema versions.

## CLI and infrastructure

Entrypoint `waagd-discovery`. Global options `--db PATH` (env WAAGD_DB default
/data/discovery.sqlite3), `--sources PATH` (WAAGD_SOURCES default config/sources.yaml).
Commands: init-db, collect --once [--source ID] [--dry-run], stats, verify,
export --output PATH [--limit 50], backup --output PATH,
restore --input PATH --confirm. Exit codes 0 success, 1 failed/partial, 2 usage/config,
130 interrupt. No scheduler. Compose service `discovery`, named volume /data,
bind mounts ./exports:/exports, ./backups:/backups and registry read-only.
Default container command stats. Root filesystem read-only; /tmp tmpfs.
Python dependencies locked with uv.lock. `uv sync --frozen --no-dev` for runtime;
`uv sync --frozen` for CI/dev. Native Python 3.13 and 3.14 tests; Docker Python 3.13.

Sprint 3 and Sprint 4 later contracts are documented in SPRINT_3_CONTRACTS.md and
SPRINT_4_CONTRACTS.md; the original CLI list above is historical. The public
selected export remains schema2. The data-only bridge envelope has its own
version1 schema and separate private ledger; Discovery SQLite remains schema4.
