"""Offline proof that only trusted, exact corroboration can become VERIFIED."""

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from waagd_discovery.editorial.contracts import VERIFICATION_POLICY, EditorialError
from waagd_discovery.editorial.corroboration import CorroborationClaim, CorroborationPolicy
from waagd_discovery.editorial.transport import FetchDocument
from waagd_discovery.editorial.verifier import SourceVerifier

NOW = datetime(2026, 10, 19, tzinfo=UTC)
ARTICLE = "https://news.example/article"
INDEPENDENT = "https://confirm.example/story"
CLAIM = "The fictional town council approved a new audit on Tuesday."
KEY = hashlib.sha256(ARTICLE.encode()).hexdigest()


def candidate() -> dict[str, Any]:
    return {
        "candidate_key": KEY,
        "url": ARTICLE,
        "title": CLAIM,
        "sources": [{"url": "https://attacker.example/source"}],
    }


class Documents:
    def __init__(self, *, independent: str = CLAIM, redirect: str = INDEPENDENT) -> None:
        self.urls: list[str] = []
        self.independent = independent
        self.redirect = redirect

    def fetch(self, url: str) -> FetchDocument:
        self.urls.append(url)
        if url == ARTICLE:
            return FetchDocument(ARTICLE, 200, (CLAIM + " It is fictional.").encode(), "text/plain")
        assert url == INDEPENDENT
        return FetchDocument(self.redirect, 200, self.independent.encode(), "text/plain")


def policy(independent_url: str = INDEPENDENT) -> CorroborationPolicy:
    return CorroborationPolicy(
        candidate_key=KEY,
        trusted_independent_hosts=frozenset({"confirm.example"}),
        claims=(CorroborationClaim(CLAIM, independent_url),),
    )


def test_default_retrieval_remains_partial_even_when_candidate_supplies_links() -> None:
    documents = Documents()
    result = SourceVerifier(documents, clock=lambda: NOW).verify(
        candidate(), policy_id=VERIFICATION_POLICY
    )
    assert result["verification_status"] == "PARTIAL"
    assert documents.urls == [ARTICLE]
    assert not result["claim_support"]


def test_trusted_exact_independent_corroboration_can_verify() -> None:
    documents = Documents()
    result = SourceVerifier(
        documents, clock=lambda: NOW, corroboration_policies={KEY: policy()}
    ).verify(candidate(), policy_id=VERIFICATION_POLICY)
    assert result["verification_status"] == "VERIFIED"
    assert documents.urls == [ARTICLE, INDEPENDENT]
    assert result["uncertainties"] == []
    assert result["claim_support"] == [
        {
            "claim": CLAIM,
            "evidence_ids": [result["evidence"][0]["id"], result["evidence"][1]["id"]],
            "independently_confirmed": True,
        }
    ]


def test_independent_text_mismatch_stays_partial() -> None:
    result = SourceVerifier(
        Documents(independent="A different statement with no matching claim."),
        corroboration_policies={KEY: policy()},
    ).verify(candidate(), policy_id=VERIFICATION_POLICY)
    assert result["verification_status"] == "PARTIAL"
    assert not result["claim_support"]


def test_redirect_outside_trusted_host_stays_partial() -> None:
    result = SourceVerifier(
        Documents(redirect="https://attacker.example/forged"),
        corroboration_policies={KEY: policy()},
    ).verify(candidate(), policy_id=VERIFICATION_POLICY)
    assert result["verification_status"] == "PARTIAL"
    assert len(result["evidence"]) == 1


def test_related_source_host_cannot_be_independent() -> None:
    documents = Documents()
    related = CorroborationPolicy(
        candidate_key=KEY,
        trusted_independent_hosts=frozenset({"news.example"}),
        claims=(CorroborationClaim(CLAIM, "https://news.example/duplicate"),),
    )
    result = SourceVerifier(documents, corroboration_policies={KEY: related}).verify(
        candidate(), policy_id=VERIFICATION_POLICY
    )
    assert result["verification_status"] == "PARTIAL"
    assert documents.urls == [ARTICLE]


def test_wrong_candidate_key_policy_rejected_at_construction() -> None:
    with pytest.raises(EditorialError, match="corroboration_policy_invalid"):
        SourceVerifier(Documents(), corroboration_policies={"other": policy()})
    with pytest.raises(EditorialError, match="corroboration_policy_invalid"):
        SourceVerifier(Documents(), corroboration_policies={KEY: "not a policy"})  # type: ignore[dict-item]


def test_untrusted_domain_policy_rejected() -> None:
    with pytest.raises(EditorialError, match="corroboration_policy_invalid"):
        policy("https://attacker.example/story")
