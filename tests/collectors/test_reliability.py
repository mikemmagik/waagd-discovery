"""Offline recovery checks beyond the baseline retry and XML safety tests."""

import gzip
import time
import zlib
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpcore
import httpx
import pytest
from test_http import Chunks

from waagd_discovery import http
from waagd_discovery.collectors.rss import parse_feed
from waagd_discovery.http import HttpClient
from waagd_discovery.models import DiscoveryError, HttpSettings, Source
from waagd_discovery.pipeline import collect
from waagd_discovery.storage import Repository

SOURCE = Source("reliability", "Reliability", "http://feeds.example/rss")
RSS = (Path(__file__).parent / "fixtures" / "rss.xml").read_bytes()


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    monkeypatch.setattr(time, "sleep", lambda value: None)


class InterruptedChunks(Chunks):
    def __init__(self, error: Exception) -> None:
        super().__init__(b"discard-this-partial-response")
        self.error = error

    def __iter__(self) -> Iterator[bytes]:
        yield from super().__iter__()
        raise self.error


@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError]
)
def test_midstream_failure_closes_attempt_and_retries_from_empty_body(
    error_type: type[httpx.RequestError],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams: list[Chunks] = []
    delays: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        stream = (
            InterruptedChunks(error_type("untrusted-body-secret", request=request))
            if not streams
            else Chunks(RSS)
        )
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    def sleep(delay: float) -> None:
        assert streams[-1].closed, "response must close before retry delay"
        delays.append(delay)

    monkeypatch.setattr(time, "sleep", sleep)
    with HttpClient(HttpSettings(retries=1), transport=httpx.MockTransport(handle)) as client:
        assert client.fetch(SOURCE).body == RSS
    assert len(streams) == 2 and all(stream.closed for stream in streams)
    assert delays == [0.5]


@pytest.mark.parametrize(
    "error_type,code",
    [
        (httpx.ReadTimeout, "http_timeout"),
        (httpx.ReadError, "http_network"),
        (httpx.RemoteProtocolError, "http_network"),
    ],
)
def test_exhausted_midstream_failures_discard_partial_bytes(
    error_type: type[httpx.RequestError],
    code: str,
) -> None:
    streams: list[Chunks] = []

    def handle(request: httpx.Request) -> httpx.Response:
        stream = InterruptedChunks(error_type("untrusted-body-secret", request=request))
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    with HttpClient(HttpSettings(retries=1), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiscoveryError, match=f"^{code}$") as error:
            client.fetch(SOURCE)
    assert "untrusted-body-secret" not in str(error.value)
    assert len(streams) == 2 and all(stream.closed for stream in streams)


class WireConnection:
    """Feed actual httpcore/h11 bytes without opening any network socket."""

    def __init__(self, chunks: list[bytes | Exception]) -> None:
        self.chunks = deque(chunks)
        self.closed = False

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, info: str) -> None:
        return None


class WireBackend:
    def __init__(self, *connections: WireConnection) -> None:
        self.remaining = deque(connections)
        self.used: list[WireConnection] = []

    def connect_tcp(self, host: str, **kwargs: object) -> WireConnection:
        assert host == "93.184.216.34", "only the pinned address may reach the network backend"
        connection = self.remaining.popleft()
        self.used.append(connection)
        return connection


@pytest.mark.parametrize(
    "wire",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: 99999\r\n\r\n" + RSS,
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZ\r\ninvalid\r\n",
    ],
)
def test_real_protocol_rejects_truncated_length_and_chunked_framing(
    wire: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = WireConnection([wire])
    backend = WireBackend(connection)
    with HttpClient(HttpSettings(retries=0)) as client:
        monkeypatch.setattr(client._client._transport._inner._pool, "_network_backend", backend)
        with pytest.raises(DiscoveryError, match="^http_network$"):
            client.fetch(SOURCE)
    assert connection.closed
    assert backend.used == [connection]


def test_real_protocol_midstream_timeout_recovers_on_new_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = WireConnection(
        [
            b"HTTP/1.1 200 OK\r\nContent-Length: 99999\r\n\r\npartial",
            httpcore.ReadTimeout("untrusted connection details"),
        ]
    )
    second = WireConnection(
        [
            b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(RSS)).encode() + b"\r\n\r\n",
            RSS,
        ]
    )
    backend = WireBackend(first, second)
    with HttpClient(HttpSettings(retries=1)) as client:
        monkeypatch.setattr(client._client._transport._inner._pool, "_network_backend", backend)
        assert client.fetch(SOURCE).body == RSS
    assert first.closed and second.closed
    assert backend.used == [first, second]


def test_real_chunked_gzip_works_across_arbitrary_wire_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compressed = gzip.compress(RSS)
    wire = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Encoding: gzip\r\n\r\n"
        + f"{len(compressed):x}\r\n".encode()
        + compressed
        + b"\r\n0\r\n\r\n"
    )
    connection = WireConnection([wire[index : index + 3] for index in range(0, len(wire), 3)])
    with HttpClient(HttpSettings(retries=0)) as client:
        monkeypatch.setattr(
            client._client._transport._inner._pool, "_network_backend", WireBackend(connection)
        )
        result = client.fetch(SOURCE)
    assert result.body == RSS
    assert len(parse_feed(result.body, SOURCE).items) == 3
    assert connection.closed


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
@pytest.mark.parametrize("damage", ["truncated", "checksum"])
def test_compressed_corruption_rejects_entire_body_without_retry(
    encoding,
    compress,
    damage,
) -> None:
    compressed = compress(RSS)
    compressed = (
        compressed[:-1]
        if damage == "truncated"
        else compressed[:-1] + bytes([compressed[-1] ^ 0xFF])
    )
    streams: list[Chunks] = []

    def handle(request: httpx.Request) -> httpx.Response:
        stream = Chunks(*(compressed[index : index + 7] for index in range(0, len(compressed), 7)))
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"Content-Encoding": encoding})

    with HttpClient(HttpSettings(retries=2), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiscoveryError, match="^http_encoding$"):
            client.fetch(SOURCE)
    assert len(streams) == 1 and streams[0].closed


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0),
        (" 2 ", 2),
        ("-1", 0.5),
        ("1.5", 0.5),
        ("NaN", 0.5),
        ("Infinity", 0.5),
        ("9" * 400, 0.5),
        (format_datetime(datetime(2000, 1, 1, tzinfo=UTC)), 0),
        (format_datetime(datetime.now(UTC) + timedelta(days=1)), 3),
        ("Sat, 17 Oct 2026 10:00:00", 0.5),
    ],
)
def test_retry_after_edges_close_response_before_delay(
    status: int,
    value: str,
    expected: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams: list[Chunks] = []
    delays: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        stream = Chunks(b"never-consume-error-body") if not streams else Chunks(RSS)
        streams.append(stream)
        return httpx.Response(
            status if len(streams) == 1 else 200,
            stream=stream,
            headers={"Retry-After": value} if len(streams) == 1 else {},
        )

    def sleep(delay: float) -> None:
        assert streams[-1].closed
        delays.append(delay)

    monkeypatch.setattr(time, "sleep", sleep)
    with HttpClient(
        HttpSettings(retries=1, max_retry_after=3), transport=httpx.MockTransport(handle)
    ) as client:
        assert client.fetch(SOURCE).body == RSS
    assert delays == [expected]
    assert len(streams) == 2 and all(stream.closed for stream in streams)


@pytest.mark.parametrize("failure", ["gzip", "xml", "codec", "timeout"])
def test_corrupt_source_retains_old_metadata_and_does_not_block_next_source(
    failure: str,
    tmp_path: Path,
) -> None:
    broken = Source("broken", "Broken", "https://broken.example/rss")
    healthy = Source("healthy", "Healthy", "https://healthy.example/rss")
    prior = (
        b'<rss version="2.0"><channel><item><title>Keep me</title>'
        b"<link>https://example.org/prior</link></item></channel></rss>"
    )
    fresh = prior.replace(b"Keep me", b"New discovery").replace(b"/prior", b"/fresh")
    streams: list[Chunks] = []

    def handle(request: httpx.Request) -> httpx.Response:
        headers = {"ETag": '"must-not-commit"'}
        if request.url.host == "healthy.example":
            stream = Chunks(fresh)
        elif failure == "gzip":
            stream = Chunks(gzip.compress(prior.replace(b"Keep me", b"Corrupted"))[:-2])
            headers["Content-Encoding"] = "gzip"
        elif failure == "timeout":
            stream = InterruptedChunks(httpx.ReadTimeout("untrusted secret", request=request))
        elif failure == "codec":
            stream = Chunks(b'<?xml version="1.0" encoding="unknown-secret-codec"?>' + prior)
        else:
            stream = Chunks(prior[:-4])
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers=headers)

    db = tmp_path / "discovery.sqlite3"
    with Repository(db) as repository:
        with HttpClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=prior, headers={"ETag": '"saved"'})
            )
        ) as client:
            assert collect(repository, [broken], client)["status"] == "success"
        before = repository.candidates()[0]
        with HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(handle)) as client:
            result = collect(repository, [broken, healthy], client)
        assert result["status"] == "partial"
        assert result["sources_failed"] == 1
        assert result["items_added"] == 1
        assert repository.get_source("broken").etag == '"saved"'
        assert repository.stats()["articles"] == 2
        assert repository.stats()["observations"] == 2
        assert (
            next(item for item in repository.candidates() if item["id"] == before["id"]) == before
        )
        repository.verify()
    assert all(stream.closed for stream in streams)
    with Repository(db) as reopened:
        assert reopened.stats()["articles"] == 2
        assert reopened.get_source("broken").etag == '"saved"'
