"""Conservative retrieval evidence, never automatic factual corroboration."""

import hashlib
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Protocol

from waagd_discovery.editorial.contracts import (
    VERIFICATION_POLICY,
    EditorialError,
    digest_candidate,
    utc,
    validate_verification,
)
from waagd_discovery.editorial.transport import FetchDocument, PinnedTransport

_HIDDEN = {"head", "script", "style", "svg", "math", "template", "iframe", "object", "noscript"}


class ArticleTransport(Protocol):
    def fetch(self, value: str) -> FetchDocument: ...


def _plain(value: str, maximum: int = 16000) -> str:
    value = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in value)
    return re.sub(r"\s+", " ", value).strip()[:maximum]


class _ArticleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden: list[str] = []
        self.fragments: list[str] = []
        self.size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            self.hidden.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.hidden:
            self.hidden = self.hidden[: self.hidden.index(tag)]

    def handle_data(self, data: str) -> None:
        if not self.hidden and self.size < 16000:
            text = _plain(data, 16000 - self.size)
            if text:
                self.fragments.append(text)
                self.size += len(text) + 1


def article_text(document: FetchDocument) -> str:
    try:
        decoded = document.body.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        raise EditorialError("source_text_encoding_unsupported") from None
    if document.content_type == "text/plain":
        return _plain(decoded)
    parser = _ArticleText()
    try:
        parser.feed(decoded)
        parser.close()
    except (ValueError, RecursionError):
        raise EditorialError("source_html_invalid") from None
    return _plain(" ".join(parser.fragments))


def _base(candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        key = candidate["candidate_key"]
        source_url = candidate["url"]
        if not isinstance(key, str) or not isinstance(source_url, str):
            raise ValueError
        if digest_candidate(source_url) != key:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise EditorialError("verification_candidate_invalid") from None
    return {
        "schema_version": 1,
        "candidate_key": key,
        "verification_status": "FAILED",
        "canonical_url": source_url,
        "fetched_at": None,
        "http_status": None,
        "publisher": None,
        "publication_date": None,
        "event_date": None,
        "evidence": [],
        "uncertainties": [],
        "errors": [],
        "claim_support": [],
    }


class SourceVerifier:
    def __init__(
        self,
        transport: ArticleTransport | None = None,
        *,
        allow_http: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport or PinnedTransport(allow_http=allow_http)
        self.clock = clock

    def verify(self, candidate: dict[str, Any], *, policy_id: str) -> dict[str, Any]:
        result = _base(candidate)
        if policy_id != VERIFICATION_POLICY:
            result["errors"] = ["source_policy_unauthorized"]
        else:
            try:
                document = self.transport.fetch(candidate["url"])
                result["http_status"] = document.status
                result["canonical_url"] = document.url
                if document.status != 200:
                    raise EditorialError(f"source_http_status_{document.status}")
                extracted = article_text(document)
                if not extracted:
                    raise EditorialError("source_text_empty")
                fetched = utc(self.clock() if self.clock else None).isoformat()
                body_hash = hashlib.sha256(document.body).hexdigest()
                identity = hashlib.sha256((document.url + "\0" + body_hash).encode()).hexdigest()
                result.update(
                    {
                        "verification_status": "PARTIAL",
                        "fetched_at": fetched,
                        "evidence": [
                            {
                                "id": identity,
                                "url": document.url,
                                "retrieved_at": fetched,
                                "sha256": body_hash,
                                "excerpt": extracted[:8000],
                                "source_kind": "article",
                            }
                        ],
                        "uncertainties": [
                            "Retrieved article text is an untrusted source assertion, "
                            "not confirmation of an event.",
                            "Material claims have not been independently corroborated.",
                            "Publisher identity, publication date and event date "
                            "have not been established.",
                            "Only bounded UTF-8 article text was retained; "
                            "scripts, metadata and links grant no permissions.",
                        ],
                    }
                )
            except EditorialError as error:
                # Only errors emitted by our transport/extractor are permitted into the schema.
                code = str(error)
                result["errors"] = [
                    code if re.fullmatch(r"source_[a-z0-9_]{1,72}", code) else "source_fetch_failed"
                ]
                result["uncertainties"] = ["No sufficient source evidence was obtained."]
        validate_verification(result, candidate["candidate_key"])
        return result


class SyntheticVerifier:
    """Explicit offline simulation; candidate text is not independent evidence."""

    def verify(self, candidate: dict[str, Any], *, policy_id: str) -> dict[str, Any]:
        result = _base(candidate)
        if policy_id != VERIFICATION_POLICY:
            result["errors"] = ["source_policy_unauthorized"]
        else:
            result.update(
                {
                    "verification_status": "PARTIAL",
                    "uncertainties": [
                        "SIMULATION ONLY: no source was contacted or verified.",
                        "Candidate assertions require independent factual verification.",
                    ],
                }
            )
        validate_verification(result, candidate["candidate_key"])
        return result
