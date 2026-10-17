import socket
import ssl
import threading
from collections.abc import Iterator

import httpx
import pytest

from waagd_discovery import http
from waagd_discovery.http import HttpClient, _PublicTransport, _resolve_public
from waagd_discovery.models import DiscoveryError, HttpSettings, Source


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org/feed",
        "https://user:secret@example.org/feed",
        "https://example.org:99999/feed",
        "https:///feed",
        "https://example.org/feed\nInjected",
        "https://[fe80::1%en0]/feed",
        "https://example.org/feed#fragment",
    ],
)
def test_invalid_configured_url_never_reaches_transport(url: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsafe URL reached transport")

    with HttpClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiscoveryError, match="^http_url$"):
            client.fetch(Source(id="test", name="Test", url=url))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "100.64.0.1",
        "192.0.2.1",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
    ],
)
def test_nonpublic_literal_addresses_rejected(address: str) -> None:
    with pytest.raises(DiscoveryError, match="^http_private_address$"):
        _resolve_public(address, 443, 1)


def test_mixed_public_and_private_dns_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(DiscoveryError, match="^http_private_address$"):
        _resolve_public("feeds.example", 443, 1)


def test_dns_failure_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> Iterator[object]:
        raise socket.gaierror("resolver secret hostname")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(DiscoveryError, match="^http_dns$"):
        _resolve_public("feeds.example", 443, 1)


def test_dns_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoWorker:
        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(http.threading, "Thread", NoWorker)
    monkeypatch.setattr(http, "_DNS_WORKERS", threading.BoundedSemaphore(1))
    with pytest.raises(DiscoveryError, match="^http_dns_timeout$"):
        _resolve_public("feeds.example", 443, 0.001)


def test_dns_worker_cap_fails_without_spawning_more_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(http, "_DNS_WORKERS", slots)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("no worker may be created while DNS capacity is exhausted")

    monkeypatch.setattr(http.threading, "Thread", forbidden)
    with pytest.raises(DiscoveryError, match="^http_dns_timeout$"):
        _resolve_public("feeds.example", 443, 0.001)


def test_validated_dns_is_pinned_and_tls_uses_original_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    with _PublicTransport(httpx.MockTransport(handle), 5, pin=True) as transport:
        response = transport.handle_request(httpx.Request("GET", "https://feeds.example/rss"))
    assert response.status_code == 200
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["Host"] == "feeds.example"
    assert seen[0].extensions["sni_hostname"] == "feeds.example"


def test_redirect_to_private_address_is_rejected_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def resolve(host: str, port: int, timeout: float) -> str:
        if host == "feeds.example":
            return "93.184.216.34"
        raise DiscoveryError("http_private_address")

    monkeypatch.setattr(http, "_resolve_public", resolve)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    with HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DiscoveryError, match="^http_private_address$"):
            client.fetch(Source(id="test", name="Test", url="http://feeds.example/rss"))
    assert calls == 1


def test_https_downgrade_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    with HttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(301, headers={"Location": "http://feeds.example/rss"})
        )
    ) as client:
        with pytest.raises(DiscoveryError, match="^http_redirect_downgrade$"):
            client.fetch(Source(id="test", name="Test", url="https://feeds.example/rss"))


def test_real_transport_pins_peer_and_does_not_reuse_tls_across_hostnames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise httpcore's actual Host/SNI path with an in-memory network backend."""
    peers: list[str] = []
    server_names: list[str | None] = []
    writes: list[bytes] = []
    closed: list[bool] = []

    class InMemoryStream:
        def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

        def write(self, buffer: bytes, timeout: float | None = None) -> None:
            writes.append(buffer)

        def close(self) -> None:
            closed.append(True)

        def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> "InMemoryStream":
            assert ssl_context.check_hostname
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            server_names.append(server_hostname)
            return self

        def get_extra_info(self, info: str) -> None:
            return None

    class InMemoryBackend:
        def connect_tcp(self, host: str, **kwargs: object) -> InMemoryStream:
            peers.append(host)
            assert kwargs["timeout"] == 5.0
            return InMemoryStream()

    monkeypatch.setattr(http, "_resolve_public", lambda *args: "93.184.216.34")
    with HttpClient() as client:
        monkeypatch.setattr(
            client._client._transport._inner._pool, "_network_backend", InMemoryBackend()
        )
        for index, host in enumerate(("first.example", "second.example")):
            assert (
                client.fetch(
                    Source(id=f"source{index}", name="Test", url=f"https://{host}/rss")
                ).body
                == b"ok"
            )
    assert peers == ["93.184.216.34", "93.184.216.34"]
    assert server_names == ["first.example", "second.example"]
    assert len(closed) == 2
    assert any(b"Host: first.example" in value for value in writes)
    assert any(b"Host: second.example" in value for value in writes)
