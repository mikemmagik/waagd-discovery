# Deterministic preliminary filtering — engine 1

Filtering establishes technical eligibility for future editorial review. It does
not assess truth, satire, political interest or publication approval. There are
no keywords, topics, embeddings, network requests or LLM calls.

`config/filter.yaml` is operator-owned configuration mounted read-only. Defaults:
max_age_hours 72, future_tolerance_minutes 15, min_description_chars 40,
require_publisher false. Missing keys use defaults; unknown keys, nonfinite
numbers, wrong types and recursive/deep invalid YAML fail safely. Lower the
description threshold to zero for feeds that legitimately omit descriptions;
the operator can reconsider every decision without recollecting or losing data.

Each article receives all eight check outcomes in stable order: canonical URL,
title, enabled source, description, optional publisher, publication date, future
tolerance and freshness. Missing metadata/date is deferred; stale materials or
those with no enabled provenance source are excluded. Other valid recent records
are eligible. An article can move between all three states on reassessment.
All failed reason codes are retained; an eligible article has an empty failure
list and all successful checks. The checks and policy values explain eligibility.

## Persisted review and repeatability

```sh
docker compose run --rm discovery filter
docker compose run --rm discovery filter-report --limit 100
docker compose run --rm discovery filter --at 2026-10-17T12:00:00Z
```

`filter` syncs the current trusted registry, then evaluates the complete database
using one aware UTC reference timestamp. Each complete batch stores the full
policy/hash, engine version, actual assessment time, chosen reference time,
input fingerprints and per-article check results/reasons. Batches/decisions are
immutable. The latest complete batch defines current state. Cancellation or a
failure rolls back the whole new batch; the previous batch remains current.
Articles, source links, aliases, GUIDs, discovery runs and original observations
are not deleted or rewritten by filtering. Identical inputs, policy and reference
time reproduce the same decisions and candidate order; audit batch IDs and actual
wall-clock assessment timestamps can differ.

Normal `collect --once` assesses after ingestion, including partial source runs.
Dry-run assesses its memory snapshot only. An interrupted collection retains
committed discoveries and skips assessment; run `filter` after recovery.
After an upgrade from v0.1, run `filter` before expecting selected exports.

## Selected export v2

```sh
docker compose run --rm discovery export --output /exports/candidates.json --limit 50
docker compose run --rm discovery export --output /exports/replay.json --at 2026-10-17T12:00:00Z
```

The export reads a consistent memory snapshot and syncs current registry settings
there. It never modifies the working database or its schema. It considers every
article before limiting, orders eligible candidates by publication time descending,
metadata completeness descending and article ID ascending, then returns 1–500
at most (default 50). Unknown publication dates do not get fabricated timestamps.

Every returned article must have an eligible decision in the latest complete
persisted batch, a matching policy/engine/input fingerprint, and pass all checks
at export reference time. Metadata/source/policy changes cannot silently reuse
old eligibility. A 304 does not keep an old story fresh: age is always rechecked.
New or changed material needs a new `filter`/collection assessment. A policy change
requires reassessment even if it relaxes a threshold. Returning zero candidates
is valid and explicit; the context distinguishes unassessed, stale and incompatible
decisions and reports counts before the export limit.

The top-level selection context contains full policy/hash, engine version,
reference time, persisted assessment ID/time and counts. Each candidate carries
its own eligible decision/checks and assessment reference, alongside the existing
ID, canonical URL, bounded metadata and provenance. All text remains untrusted
data. JSON is schema-validated, fsynced and atomically renamed, preserving prior
output on validation/write/rename failure. Database, policy and registry paths
are protected from accidental export replacement.

The wire schema intentionally changes from raw discoveries (v1) to selected
candidates (v2), not from verified to approved news. The current schema is
src/waagd_discovery/candidate-v2.schema.json; candidate.schema.json is the retained
historical v1 schema. No consumer integration is enabled in this sprint.
