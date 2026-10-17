"""Cancellation, validator and damaged-data boundaries, without external requests."""

import json
import signal
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from waagd_discovery import cli, collection, http
from waagd_discovery.collectors.rss import parse_feed
from waagd_discovery.http import HttpClient
from waagd_discovery.locking import database_lock
from waagd_discovery.models import DiscoveryError, HttpSettings, Source
from waagd_discovery.storage import Repository

SOURCE = Source("test", "Test", "https://feeds.example/rss")
RSS = (
    b'<rss version="2.0"><channel><item><title>Retained</title>'
    b"<link>https://example.org/retained</link></item></channel></rss>"
)


@pytest.mark.parametrize("encoding", ["unknown-secret-codec", "UTF-42"])
def test_unknown_xml_codec_produces_safe_source_error(encoding: str) -> None:
    body = f'<?xml version="1.0" encoding="{encoding}"?>'.encode() + RSS
    with pytest.raises(DiscoveryError, match="^feed_xml$") as error:
        parse_feed(body, SOURCE)
    assert encoding not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        "\x00",
        "\t",
        "\r",
        "\n",
        "\x7f",
        "nonascii-\u00e9",
        "x" * 4097,
    ],
)
@pytest.mark.parametrize(
    "field,header", [("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")]
)
def test_malformed_saved_validators_cannot_inject_request_headers(
    field: str,
    header: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    source = Source(**{**SOURCE.__dict__, field: value})

    def handle(request: httpx.Request) -> httpx.Response:
        assert header not in request.headers
        return httpx.Response(304)

    with HttpClient(transport=httpx.MockTransport(handle)) as client:
        assert client.fetch(source).not_modified


@pytest.mark.parametrize("value", [b"\x00", b"\t", b"\r", b"\n", b"\x7f", b"\xff", b"x" * 4097])
def test_malformed_response_validators_are_not_returned_for_persistence(
    value: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    with HttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                304, headers=[(b"etag", value), (b"last-modified", value)]
            )
        )
    ) as client:
        result = client.fetch(SOURCE)
    assert result.etag is None
    assert result.last_modified is None


def test_alternating_origin_redirect_loop_is_bounded_and_closes_every_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    streams = []

    class Unreadable(httpx.SyncByteStream):
        closed = False

        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("redirect bodies must not be read")
            yield b""  # pragma: no cover

        def close(self) -> None:
            self.closed = True

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "other.example":
            assert "If-None-Match" not in request.headers
        stream = Unreadable()
        streams.append(stream)
        other = "other.example" if request.url.host == SOURCE.url.split("/")[2] else "feeds.example"
        return httpx.Response(302, stream=stream, headers={"Location": f"https://{other}/rss"})

    source = Source(**{**SOURCE.__dict__, "etag": '"private-validator"'})
    with HttpClient(
        HttpSettings(max_redirects=3, retries=2), transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(DiscoveryError, match="^http_redirect$"):
            client.fetch(source)
    assert len(streams) == 4 and all(stream.closed for stream in streams)


@pytest.mark.parametrize("phase", ["body", "retry_delay"])
def test_sigterm_closes_http_and_database_resources_and_retains_prior_source(
    phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deliver a real signal inside HTTP, extending baseline between-source cancellation."""
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    streams = []
    requests = []

    class SignalBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self) -> Iterator[bytes]:
            yield b"partial"
            signal.raise_signal(signal.SIGTERM)
            raise AssertionError("SIGTERM did not stop response consumption")

        def close(self) -> None:
            self.closed = True

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "one.example":
            return httpx.Response(200, content=RSS, headers={"ETag": '"saved"'})
        stream = SignalBody()
        streams.append(stream)
        return httpx.Response(
            200 if phase == "body" else 503,
            stream=stream,
            headers={"ETag": '"must-not-commit"', "Retry-After": "1"},
        )

    def interrupted_sleep(delay: float) -> None:
        assert streams[-1].closed
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt retry delay")

    if phase == "retry_delay":
        monkeypatch.setattr(time, "sleep", interrupted_sleep)
    client = HttpClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(collection, "HttpClient", lambda: client)
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "sources:\n"
        "- {id: one, name: One, url: 'https://one.example/rss'}\n"
        "- {id: two, name: Two, url: 'https://two.example/rss'}\n"
        "- {id: three, name: Three, url: 'https://three.example/rss'}\n"
    )
    db = tmp_path / "discovery.sqlite3"
    previous_handler = signal.getsignal(signal.SIGTERM)
    assert cli.main(["--db", str(db), "--sources", str(registry), "collect", "--once"]) == 130
    assert signal.getsignal(signal.SIGTERM) is previous_handler
    assert client._client.is_closed
    assert len(requests) == 2 and len(streams) == 1 and streams[0].closed
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "interrupted"
    assert "Traceback" not in output.err
    with database_lock(db), Repository(db) as repository:
        assert repository.stats()["articles"] == 1
        assert repository.stats()["observations"] == 1
        assert repository.get_source("one").etag == '"saved"'
        assert repository.get_source("two").etag is None
        assert repository.get_source("three").etag is None
        assert (
            repository.connection.execute("SELECT status FROM runs").fetchone()[0] == "interrupted"
        )
        repository.verify()


def test_dns_worker_capacity_returns_after_caller_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = threading.BoundedSemaphore(1)
    release_worker = threading.Event()
    started = threading.Event()
    monkeypatch.setattr(http, "_DNS_WORKERS", slots)

    def resolve(*args: object, **kwargs: object):
        started.set()
        assert release_worker.wait(timeout=2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    def interrupted_get(self, *args: object, **kwargs: object):
        assert started.wait(timeout=2)
        raise KeyboardInterrupt

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(http.queue.Queue, "get", interrupted_get)
    try:
        with pytest.raises(KeyboardInterrupt):
            http._resolve_public("feeds.example", 443, 2)
    finally:
        release_worker.set()
    assert slots.acquire(timeout=2), "completed DNS worker leaked its capacity slot"
    slots.release()
