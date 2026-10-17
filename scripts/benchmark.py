#!/usr/bin/env python3
"""Offline acceptance workload; run on a disposable, constrained Docker volume."""

import argparse
import json
import platform
import resource
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from waagd_discovery.export import export_candidates
from waagd_discovery.filtering import FilterPolicy, assess, select_candidates
from waagd_discovery.models import FetchResult, Source
from waagd_discovery.operations import restore, snapshot
from waagd_discovery.pipeline import collect
from waagd_discovery.storage import Repository

REFERENCE = datetime(2026, 10, 17, 12, tzinfo=UTC)


def memory() -> dict[str, int]:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        peak //= 1024
    current = 0
    if Path("/proc/self/status").exists():
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                current = int(line.split()[1])
    result = {"peak_rss_kib": int(peak), "current_rss_kib": current}
    for metric in ("current", "peak"):
        path = Path(f"/sys/fs/cgroup/memory.{metric}")
        if path.exists():
            result[f"cgroup_memory_{metric}_bytes"] = int(path.read_text().strip())
    return result


class FixtureClient:
    def __init__(self, count: int, description_size: int) -> None:
        self.count = count
        self.description = "Synthetic untrusted feed metadata. " * (description_size // 35 + 1)
        self.description = self.description[:description_size]
        self.update = False

    def fetch(self, source: Source) -> FetchResult:
        offset = int(source.id.rsplit("_", 1)[1]) * 1000
        entries = []
        for identity in range(offset, min(offset + 1000, self.count)):
            description = "short" if identity % 23 == 0 else self.description
            publication = REFERENCE - timedelta(hours=identity % 96)
            date = "" if identity % 19 == 0 else f"<pubDate>{publication.isoformat()}</pubDate>"
            title = f"Synthetic technical discovery {identity}"
            if self.update and offset == 0:
                title = "Updated " + title
            entries.append(
                f"<item><guid>fixture-{identity}</guid><title>{title}</title>"
                f"<link>https://example.org/news/{identity}?utm_source=fixture</link>"
                f"<description>{description}</description>{date}</item>"
            )
        return FetchResult(
            200, ("<rss version='2.0'><channel>" + "".join(entries) + "</channel></rss>").encode()
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--articles", type=int, default=10_000)
    parser.add_argument("--description-size", type=int, default=800)
    args = parser.parse_args()
    if not 1000 <= args.articles <= 100_000 or not 40 <= args.description_size <= 3500:
        parser.error("articles must be 1000..100000 and description size 40..3500")
    args.directory.mkdir(parents=True, exist_ok=True)
    database = args.directory / "load.sqlite3"
    if database.exists():
        parser.error("Use an empty disposable directory; benchmark never resets an existing DB")
    phases: list[dict[str, object]] = []

    def record(name: str, started: float, **detail: object) -> None:
        phases.append(
            {"phase": name, "seconds": round(time.monotonic() - started, 4), **memory(), **detail}
        )

    total = time.monotonic()
    sources = [
        Source(f"bench_{i:03}", f"Benchmark {i}", f"https://example.org/feed/{i}")
        for i in range((args.articles + 999) // 1000)
    ]
    client = FixtureClient(args.articles, args.description_size)
    policy = FilterPolicy()
    with Repository(database) as repository:
        started = time.monotonic()
        first = collect(repository, sources, client)  # type: ignore[arg-type]
        assert first["status"] == "success" and first["items_added"] == args.articles
        record("collect_parse_normalize_store", started, items=first["items_received"])

        started = time.monotonic()
        assessment = assess(repository, policy, REFERENCE)
        selected, selection = select_candidates(repository, policy, REFERENCE, 50)
        assert selected and all(row["selection"]["status"] == "eligible" for row in selected)
        first_ids = [row["id"] for row in selected]
        record("filter_and_select", started, assessment=assessment, selection=selection)

        started = time.monotonic()
        replay = collect(repository, sources, client)  # type: ignore[arg-type]
        assert replay["items_added"] == 0 and replay["items_existing"] == args.articles
        record("replay_dedup", started, existing=replay["items_existing"])

        started = time.monotonic()
        assess(repository, policy, REFERENCE)
        selected, _ = select_candidates(repository, policy, REFERENCE, 50)
        assert [row["id"] for row in selected] == first_ids
        record("repeat_filter_same_inputs", started)

        client.update = True
        started = time.monotonic()
        replay = collect(repository, sources, client)  # type: ignore[arg-type]
        assert replay["items_added"] == 0
        _, stale = select_candidates(repository, policy, REFERENCE, 50)
        assess(repository, policy, REFERENCE)
        record("metadata_update_and_reassessment", started, prior_selection=stale)

        started = time.monotonic()
        with snapshot(database) as reader:
            count = export_candidates(
                reader, args.directory / "candidates.json", 50, policy=policy, as_of=REFERENCE
            )
        assert count == 50
        payload = json.loads((args.directory / "candidates.json").read_text())
        assert payload["schema_version"] == 2
        record("snapshot_validate_atomic_export", started, exported=count)

        started = time.monotonic()
        repository.backup(args.directory / "verified.sqlite3")
        record("sqlite_online_backup_verify", started)

        repository.connection.execute("BEGIN IMMEDIATE")
        with closing(sqlite3.connect(database, timeout=0.05)) as contender:
            started = time.monotonic()
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
            else:
                raise AssertionError("Expected competing writer to wait and time out")
            finally:
                repository.connection.rollback()
            record("sqlite_competing_writer_timeout", started)
            contender.execute("BEGIN IMMEDIATE")
            contender.rollback()

        repository.verify()
        stats = repository.stats()
        assert stats["articles"] == args.articles and stats["observations"] == args.articles * 3
        page_size = repository.connection.execute("PRAGMA page_size").fetchone()[0]
        pages = repository.connection.execute("PRAGMA page_count").fetchone()[0]
        wal = Path(str(database) + "-wal")
        sqlite_result = {
            "journal_mode": repository.connection.execute("PRAGMA journal_mode").fetchone()[0],
            "page_size": page_size,
            "page_count": pages,
            "logical_bytes": page_size * pages,
            "wal_bytes_before_checkpoint": wal.stat().st_size,
            "checkpoint": list(
                repository.connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            ),
            "url_lookup_plan": [
                list(row)
                for row in repository.connection.execute(
                    "EXPLAIN QUERY PLAN SELECT article_id FROM url_aliases WHERE url=?",
                    ("https://example.org/news/500",),
                )
            ],
        }
    started = time.monotonic()
    restore(args.directory / "restored.sqlite3", args.directory / "verified.sqlite3")
    with Repository(args.directory / "restored.sqlite3") as restored:
        restored.verify()
        assert restored.stats() == stats
    record("restore_and_integrity", started)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(
        json.dumps(
            {
                "workload": {
                    "articles": args.articles,
                    "observations": args.articles * 3,
                    "description_chars": args.description_size,
                    "sources": len(sources),
                },
                "platform": platform.platform(),
                "architecture": platform.machine(),
                "python": platform.python_version(),
                "reference_time": REFERENCE.isoformat(),
                "total_seconds": round(time.monotonic() - total, 4),
                "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 4),
                **memory(),
                "stats": stats,
                "sqlite": sqlite_result,
                "phases": phases,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
