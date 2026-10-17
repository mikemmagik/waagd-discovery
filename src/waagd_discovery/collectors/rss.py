"""Safe RSS 2.0 and Atom parsing into bounded, provenance-ready metadata."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, ParseError, tostring

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from waagd_discovery.models import ArticleCandidate, DiscoveryError, ParseResult, Source

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(element: Element | None, limit: int) -> str | None:
    if element is None:
        return None
    # XML descendants (Atom XHTML) and escaped HTML (RSS) both become plain text.
    raw = (element.text or "") + "".join(
        tostring(child, encoding="unicode", method="html") for child in element
    )
    parser = _PlainText()
    parser.feed(raw)
    parser.close()
    value = " ".join("".join(parser.parts).split())
    return value[:limit] or None


def _identifier(element: Element | None) -> str | None:
    if element is None:
        return None
    value = "".join(element.itertext()).strip()
    return value if value and len(value) <= 4096 else None


def _url(value: str | None) -> str | None:
    if not value or len(value) > 4096 or any(ord(character) < 33 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        if port is not None and not 1 <= port <= 65535:
            return None
        parsed.hostname.encode("idna")
    except (ValueError, UnicodeError):
        return None
    return value


def _date(value: str | None) -> tuple[datetime | None, Literal["valid", "missing", "invalid"]]:
    if value is None:
        return None, "missing"
    try:
        # ISO 8601 handles Atom; email dates handle RSS pubDate and DC fallback.
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, "invalid"
        return parsed.astimezone(UTC), "valid"
    except (ValueError, TypeError, OverflowError):
        return None, "invalid"


def _date_value(element: Element | None) -> str | None:
    if element is None:
        return None
    # A present but empty/oversized date is invalid rather than absent.
    value = "".join(element.itertext()).strip()
    return value if len(value) <= 4096 else ""


def _check_structure(root: Element) -> None:
    pending = [(root, 0)]
    count = 0
    while pending:
        element, depth = pending.pop()
        count += 1
        if depth > 100 or count > 100_000:
            raise DiscoveryError("feed_xml_limit")
        pending.extend((child, depth + 1) for child in element)


def _first(element: Element, *names: str) -> Element | None:
    for name in names:
        child = element.find(name)
        if child is not None:
            return child
    return None


def parse_feed(body: bytes, source: Source, max_items: int = 1000) -> ParseResult:
    """Parse only supported syndication formats; never fetch article URLs."""
    if max_items < 1:
        raise ValueError("max_items must be positive")
    try:
        root = ElementTree.fromstring(
            body, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except (ParseError, DefusedXmlException, ValueError):
        raise DiscoveryError("feed_xml") from None
    _check_structure(root)
    if root.tag == "rss" and root.get("version") == "2.0":
        channel = root.find("channel")
        if channel is None:
            raise DiscoveryError("feed_format")
        entries = channel.findall("item")
        atom = False
        feed_publisher = _text(_first(channel, "managingEditor", _DC + "publisher"), 300)
    elif root.tag == _ATOM + "feed":
        entries = root.findall(_ATOM + "entry")
        atom = True
        feed_publisher = _text(root.find(_ATOM + "author/" + _ATOM + "name"), 300)
    else:
        raise DiscoveryError("feed_format")
    if len(entries) > max_items:
        raise DiscoveryError("feed_item_limit")
    items: list[ArticleCandidate] = []
    skipped = 0
    for entry in entries:
        if atom:
            link = None
            for candidate in entry.findall(_ATOM + "link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = _url(candidate.get("href"))
                    if link:
                        break
            title = _text(entry.find(_ATOM + "title"), 1000)
            description = _text(_first(entry, _ATOM + "summary", _ATOM + "content"), 4000)
            guid = _identifier(entry.find(_ATOM + "id"))
            date = _date_value(_first(entry, _ATOM + "published", _ATOM + "updated"))
            publisher = _text(entry.find(_ATOM + "author/" + _ATOM + "name"), 300)
        else:
            guid_element = entry.find("guid")
            guid = _identifier(guid_element)
            link = _url(_identifier(entry.find("link")))
            if not link and guid_element is not None:
                if guid_element.get("isPermaLink", "true").lower() == "true":
                    link = _url(guid)
            title = _text(entry.find("title"), 1000)
            description = _text(_first(entry, "description", _CONTENT + "encoded"), 4000)
            date = _date_value(_first(entry, "pubDate", _DC + "date"))
            publisher = _text(_first(entry, "source", _DC + "publisher", "author"), 300)
        if not link:
            skipped += 1
            continue
        published_at, date_status = _date(date)
        items.append(
            ArticleCandidate(
                original_url=link,
                title=title,
                description=description,
                publisher=publisher or feed_publisher or source.name[:300],
                published_at=published_at,
                date_status=date_status,
                external_guid=guid,
            )
        )
    return ParseResult(items=items, skipped=skipped)
