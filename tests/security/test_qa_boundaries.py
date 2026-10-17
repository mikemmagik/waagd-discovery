"""Independent offline probes of network and XML trust boundaries."""

import gzip
import socket
from collections.abc import Iterator

import httpx
import pytest

from waagd_discovery import http
from waagd_discovery.collectors.rss import parse_feed
from waagd_discovery.http import HttpClient, _PublicTransport
from waagd_discovery.models import DiscoveryError, HttpSettings, Source

SOURCE = Source("qa", "QA source", "https://feeds.example.org/rss")


def test_same_hostname_redirect_revalidates_dns_before_second_request(monkeypatch):
    answers = iter(("93.184.216.34", "169.254.169.254"))

    def resolver(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "/next"})

    with HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiscoveryError, match="^http_private_address$"):
            client.fetch(SOURCE)
    assert len(requests) == 1


def test_ipv6_pinning_preserves_tls_hostname_and_explicit_host_port(monkeypatch):
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "2606:4700:4700::1111")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b"ok")

    with _PublicTransport(httpx.MockTransport(handle), 5, pin=True) as transport:
        transport.handle_request(httpx.Request("GET", "https://feeds.example.org:8443/rss"))
    assert requests[0].url.host == "2606:4700:4700::1111"
    assert requests[0].url.port == 8443
    assert requests[0].headers["Host"] == "feeds.example.org:8443"
    assert requests[0].extensions["sni_hostname"] == "feeds.example.org"


class Payload(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks

    def close(self):
        self.closed = True


@pytest.mark.parametrize("trailer", [b"untrusted trailer", gzip.compress(b"second member")])
def test_gzip_trailing_data_or_second_member_fails_closed(monkeypatch, trailer):
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    stream = Payload([gzip.compress(b"first member"), trailer])
    with HttpClient(
        HttpSettings(retries=0),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"})
        ),
    ) as client:
        with pytest.raises(DiscoveryError, match="^http_encoding$"):
            client.fetch(SOURCE)
    assert stream.closed


def test_non_200_bodies_are_never_consumed_or_included_in_error(monkeypatch):
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")

    class ForbiddenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("error body must not be read")
            yield b""  # pragma: no cover

    with HttpClient(
        HttpSettings(retries=0),
        transport=httpx.MockTransport(lambda request: httpx.Response(503, stream=ForbiddenBody())),
    ) as client:
        with pytest.raises(DiscoveryError, match="^http_status_503$"):
            client.fetch(SOURCE)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_xml_encoding_cannot_bypass_dtd_rejection(encoding):
    xml = (
        '<?xml version="1.0" encoding="' + encoding + '"?>'
        '<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<rss version="2.0"><channel><title>&x;</title></channel></rss>'
    ).encode(encoding)
    with pytest.raises(DiscoveryError, match="^feed_xml$"):
        parse_feed(xml, SOURCE)


def test_out_of_range_utc_conversion_is_invalid_metadata_not_source_failure():
    body = b"""<rss version="2.0"><channel><item>
    <link>https://example.org/item</link>
    <pubDate>0001-01-01T00:00:00+14:00</pubDate>
    </item></channel></rss>"""
    parsed = parse_feed(body, SOURCE)
    assert len(parsed.items) == 1
    assert parsed.items[0].published_at is None
    assert parsed.items[0].date_status == "invalid"
