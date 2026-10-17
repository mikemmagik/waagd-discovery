import gzip
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from waagd_discovery import http
from waagd_discovery.http import HttpClient
from waagd_discovery.models import DiscoveryError, HttpSettings, Source

SOURCE = Source(id="test", name="Test", url="https://feeds.example/rss")


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "_resolve_public", lambda host, port, timeout: "93.184.216.34")


class Chunks(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


def test_user_agent_conditionals_and_response_validators() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == SOURCE.url
        assert request.headers["User-Agent"].startswith("WAAGD-Discovery/")
        assert request.headers["If-None-Match"] == '"v1"'
        assert request.headers["If-Modified-Since"] == "Sat, 17 Oct 2026 00:00:00 GMT"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, content=b"<rss/>", headers={"ETag": '"v2"'})

    source = Source(
        **{**SOURCE.__dict__, "etag": '"v1"', "last_modified": "Sat, 17 Oct 2026 00:00:00 GMT"}
    )
    with HttpClient(transport=httpx.MockTransport(handle)) as client:
        result = client.fetch(source)
    assert result.body == b"<rss/>"
    assert result.etag == '"v2"'
    assert not result.not_modified


def test_304_does_not_read_response_body() -> None:
    class Unreadable(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("304 body must not be consumed")
            yield b""  # pragma: no cover

    with HttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(304, stream=Unreadable(), headers={"ETag": '"v1"'})
        )
    ) as client:
        result = client.fetch(SOURCE)
    assert result.not_modified
    assert result.body == b""


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418])
def test_client_error_is_not_retried_or_leaked(status: int) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=b"secret response body")

    with HttpClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiscoveryError, match=f"^http_status_{status}$"):
            client.fetch(SOURCE)
    assert calls == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_limit_and_capped_retry_after(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    calls = 0
    monkeypatch.setattr(time, "sleep", delays.append)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Retry-After": "9999999"})

    with HttpClient(
        HttpSettings(retries=2, max_retry_after=3), transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(DiscoveryError, match=f"^http_status_{status}$"):
            client.fetch(SOURCE)
    assert calls == 3
    assert delays == [3, 3]


def test_retry_after_date_and_invalid_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    headers = [format_datetime(datetime.now(UTC) + timedelta(hours=2)), "not-a-date", None]
    monkeypatch.setattr(time, "sleep", delays.append)

    def handle(request: httpx.Request) -> httpx.Response:
        value = headers.pop(0)
        if value is None:
            return httpx.Response(200, content=b"ok")
        return httpx.Response(429, headers={"Retry-After": value})

    with HttpClient(
        HttpSettings(retries=2, backoff=0.5, max_retry_after=2),
        transport=httpx.MockTransport(handle),
    ) as client:
        assert client.fetch(SOURCE).body == b"ok"
    assert delays == [2, 1]


def test_timeout_retries_and_safe_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("upstream password=secret", request=request)

    with HttpClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiscoveryError, match="^http_timeout$"):
            client.fetch(SOURCE)
    assert calls == 3
    assert delays == [0.5, 1.0]


def test_network_failure_can_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda value: None)
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("upstream credentials", request=request)
        return httpx.Response(200, content=b"ok")

    with HttpClient(transport=httpx.MockTransport(handle)) as client:
        assert client.fetch(SOURCE).body == b"ok"
    assert calls == 2


def test_redirects_relative_urls_and_origin_bound_headers() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers={
                    "Location": "/next",
                    "Set-Cookie": "session=untrusted; Path=/",
                },
            )
        if len(seen) == 2:
            return httpx.Response(307, headers={"Location": "https://other.example/final"})
        return httpx.Response(200, content=b"ok")

    source = Source(**{**SOURCE.__dict__, "etag": '"v1"'})
    with HttpClient(transport=httpx.MockTransport(handle)) as client:
        assert client.fetch(source).body == b"ok"
    assert str(seen[1].url) == "https://feeds.example/next"
    assert seen[1].headers["If-None-Match"] == '"v1"'
    assert "Cookie" not in seen[1].headers
    assert "If-None-Match" not in seen[2].headers


def test_redirect_limit() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "/loop"})

    with HttpClient(HttpSettings(max_redirects=2), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiscoveryError, match="^http_redirect$"):
            client.fetch(SOURCE)
    assert calls == 3


@pytest.mark.parametrize(
    "length,code",
    [("99", "http_size"), ("-1", "http_content_length"), ("garbage", "http_content_length")],
)
def test_response_length_rejected_before_read(length: str, code: str) -> None:
    stream = Chunks(b"oversized")
    with HttpClient(
        HttpSettings(max_bytes=5),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers={"Content-Length": length})
        ),
    ) as client:
        with pytest.raises(DiscoveryError, match=f"^{code}$"):
            client.fetch(SOURCE)
    assert stream.closed


def test_streaming_size_bound_without_content_length() -> None:
    stream = Chunks(b"123", b"456")
    with HttpClient(
        HttpSettings(max_bytes=5),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    ) as client:
        with pytest.raises(DiscoveryError, match="^http_size$"):
            client.fetch(SOURCE)
    assert stream.closed


def test_gzip_bounded_decompression() -> None:
    compressed = gzip.compress(b"A" * 100_000)
    with HttpClient(
        HttpSettings(max_bytes=1000),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=Chunks(compressed), headers={"Content-Encoding": "gzip"}
            )
        ),
    ) as client:
        with pytest.raises(DiscoveryError, match="^http_size$"):
            client.fetch(SOURCE)


def test_gzip_normal_response() -> None:
    compressed = gzip.compress(b"a feed body")
    with HttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                stream=Chunks(compressed[:4], compressed[4:]),
                headers={"Content-Encoding": "gzip"},
            )
        )
    ) as client:
        assert client.fetch(SOURCE).body == b"a feed body"


@pytest.mark.parametrize(
    "encoding,body", [("br", b"bad"), ("gzip", b"not gzip"), ("gzip", gzip.compress(b"feed")[:-4])]
)
def test_unknown_or_corrupt_encoding(encoding: str, body: bytes) -> None:
    with HttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=Chunks(body), headers={"Content-Encoding": encoding}
            )
        )
    ) as client:
        with pytest.raises(DiscoveryError, match="^http_encoding$"):
            client.fetch(SOURCE)


def test_total_deadline_stops_slow_trickle(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class SlowTrickle(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _ in range(100):
                clock[0] += 1
                yield b"a"

    with HttpClient(
        HttpSettings(connect_timeout=1, read_timeout=2),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowTrickle())),
    ) as client:
        with pytest.raises(DiscoveryError, match="^http_deadline$"):
            client.fetch(SOURCE)
    assert clock[0] == 4


def test_unsafe_validators_are_not_forwarded() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "If-None-Match" not in request.headers
        return httpx.Response(200, content=b"ok", headers={"ETag": "x" * 5000})

    source = Source(**{**SOURCE.__dict__, "etag": '"safe"\r\nInjected: bad'})
    with HttpClient(transport=httpx.MockTransport(handle)) as client:
        assert client.fetch(source).etag is None
