"""Trusted, narrow corroboration policy for a bounded candidate batch.

The caller constructs these rules from an independently governed source registry.
Neither Bridge candidate metadata nor retrieved page links are policy input here.
"""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from waagd_discovery.editorial.contracts import EditorialError


def _host(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or len(value) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError
        return parsed.hostname.lower().rstrip(".")
    except (ValueError, TypeError):
        raise EditorialError("corroboration_policy_invalid") from None


def _related_hosts(first: str, second: str) -> bool:
    return first == second or first.endswith("." + second) or second.endswith("." + first)


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def contains_exact_claim(excerpt: str, claim: str) -> bool:
    """A transparent phrase check, intentionally not semantic equivalence."""
    return _normalized(claim) in _normalized(excerpt)


@dataclass(frozen=True)
class CorroborationClaim:
    claim: str
    independent_url: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.claim, str)
            or not 20 <= len(self.claim) <= 300
            or len(self.claim.split()) < 4
            or any(unicodedata.category(char)[0] == "C" for char in self.claim)
        ):
            raise EditorialError("corroboration_policy_invalid")
        _host(self.independent_url)


@dataclass(frozen=True)
class CorroborationPolicy:
    candidate_key: str
    trusted_independent_hosts: frozenset[str]
    claims: tuple[CorroborationClaim, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidate_key, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.candidate_key) is None
            or not isinstance(self.trusted_independent_hosts, frozenset)
            or not 1 <= len(self.trusted_independent_hosts) <= 16
            or not isinstance(self.claims, tuple)
            or not 1 <= len(self.claims) <= 4
        ):
            raise EditorialError("corroboration_policy_invalid")
        if any(
            not isinstance(host, str)
            or host != host.lower().rstrip(".")
            or re.fullmatch(r"[a-z0-9.-]{1,253}", host) is None
            for host in self.trusted_independent_hosts
        ):
            raise EditorialError("corroboration_policy_invalid")
        if any(
            not isinstance(claim, CorroborationClaim)
            or _host(claim.independent_url) not in self.trusted_independent_hosts
            for claim in self.claims
        ):
            raise EditorialError("corroboration_policy_invalid")

    def authorizes(self, article_url: str, independent_url: str) -> bool:
        article_host, independent_host = _host(article_url), _host(independent_url)
        return independent_host in self.trusted_independent_hosts and not _related_hosts(
            article_host, independent_host
        )


@dataclass(frozen=True)
class AutomaticCorroborationPolicy:
    """A governed verifier endpoint, not a candidate-provided lookup URL.

    The endpoint must return one strict JSON assertion for the supplied canonical
    URL. The resulting claim still has to occur verbatim in the retrieved article.
    """

    endpoint_template: str

    def __post_init__(self) -> None:
        marker = "{candidate_url}"
        if (
            not isinstance(self.endpoint_template, str)
            or self.endpoint_template.count(marker) != 1
            or marker not in (urlsplit(self.endpoint_template).query or "")
        ):
            raise EditorialError("corroboration_policy_invalid")
        _host(self.endpoint_template.replace(marker, "https%3A%2F%2Fcandidate.invalid%2F"))

    def lookup_url(self, article_url: str) -> str:
        value = self.endpoint_template.replace("{candidate_url}", quote(article_url, safe=""))
        if not self.authorizes(article_url, value):
            raise EditorialError("corroboration_policy_invalid")
        return value

    def authorizes(self, article_url: str, independent_url: str) -> bool:
        return not _related_hosts(_host(article_url), _host(independent_url))
