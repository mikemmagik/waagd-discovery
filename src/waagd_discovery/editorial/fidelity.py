"""Fail-closed source-fidelity checks for untrusted creative editorial text.

This reviewer does not establish that a news report is true.  It only prevents a
READY_FOR_REVIEW result when an editorial response makes one of the explicit,
high-risk factual claims that is absent from, or contradicts, the supplied
candidate and verification evidence.  It returns diagnostic *codes* only; raw
source material and model output never enter durable state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from waagd_discovery.editorial.contracts import EditorialError, validate_request

_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")

# These phrases are factual assertions rather than an absurdist framing.  The
# writer may still use satire, but it must not invent a plan, statement, policy
# change, or anonymous report around a real institution.
_ATTRIBUTION = re.compile(
    r"\b(?:reportedly|sources\s+say|officials\s+(?:intend|plan|say|said|announce|announced)|"
    r"the\s+administration\s+(?:intends?|plans?|will)|"
    r"[A-Z][A-Za-z0-9&.-]*(?:\s+[A-Z][A-Za-z0-9&.-]*){0,3}\s+"
    r"(?:intends?|plans?|will|said|announced|confirmed|admits?|admitted))\b"
)

# The two pairs below encode known misleading transformations observed in the
# canary.  They remain narrow so a legitimate source phrase is never banned
# merely because an article is political or satirical.
_CONTRADICTIONS = (
    (
        re.compile(r"\breasonable\s+(?:guest\s+)?chores?\b", re.IGNORECASE),
        re.compile(r"\bsignificant(?:\s+effort)?\b", re.IGNORECASE),
    ),
    (
        re.compile(r"\bstalled\s+(?:tesla[\s/-]+)?(?:spacex[\s/-]+)?merger\b", re.IGNORECASE),
        re.compile(r"\bexploratory\s+merger\b", re.IGNORECASE),
    ),
)

_TIMING = re.compile(
    r"\b(?:new|newly|just|now)\s+(?:cleaning[- ]fee\s+)?(?:policy|rule|change)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceFidelityReview:
    """A safe review result; ``code`` deliberately never contains model text."""

    code: str | None = None

    @property
    def accepted(self) -> bool:
        return self.code is None


class SourceFidelityReviewer:
    """Compare bounded factual assertion markers with supplied source evidence.

    It intentionally has no network, filesystem, model, or persistence access.
    More complex semantic fact checking remains an operator-reviewed concern;
    this gate is a fail-closed guard against the concrete unsupported claims it
    can identify from the material already admitted to the editorial request.
    """

    def review(self, request: dict[str, Any], decision: dict[str, Any]) -> SourceFidelityReview:
        validate_request(request)
        if decision.get("decision") != "READY_FOR_REVIEW":
            return SourceFidelityReview()
        creative = " ".join(
            value
            for key in ("satirical_angle", "headline", "summary", "article")
            if isinstance((value := decision.get(key)), str)
        )
        evidence = self._evidence_text(request)
        normalized_evidence = _normalize(evidence)
        normalized_creative = _normalize(creative)

        for generated, supported in _CONTRADICTIONS:
            if generated.search(creative) and supported.search(evidence):
                return SourceFidelityReview("editorial_fidelity_contradiction")

        # A phrase that calls a policy "new" is an assertion about timing.  It
        # is allowed only where the supplied material itself carries a matching
        # timing claim rather than merely mentioning the policy.
        if _TIMING.search(creative) and not _TIMING.search(evidence):
            return SourceFidelityReview("editorial_fidelity_misleading_timing")

        for match in _ATTRIBUTION.finditer(creative):
            marker = _normalize(match.group(0))
            if marker and marker not in normalized_evidence:
                return SourceFidelityReview("editorial_fidelity_unsupported_attribution")

        # Do not reject satire for a vocabulary overlap alone.  This compact
        # marker guards only a common unsupported "merger stalled" assertion
        # when the source describes the proposal as exploratory.
        if (
            "stalled" in normalized_creative
            and "merger" in normalized_creative
            and "exploratory" in normalized_evidence
            and "stalled" not in normalized_evidence
        ):
            return SourceFidelityReview("editorial_fidelity_contradiction")
        return SourceFidelityReview()

    @staticmethod
    def _evidence_text(request: dict[str, Any]) -> str:
        candidate = request["candidate"]
        verification = request["verification"]
        parts = [candidate["title"], candidate["description"]]
        parts.extend(item["excerpt"] for item in verification["evidence"])
        parts.extend(item["claim"] for item in verification["claim_support"])
        return "\n".join(parts)


def _normalize(value: str) -> str:
    return " ".join(_WORD.findall(_WHITESPACE.sub(" ", value).lower()))


def require_source_fidelity(request: dict[str, Any], decision: dict[str, Any]) -> None:
    """Raise only a safe, taxonomy-owned code when the review rejects text."""

    result = SourceFidelityReviewer().review(request, decision)
    if result.code is not None:
        raise EditorialError(result.code)
