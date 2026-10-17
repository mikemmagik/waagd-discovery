# Configuration

The trusted `config/sources.yaml` registry supports 1–100 uniquely identified
RSS/Atom sources. Keys: id, name, url, type (rss/atom), enabled (boolean).
Unknown keys and credentials in URLs are rejected. Source changes are synced at
collection; changed feed URLs must discard old conditional validators. Removed
sources are retained for provenance but disabled. Disabled sources are not fetched.

Environment: WAAGD_DB defaults to /data/discovery.sqlite3, WAAGD_SOURCES to
config/sources.yaml. CLI --db/--sources override these. --log-level selects DEBUG,
INFO, WARNING or ERROR. No secrets or AI API keys are needed.

HTTP defaults: connect 5s, read 20s, 2MiB response, 5 redirects, 2 retries,
exponential backoff 0.5s, Retry-After capped at 30s. These typed settings can be
changed in code; environment overrides for advanced tuning are deferred.
Feeds are fetched sequentially to minimize upstream load. Only explicitly
configured feeds are fetched, never the linked articles.

Export schema lives in src/waagd_discovery/candidate.schema.json and is bundled
in the wheel. --limit is 1–500, default 50. Export contains latest discovery
metadata and provenance only, no complete article bodies, secrets or local paths.
Publication date is distinct from event date. Unknown dates remain null.
