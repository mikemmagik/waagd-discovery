"""Offline source-fidelity gate tests; fixtures contain no live reporting."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from waagd_discovery.editorial.contracts import EditorialError, make_request, trusted_decision
from waagd_discovery.editorial.fidelity import SourceFidelityReviewer, require_source_fidelity

NOW = datetime(2026, 10, 22, tzinfo=UTC).isoformat()
URL = "https://news.example.test/source-fidelity"
KEY = hashlib.sha256(URL.encode()).hexdigest()


def request(*, title: str, description: str, excerpt: str) -> dict[str, Any]:
    candidate = {
        "candidate_key": KEY,
        "id": 1,
        "url": URL,
        "title": title,
        "description": description,
        "publisher": "Fixture News",
        "published_at": NOW,
        "sources": [{"id": "fixture", "name": "Fixture", "url": "https://news.example.test/rss"}],
    }
    verification = {
        "schema_version": 1,
        "candidate_key": KEY,
        "verification_status": "VERIFIED",
        "canonical_url": URL,
        "fetched_at": NOW,
        "http_status": 200,
        "publisher": "Fixture News",
        "publication_date": None,
        "event_date": None,
        "evidence": [
            {
                "id": "a" * 64,
                "url": URL,
                "retrieved_at": NOW,
                "sha256": "b" * 64,
                "excerpt": excerpt,
                "source_kind": "article",
            },
            {
                "id": "c" * 64,
                "url": "https://independent.example.test/evidence",
                "retrieved_at": NOW,
                "sha256": "d" * 64,
                "excerpt": excerpt,
                "source_kind": "independent",
            },
        ],
        "uncertainties": [],
        "errors": [],
        "claim_support": [
            {
                "claim": excerpt,
                "evidence_ids": ["a" * 64, "c" * 64],
                "independently_confirmed": True,
            }
        ],
    }
    return make_request(candidate, verification, simulation=False)


def decision(value: dict[str, Any], *, article: str) -> dict[str, Any]:
    creative = {
        "satirical_angle": "Institutions treat process as a destination.",
        "headline": "A Fixture Institution Finds a New Form",
        "summary": "The paperwork celebrates an outcome that the source actually describes.",
        "article": article,
    }
    return trusted_decision(value, creative=creative, now=datetime(2026, 10, 22, tzinfo=UTC))


def article(extra: str = "") -> str:
    text = (
        "The institution placed another form on the desk and called the resulting queue a "
        "strategy. "
        "Its clerks admired the process, then scheduled a meeting to decide whether the meeting "
        "needed a committee. Nobody claimed the paperwork solved the underlying problem. The joke "
        "was the paperwork itself, which became more confident each time it produced another "
        "folder. "
        "The file waited politely while the office celebrated its ability to label the waiting. "
        "By sunset, the system had achieved a complete inventory of its own hesitation. "
    )
    return text + extra


@pytest.mark.parametrize(
    ("title", "description", "excerpt", "generated", "code"),
    [
        (
            "Guests describe chores requiring significant effort",
            "The report describes substantial guest chores, not a policy change.",
            "Guests said the checklist required significant effort. The cleaning-fee rule was "
            "older.",
            "The reasonable chores under the new cleaning-fee policy received another form.",
            "editorial_fidelity_contradiction",
        ),
        (
            "Companies explore a possible merger",
            "The report calls the merger exploratory.",
            "The parties described exploratory merger discussions and did not say talks had "
            "stalled.",
            "The stalled Tesla SpaceX merger received a ceremonial filing cabinet.",
            "editorial_fidelity_contradiction",
        ),
        (
            "Organisation releases its annual filing",
            "The filing contains no future policy statement.",
            "The organisation published an annual filing without announcing a plan.",
            "Acme plans a sudden policy change, reportedly, according to sources say.",
            "editorial_fidelity_unsupported_attribution",
        ),
    ],
)
def test_source_fidelity_rejects_observed_unsupported_transformations(
    title: str, description: str, excerpt: str, generated: str, code: str
) -> None:
    value = request(title=title, description=description, excerpt=excerpt)
    result = decision(value, article=article(generated))

    review = SourceFidelityReviewer().review(value, result)

    assert review.code == code
    with pytest.raises(EditorialError, match=f"^{code}$"):
        require_source_fidelity(value, result)


def test_source_fidelity_allows_commentary_without_new_factual_assertion() -> None:
    value = request(
        title="Office publishes a routine filing",
        description="The article describes a filing.",
        excerpt="The office published a routine filing.",
    )
    result = decision(value, article=article())

    assert SourceFidelityReviewer().review(value, result).accepted


def test_source_fidelity_is_not_a_claim_of_independent_fact_checking() -> None:
    value = request(
        title="Office publishes a routine filing",
        description="The article describes a filing.",
        excerpt="The office published a routine filing.",
    )
    result = decision(value, article=article())

    assert SourceFidelityReviewer.__doc__ is not None
    assert "no network, filesystem, model, or persistence access" in SourceFidelityReviewer.__doc__
    assert SourceFidelityReviewer().review(value, result).code is None


@pytest.mark.parametrize("schema_version", [True, 1.0, False])
def test_gateway_fidelity_rejects_noninteger_schema_version(schema_version: object) -> None:
    from waagd_discovery.editorial.fidelity import GatewayFidelityReviewer

    value = request(
        title="Office publishes a routine filing",
        description="The article describes a filing.",
        excerpt="The office published a routine filing.",
    )
    result = decision(value, article=article())

    class Gateway:
        def review_fidelity(self, payload: object) -> object:
            del payload
            return {"schema_version": schema_version, "verdict": "PASS", "findings": []}

    review = GatewayFidelityReviewer(Gateway()).review(value, result)
    assert review.code == "editorial_fidelity_invalid_schema"
