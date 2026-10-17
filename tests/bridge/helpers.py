import hashlib
import json
from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def export_document():
    policy = {
        "max_age_hours": 72.0,
        "future_tolerance_minutes": 15.0,
        "min_description_chars": 40,
        "require_publisher": False,
    }
    policy_hash = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 2,
        "generated_at": NOW.isoformat(),
        "selection": {
            "policy_hash": policy_hash,
            "policy": policy,
            "engine_version": "rules-v1",
            "reference_time": NOW.isoformat(),
            "assessment_run_id": "independent-assessment",
            "assessed_at": NOW.isoformat(),
            "assessment_status": "current",
            "counts": {"eligible": 1, "deferred": 0, "excluded": 0, "unassessed": 0, "stale": 0},
        },
        "candidates": [
            {
                "id": 1,
                "url": "https://example.org/article",
                "title": "An unusual and interesting scientific result",
                "description": "Untrusted news description. " * 10,
                "publisher": "Independent source",
                "published_at": (NOW - timedelta(hours=1)).isoformat(),
                "date_status": "valid",
                "first_seen_at": (NOW - timedelta(minutes=30)).isoformat(),
                "last_seen_at": NOW.isoformat(),
                "sources": [{"id": "qa", "name": "QA feed", "url": "https://example.org/rss"}],
                "selection": {
                    "status": "eligible",
                    "assessment_run_id": "independent-assessment",
                    "policy_hash": policy_hash,
                    "reasons": [],
                    "checks": {
                        name: True
                        for name in (
                            "canonical_url",
                            "title",
                            "enabled_source",
                            "description",
                            "publisher",
                            "publication_date",
                            "future_tolerance",
                            "freshness",
                        )
                    },
                },
            }
        ],
    }
