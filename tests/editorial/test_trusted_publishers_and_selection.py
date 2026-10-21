from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waagd_discovery.editorial.contracts import EditorialError, make_request, trusted_decision
from waagd_discovery.editorial.selection import accept_selection, selection_log, shortlist
from waagd_discovery.editorial.trusted_publishers import load_trusted_publishers
from waagd_discovery.editorial.verifier import FetchDocument, SourceVerifier

NOW = datetime(2026, 10, 22, tzinfo=UTC)
URL = "https://www.theguardian.com/world/2026/oct/22/story"
KEY = hashlib.sha256(URL.encode()).hexdigest()


class Documents:
    def fetch(self, value: str) -> FetchDocument:
        assert value == URL
        return FetchDocument(
            URL, 200, b"<p>A calm institutional report about a policy decision.</p>", "text/html"
        )


def candidate(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "candidate_key": KEY,
        "id": 1,
        "url": URL,
        "title": "A policy decision",
        "description": "A report about a policy decision.",
        "publisher": "The Guardian",
        "published_at": NOW.isoformat(),
        "sources": [
            {
                "id": "guardian_world",
                "name": "The Guardian World",
                "url": "https://www.theguardian.com/world/rss",
            }
        ],
    }
    return {**value, **changes}


def test_trusted_publisher_is_identity_not_independent_verification() -> None:
    policy = load_trusted_publishers(Path("config/trusted_publishers.yaml"))
    verification = SourceVerifier(Documents(), clock=lambda: NOW, trusted_publishers=policy).verify(
        candidate(), policy_id="source-verification-v1"
    )
    assert verification["verification_status"] == "TRUSTED_PUBLISHER"
    assert verification["claim_support"] == []
    assert verification["publisher_trust"] == {
        "publisher_id": "guardian",
        "rss_url": "https://www.theguardian.com/world/rss",
    }
    request = make_request(candidate(), verification, simulation=False)
    creative = {
        "satirical_angle": "A committee celebrates process.",
        "headline": "A Committee Celebrates Process",
        "summary": "The forms are pleased with their progress.",
        "article": " ".join(
            [
                "Institutions admire paperwork while the policy waits politely "
                "outside the meeting room."
            ]
            * 12
        ),
    }
    decision = trusted_decision(request, creative=creative, now=NOW)
    assert decision["decision"] == "READY_FOR_REVIEW"
    assert decision["unverified_claims"]
    assert "independently" in decision["factual_summary"]


def test_untrusted_redirect_and_sensitive_story_remain_partial() -> None:
    policy = load_trusted_publishers(Path("config/trusted_publishers.yaml"))
    assert (
        SourceVerifier(Documents(), trusted_publishers=policy).verify(
            candidate(title="A sexual assault report"), policy_id="source-verification-v1"
        )["verification_status"]
        == "PARTIAL"
    )
    assert (
        SourceVerifier(Documents(), trusted_publishers=policy).verify(
            candidate(
                sources=[
                    {"id": "other", "name": "Other", "url": "https://www.theguardian.com/world/rss"}
                ]
            ),
            policy_id="source-verification-v1",
        )["verification_status"]
        == "PARTIAL"
    )


def test_selection_is_bounded_explainable_and_fail_closed() -> None:
    first = {**candidate(), "verification_status": "TRUSTED_PUBLISHER"}
    second = {
        **candidate(
            candidate_key="b" * 64,
            title="A policy decision",
            url="https://arstechnica.com/gadgets/story",
        ),
        "verification_status": "TRUSTED_PUBLISHER",
    }
    unsafe = {
        **candidate(candidate_key="c" * 64, title="A child report"),
        "verification_status": "TRUSTED_PUBLISHER",
    }
    result = shortlist([first, second, unsafe])
    assert len(result.shortlist) == 1
    selected_key = result.shortlist[0]["candidate_key"]
    chosen = accept_selection(
        result, {"schema_version": 1, "selected_candidate_keys": [selected_key]}
    )
    assert selection_log(chosen)["selected"] == 1
    assert selection_log(chosen)["published"] == 0
    assert selection_log(chosen)["rejected"] == 2
    with pytest.raises(EditorialError, match="selection_schema_invalid"):
        accept_selection(result, {"schema_version": 1, "selected_candidate_keys": ["x" * 64]})


def test_trusted_publisher_provenance_uses_exact_matched_rss_not_same_id_decoy() -> None:
    """A duplicate source ID cannot make audit provenance point at an untrusted feed."""
    policy = load_trusted_publishers(Path("config/trusted_publishers.yaml"))
    verification = SourceVerifier(Documents(), clock=lambda: NOW, trusted_publishers=policy).verify(
        candidate(
            sources=[
                {
                    "id": "guardian_world",
                    "name": "Decoy",
                    "url": "https://attacker.example/guardian-world.rss",
                },
                {
                    "id": "guardian_world",
                    "name": "The Guardian World",
                    "url": "https://www.theguardian.com/world/rss",
                },
            ]
        ),
        policy_id="source-verification-v1",
    )
    assert verification["verification_status"] == "TRUSTED_PUBLISHER"
    assert verification["publisher_trust"]["rss_url"] == "https://www.theguardian.com/world/rss"
