"""Bounded identity policy for publisher-owned RSS reports.

This module establishes *who published a report*, never whether its factual
claims are independently true.  It accepts only explicitly configured RSS
origins and final article URLs; feeds and article content remain untrusted data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

import yaml

from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.normalization import normalize_url


@dataclass(frozen=True)
class TrustedPublisher:
    id: str
    name: str
    source_ids: frozenset[str]
    rss_urls: frozenset[str]
    article_hosts: frozenset[str]


@dataclass(frozen=True)
class TrustedPublisherMatch:
    publisher: TrustedPublisher
    rss_url: str


@dataclass(frozen=True)
class TrustedPublisherPolicy:
    publishers: tuple[TrustedPublisher, ...]

    def match(self, candidate: dict[str, Any], canonical_url: str) -> TrustedPublisherMatch | None:
        """Return the exact allowlisted feed binding for an HTTPS final URL."""
        try:
            parsed = _https(canonical_url)
            sources = candidate["sources"]
            if not isinstance(sources, list):
                return None
            for publisher in self.publishers:
                if parsed.hostname not in publisher.article_hosts:
                    continue
                for source in sources:
                    if (
                        isinstance(source, dict)
                        and source.get("id") in publisher.source_ids
                        and isinstance(source.get("url"), str)
                        and _normalized_rss(source["url"]) in publisher.rss_urls
                    ):
                        return TrustedPublisherMatch(publisher, _normalized_rss(source["url"]))
        except (KeyError, TypeError, ValueError):
            return None
        return None


def _https(value: str) -> SplitResult:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or len(value) > 2048
    ):
        raise ValueError
    return parsed


def _normalized_rss(value: str) -> str:
    _https(value)
    return normalize_url(value)


def _publisher(value: Any) -> TrustedPublisher:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "name",
        "source_ids",
        "rss_urls",
        "article_hosts",
    }:
        raise EditorialError("trusted_publishers_policy_invalid")
    identifier, name, source_ids, rss_urls, article_hosts = (
        value["id"],
        value["name"],
        value["source_ids"],
        value["rss_urls"],
        value["article_hosts"],
    )
    if (
        not isinstance(identifier, str)
        or re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", identifier) is None
        or not isinstance(name, str)
        or not 1 <= len(name) <= 100
        or not all(
            isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", item)
            for item in source_ids
        )
        or not all(isinstance(item, str) for item in rss_urls)
        or not all(
            isinstance(item, str) and re.fullmatch(r"[a-z0-9.-]{1,253}", item)
            for item in article_hosts
        )
        or not source_ids
        or not rss_urls
        or not article_hosts
    ):
        raise EditorialError("trusted_publishers_policy_invalid")
    return TrustedPublisher(
        identifier,
        name,
        frozenset(source_ids),
        frozenset(_normalized_rss(item) for item in rss_urls),
        frozenset(item.lower().rstrip(".") for item in article_hosts),
    )


def load_trusted_publishers(path: Path) -> TrustedPublisherPolicy:
    """Load a non-secret, strict allowlist. Unknown/malformed entries fail closed."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"trusted_publishers"}:
            raise ValueError
        body = data["trusted_publishers"]
        if not isinstance(body, dict) or set(body) != {"publishers"}:
            raise ValueError
        entries = body["publishers"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
            raise ValueError
        publishers = tuple(_publisher(item) for item in entries)
        if len({item.id for item in publishers}) != len(publishers):
            raise ValueError
        return TrustedPublisherPolicy(publishers)
    except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError):
        raise EditorialError("trusted_publishers_policy_unreadable") from None


# These deterministic categories keep high-risk stories out of automated satire.
# They deliberately do not include political subjects as such.
_SENSITIVE = {
    "sexual_violence": re.compile(
        r"\b(?:rape|raped|sexual assault|sexual abuse|molest\w*|sexual violence)\b", re.I
    ),
    "minors": re.compile(r"\b(?:minor|minors|child|children|teenager|teenagers|underage)\b", re.I),
    "personal_accusation": re.compile(
        r"\b(?:alleged(?:ly)?|accused|indicted|charged|arrested|bribery|fraud|corruption)\b", re.I
    ),
    "acute_violence": re.compile(
        r"\b(?:murder(?:ed)?|killed|mass shooting|terrorist attack|suicide)\b", re.I
    ),
}


def sensitive_risks(*values: str | None) -> list[str]:
    text = " ".join(value for value in values if isinstance(value, str))
    return [code for code, pattern in _SENSITIVE.items() if pattern.search(text)]
