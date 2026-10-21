"""Offline Site-v2 compatibility checks for trusted publisher exports."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waagd_discovery.editorial.contracts import EditorialError, make_request, trusted_decision
from waagd_discovery.editorial.publication import (
    _content_hash,
    _payload,
    validate_publication_intent,
)
from waagd_discovery.editorial.publication_policy import PublicationPolicy

ARTICLE_URL = "https://www.theguardian.com/world/2026/oct/22/fixture"
KEY = hashlib.sha256(ARTICLE_URL.encode()).hexdigest()
NOW = datetime.now(UTC).isoformat()


def candidate() -> dict[str, object]:
    return {
        "candidate_key": KEY,
        "id": 1,
        "url": ARTICLE_URL,
        "title": "A fixture policy decision",
        "description": "A synthetic report for offline contract testing.",
        "publisher": "The Guardian",
        "published_at": NOW,
        "sources": [
            {
                "id": "guardian_world",
                "name": "The Guardian World",
                "url": "https://www.theguardian.com/world/rss",
            }
        ],
    }


def verification() -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate_key": KEY,
        "verification_status": "TRUSTED_PUBLISHER",
        "canonical_url": ARTICLE_URL,
        "fetched_at": NOW,
        "http_status": 200,
        "publisher": "The Guardian",
        "publication_date": None,
        "event_date": None,
        "evidence": [
            {
                "id": "a" * 64,
                "url": ARTICLE_URL,
                "retrieved_at": NOW,
                "sha256": "b" * 64,
                "excerpt": "Synthetic fixture article text.",
                "source_kind": "article",
            }
        ],
        "uncertainties": ["Publisher identity is trusted; facts are not independently verified."],
        "errors": [],
        "claim_support": [],
        "publisher_trust": {
            "publisher_id": "guardian",
            "rss_url": "https://www.theguardian.com/world/rss",
        },
    }


def decision() -> dict[str, object]:
    request = make_request(candidate(), verification(), simulation=False)
    creative = {
        "satirical_angle": "A committee discovers procedure.",
        "headline": "A Committee Discovers Procedure",
        "summary": "The paperwork waits patiently for the paperwork.",
        "article": " ".join(["The institution filed another meeting and called it progress."] * 12),
    }
    return trusted_decision(request, creative=creative, now=datetime.now(UTC))


def policy() -> PublicationPolicy:
    return PublicationPolicy(automatic_enabled=True)


def test_trusted_publisher_exports_site_v2_with_honest_provenance() -> None:
    intent = _payload(candidate(), verification(), decision(), "delivery", 1, policy())
    assert intent["version"] == 2
    assert intent["editorial"]["review_route"] == "trusted_publisher"
    assert intent["editorial"]["trusted_publisher"] == {
        "publisher_id": "guardian",
        "publisher_name": "The Guardian",
        "trusted_rss_url": "https://www.theguardian.com/world/rss",
        "article_url": ARTICLE_URL,
        "article_host": "www.theguardian.com",
        "verified_at": intent["editorial"]["trusted_publisher"]["verified_at"],
        "safety_gates": {
            "publisher_identity": True,
            "safe_retrieval": True,
            "sensitive_content": "clear",
        },
    }
    assert intent["post"]["canonical_url"] == ARTICLE_URL
    assert intent["post"]["source"] == "The Guardian"
    validate_publication_intent(intent)


def test_site_v2_hash_vectors_match_python_producer() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "site-v2"
    for name in (
        "publication-intent-v2-trusted-publisher.json",
        "publication-intent-v2-independently-verified.json",
    ):
        intent = json.loads((fixture_dir / name).read_text(encoding="utf-8"))
        assert _content_hash(intent) == intent["content_hash"]
        validate_publication_intent(intent)


def test_tampered_v2_provenance_is_rejected() -> None:
    intent = _payload(candidate(), verification(), decision(), "delivery", 1, policy())
    intent["editorial"]["trusted_publisher"]["article_host"] = "attacker.example"
    intent["content_hash"] = _content_hash(intent)
    with pytest.raises(EditorialError, match="publication_schema_invalid"):
        validate_publication_intent(intent)


def test_independently_verified_route_remains_v1() -> None:
    from test_independent_contracts import candidate as verified_candidate
    from test_independent_contracts import decision as verified_decision
    from test_independent_contracts import inbox, verification as verified_result

    ready = verified_decision("READY_FOR_REVIEW")
    ready["risks"] = []
    intent = _payload(
        verified_candidate(),
        verified_result("VERIFIED"),
        ready,
        inbox()["delivery_id"],
        1,
        policy(),
    )
    assert intent["version"] == 1
