"""Integrates collection and storage, isolating errors at the source boundary."""

import logging
import sqlite3
import time

from waagd_discovery.collectors.rss import parse_feed
from waagd_discovery.http import HttpClient
from waagd_discovery.logging import event
from waagd_discovery.models import DiscoveryError, Source
from waagd_discovery.storage import Repository


def collect(
    repository: Repository,
    sources: list[Source],
    client: HttpClient,
    selected: str | None = None,
) -> dict[str, object]:
    repository.sync_sources(sources)
    run_id = repository.start_run()
    started = time.monotonic()
    event("discovery_started", run_id=run_id)
    failures = 0
    successes = 0
    item_errors = 0
    try:
        for configured in sources:
            if not configured.enabled or (selected and configured.id != selected):
                continue
            source = repository.get_source(configured.id)
            event("source_fetch_started", run_id=run_id, source_id=source.id)
            try:
                fetched = client.fetch(source)
                parsed = None if fetched.not_modified else parse_feed(fetched.body, source)
                added, existing, failed = repository.save_source(
                    run_id,
                    source,
                    parsed.items if parsed else [],
                    fetched,
                    skipped=parsed.skipped if parsed else 0,
                )
                successes += 1
                item_errors += failed + (parsed.skipped if parsed else 0)
                event(
                    "source_fetch_completed",
                    run_id=run_id,
                    source_id=source.id,
                    status_code=fetched.status_code,
                    added=added,
                    existing=existing,
                    failed=failed,
                    skipped=parsed.skipped if parsed else 0,
                )
                if added:
                    event("article_inserted", logging.DEBUG, run_id=run_id, count=added)
                if existing:
                    event("article_duplicate", logging.DEBUG, run_id=run_id, count=existing)
            except (DiscoveryError, sqlite3.Error, ValueError) as exc:
                code = exc.code if isinstance(exc, DiscoveryError) else "source_processing_failed"
                repository.fail_source(run_id, source, code)
                failures += 1
                event(
                    "source_fetch_failed",
                    logging.WARNING,
                    run_id=run_id,
                    source_id=source.id,
                    code=code,
                )
    except KeyboardInterrupt:
        result = repository.finish_run(run_id, "interrupted")
        event("discovery_completed", run_id=run_id, status="interrupted")
        return result
    status = (
        "failed"
        if failures and not successes
        else "partial"
        if failures or item_errors
        else "success"
    )
    result = repository.finish_run(run_id, status)
    event(
        "discovery_completed",
        run_id=run_id,
        status=status,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    return result
