"""Deterministic shortlist validation for a bounded, untrusted AI selection turn.

Selection is editorial preference only. It cannot prove facts, override publisher
identity, reserve a publication quota, or authorize a Publisher write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.trusted_publishers import sensitive_risks

MAX_DAILY_AUTOMATIC = 5
MAX_SHORTLIST = 25


@dataclass(frozen=True)
class SelectionResult:
    shortlist: list[dict[str, str]]
    selected: list[str]
    rejected: dict[str, str]


def _topic_key(candidate: dict[str, Any]) -> str:
    title = str(candidate.get("title") or "").casefold()
    words = [word for word in title.replace("-", " ").split() if len(word) > 3]
    return " ".join(words[:8])


def shortlist(candidates: list[dict[str, Any]]) -> SelectionResult:
    """Make a stable, conservative compact list without model input or side effects."""
    rejected: dict[str, str] = {}
    accepted: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (str(item.get("published_at", "")), str(item.get("candidate_key", ""))),
        reverse=True,
    ):
        key = candidate.get("candidate_key")
        if not isinstance(key, str) or key in rejected:
            continue
        exclusion = candidate.get("selection_exclusion")
        if isinstance(exclusion, str) and exclusion:
            rejected[key] = exclusion
            continue
        if candidate.get("verification_status") != "TRUSTED_PUBLISHER":
            rejected[key] = "not_trusted_publisher"
            continue
        if sensitive_risks(candidate.get("title"), candidate.get("description")):
            rejected[key] = "sensitive_story"
            continue
        topic = _topic_key(candidate)
        if not topic or topic in seen_topics:
            rejected[key] = "duplicate_story"
            continue
        seen_topics.add(topic)
        accepted.append(candidate)
    accepted = accepted[:MAX_SHORTLIST]
    return SelectionResult(
        shortlist=[
            {
                field: str(item.get(field) or "")
                for field in ("candidate_key", "title", "description", "publisher")
            }
            for item in accepted
        ],
        selected=[],
        rejected=rejected,
    )


def shortlist_batches(candidates: list[dict[str, Any]]) -> list[SelectionResult]:
    """Partition the deterministic shortlist into bounded independent Gateway turns.

    The input bridge may contain more eligible items than one model turn can
    safely carry.  Batches preserve deterministic order and contain no more
    than ``MAX_SHORTLIST`` items, while the Controller retains the global daily
    five-item cap before it authorizes any editorial call.
    """

    first = shortlist(candidates)
    if len(candidates) <= MAX_SHORTLIST:
        return [first]
    rejected = dict(first.rejected)
    accepted_keys = {item["candidate_key"] for item in first.shortlist}
    accepted = list(first.shortlist)
    for candidate in sorted(
        candidates,
        key=lambda item: (str(item.get("published_at", "")), str(item.get("candidate_key", ""))),
        reverse=True,
    ):
        key = candidate.get("candidate_key")
        if not isinstance(key, str) or key in rejected or key in accepted_keys:
            continue
        exclusion = candidate.get("selection_exclusion")
        if isinstance(exclusion, str) and exclusion:
            rejected[key] = exclusion
            continue
        if candidate.get("verification_status") != "TRUSTED_PUBLISHER":
            rejected[key] = "not_trusted_publisher"
            continue
        if sensitive_risks(candidate.get("title"), candidate.get("description")):
            rejected[key] = "sensitive_story"
            continue
        topic = _topic_key(candidate)
        if not topic or topic in {_topic_key({"title": item["title"]}) for item in accepted}:
            rejected[key] = "duplicate_story"
            continue
        accepted.append(
            {
                field: str(candidate.get(field) or "")
                for field in ("candidate_key", "title", "description", "publisher")
            }
        )
        accepted_keys.add(key)
    return [
        SelectionResult(accepted[index : index + MAX_SHORTLIST], [], dict(rejected))
        for index in range(0, len(accepted), MAX_SHORTLIST)
    ]


def accept_selection(result: SelectionResult, raw: Any) -> SelectionResult:
    """Validate an untrusted model answer; invalid output selects nothing."""
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "selected_candidate_keys"}:
        raise EditorialError("selection_schema_invalid")
    keys = raw["selected_candidate_keys"]
    allowed = {item["candidate_key"] for item in result.shortlist}
    if (
        raw["schema_version"] != 1
        or not isinstance(keys, list)
        or len(keys) > MAX_DAILY_AUTOMATIC
        or len(set(keys)) != len(keys)
        or not all(isinstance(key, str) and key in allowed for key in keys)
    ):
        raise EditorialError("selection_schema_invalid")
    rejected = dict(result.rejected)
    rejected.update({key: "not_selected" for key in allowed - set(keys)})
    return SelectionResult(result.shortlist, list(keys), rejected)


def selection_log(result: SelectionResult, *, published: int = 0) -> dict[str, Any]:
    """Safe aggregate log; never retains raw model output or article text."""
    if not 0 <= published <= len(result.selected):
        raise EditorialError("selection_log_invalid")
    return {
        "shortlisted": len(result.shortlist),
        "selected": len(result.selected),
        "published": published,
        "rejected": len(result.rejected),
        "reasons": dict(sorted(result.rejected.items())),
    }
