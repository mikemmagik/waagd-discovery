from datetime import UTC, datetime
from pathlib import Path

import pytest

from waagd_discovery.collectors.rss import parse_feed
from waagd_discovery.models import DiscoveryError, Source

SOURCE = Source(id="test", name="Test Source", url="https://feeds.example/rss")
FIXTURES = Path(__file__).parent / "fixtures"


def test_rss_extracts_metadata_and_keeps_unknown_dates() -> None:
    result = parse_feed((FIXTURES / "rss.xml").read_bytes(), SOURCE)
    assert len(result.items) == 3
    assert result.skipped == 1
    first, missing, invalid = result.items
    assert first.title == "Committee automates committee formation"
    assert first.description == "A new committee & its report."
    assert first.publisher == "Example News"
    assert first.external_guid == "committee-001"
    assert first.published_at == datetime(2026, 10, 17, 13, tzinfo=UTC)
    assert first.date_status == "valid"
    assert missing.title is None
    assert missing.published_at is None
    assert missing.date_status == "missing"
    assert missing.publisher == SOURCE.name
    assert invalid.published_at is None
    assert invalid.date_status == "invalid"


def test_atom_alternate_links_xhtml_and_feed_author() -> None:
    result = parse_feed((FIXTURES / "atom.xml").read_bytes(), SOURCE)
    first, second = result.items
    assert first.original_url == "https://news.example/articles/1"
    assert first.external_guid == "tag:example.org,2026:1"
    assert first.description == "One actual event."
    assert first.published_at == datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
    assert first.publisher == "Entry Publisher"
    assert second.publisher == "Feed Publisher"
    assert second.published_at == datetime(2026, 10, 17, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "body",
    [
        b'<rss version="2.0"><channel/></rss>',
        b'<feed xmlns="http://www.w3.org/2005/Atom"/>',
    ],
)
def test_empty_supported_feed(body: bytes) -> None:
    result = parse_feed(body, SOURCE)
    assert result.items == []
    assert result.skipped == 0


@pytest.mark.parametrize(
    "body,code",
    [
        (b"<rss>", "feed_xml"),
        (b"", "feed_xml"),
        (b"<html/>", "feed_format"),
        (b'<rss version="1.0"><channel/></rss>', "feed_format"),
        (b'<rss version="2.0"/>', "feed_format"),
    ],
)
def test_malformed_and_unsupported_feeds(body: bytes, code: str) -> None:
    with pytest.raises(DiscoveryError, match=f"^{code}$"):
        parse_feed(body, SOURCE)


def rss_item(fields: str) -> bytes:
    return f'<rss version="2.0"><channel><item>{fields}</item></channel></rss>'.encode()


@pytest.mark.parametrize("value", ["", "not-a-date", "2026-10-17", "2026-10-17T10:00:00"])
def test_invalid_or_ambiguous_dates_are_never_fabricated(value: str) -> None:
    result = parse_feed(
        rss_item(f"<link>https://example.org/story</link><pubDate>{value}</pubDate>"), SOURCE
    )
    assert result.items[0].published_at is None
    assert result.items[0].date_status == "invalid"


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "relative-path",
        "https:///missing-host",
        "https://user:password@example.org/",
        "https://:password@example.org/",
        "https://example.org:99999/",
        "https://example.org/a b",
    ],
)
def test_invalid_article_links_are_skipped(value: str) -> None:
    result = parse_feed(rss_item(f"<link>{value}</link>"), SOURCE)
    assert result.items == []
    assert result.skipped == 1


def test_rss_guid_permalink_fallback() -> None:
    result = parse_feed(rss_item("<guid>https://example.org/article</guid>"), SOURCE)
    assert result.items[0].original_url == "https://example.org/article"


def test_item_limit_fails_whole_document() -> None:
    with pytest.raises(DiscoveryError, match="^feed_item_limit$"):
        parse_feed((FIXTURES / "rss.xml").read_bytes(), SOURCE, max_items=2)


def test_field_limits_and_description_plain_text() -> None:
    result = parse_feed(
        rss_item(
            "<link>https://example.org/article</link>"
            f"<title>{'T' * 1100}</title><source>{'P' * 400}</source>"
            f"<description><![CDATA[<b>{'D' * 4100}</b>]]></description>"
            f'<guid isPermaLink="false">{"G" * 4100}</guid>'
        ),
        SOURCE,
    )
    item = result.items[0]
    assert item.title is not None and len(item.title) == 1000
    assert item.publisher is not None and len(item.publisher) == 300
    assert item.description is not None and len(item.description) == 4000
    assert item.description == "D" * 4000
    assert item.external_guid is None


def test_dublin_core_date_and_content_fallback() -> None:
    body = b"""<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel><item><link>https://example.org/story</link>
      <dc:date>2026-10-17T10:00:00Z</dc:date><dc:publisher>Publisher</dc:publisher>
      <content:encoded>&lt;p&gt;A story&lt;/p&gt;</content:encoded></item></channel></rss>"""
    item = parse_feed(body, SOURCE).items[0]
    assert item.description == "A story"
    assert item.publisher == "Publisher"
    assert item.date_status == "valid"


def test_repeat_parse_is_deterministic() -> None:
    body = (FIXTURES / "rss.xml").read_bytes()
    assert parse_feed(body, SOURCE) == parse_feed(body, SOURCE)
