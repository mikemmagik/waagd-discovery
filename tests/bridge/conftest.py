import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from waagd_discovery.bridge import Bridge


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 18, 12, tzinfo=UTC)


def sample_export(now: datetime, count: int = 3) -> dict[str, Any]:
    policy = {
        "max_age_hours": 72.0,
        "future_tolerance_minutes": 15.0,
        "min_description_chars": 40,
        "require_publisher": False,
    }
    hashed = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    run = "c5e9da15-e603-4211-91b0-b93ffb23f920"
    stamp = now.isoformat()
    checks = dict.fromkeys(
        [
            "canonical_url",
            "title",
            "enabled_source",
            "description",
            "publisher",
            "publication_date",
            "future_tolerance",
            "freshness",
        ],
        True,
    )
    return {
        "schema_version": 2,
        "generated_at": stamp,
        "selection": {
            "policy": policy,
            "policy_hash": hashed,
            "engine_version": "rules-v1",
            "reference_time": stamp,
            "assessment_run_id": run,
            "assessed_at": stamp,
            "assessment_status": "current",
            "counts": {
                "eligible": count,
                "deferred": 0,
                "excluded": 0,
                "stale": 0,
                "unassessed": 0,
            },
        },
        "candidates": [
            {
                "id": i + 1,
                "title": f"Story {i}",
                "url": f"https://example.test/article/{i}",
                "publisher": "Example",
                "description": "Description of an unusual news item. " * 4,
                "published_at": (now - timedelta(minutes=30)).isoformat(),
                "date_status": "valid",
                "first_seen_at": stamp,
                "last_seen_at": stamp,
                "sources": [
                    {"id": "source", "name": "Example", "url": "https://example.test/feed"}
                ],
                "selection": {
                    "status": "eligible",
                    "assessment_run_id": run,
                    "policy_hash": hashed,
                    "reasons": [],
                    "checks": checks,
                },
            }
            for i in range(count)
        ],
    }


@pytest.fixture
def bridge(tmp_path: Path, now: datetime) -> Bridge:
    root = tmp_path.resolve()
    export = root / "exports"
    export.mkdir()
    workspace = root / "fake-workspace"
    workspace.mkdir()
    path = export / "candidates.json"
    path.write_text(json.dumps(sample_export(now)))
    return Bridge(path, workspace, root / "bridge-state")
