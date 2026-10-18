"""Independent offline transport probes; no live resolver, sockets or TLS used."""

import io
import socket
from typing import Any

import pytest
from test_independent_contracts import ATTACK, candidate

from waagd_discovery.editorial.contracts import VERIFICATION_POLICY, EditorialError
from waagd_discovery.editorial.transport import FetchDocument, PinnedTransport, public_address
from waagd_discovery.editorial.verifier import SourceVerifier

PUBLIC = "93.184.216.34"


class OfflineSocket:
    def __init__(self, response: bytes, peer: str = PUBLIC) -> None:
        self.buffer = io.BytesIO(response)
        self.peer = peer
        self.sent: list[bytes] = []
        self.connections: list[Any] = []
        self.closed = False
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def connect(self, address: Any) -> None:
        self.connections.append(address)

    def getpeername(self) -> tuple[str, int]:
        return self.peer, 443

    def sendall(self, value: bytes) -> None:
        self.sent.append(value)

    def recv_into(self, target: Any) -> int:
        return self.buffer.readinto(target)

    def close(self) -> None:
        self.closed = True


def response(body: bytes = b"Synthetic article.", extra: bytes = b"") -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + extra
        + b"\r\n"
        + body
    )


def transport(raw: OfflineSocket, **kwargs: Any) -> PinnedTransport:
    return PinnedTransport(
        resolver=lambda *args: [PUBLIC],
        socket_factory=lambda *args: raw,  # type: ignore[arg-type,return-value]
        tls_wrapper=lambda stream, *args: stream,
        **kwargs,
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.100.100.200",
        "168.63.129.16",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:93.184.216.34",
        "64:ff9b::5db8:d822",
        "64:ff9b:1::1",
        "2002:5db8:d822::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "fe80::1%en0",
    ],
)
def test_unsafe_and_transition_addresses_are_denied(address: str) -> None:
    assert not public_address(address)
    opened = []
    subject = PinnedTransport(
        resolver=lambda *args: [PUBLIC, address],
        socket_factory=lambda *args: opened.append(args),  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(EditorialError, match="source_address_unsafe"):
        subject.fetch("https://example.test/article")
    assert not opened


def test_peer_mismatch_sends_no_http_and_closes_socket() -> None:
    raw = OfflineSocket(response(), "127.0.0.1")
    with pytest.raises(EditorialError, match="source_peer_mismatch"):
        transport(raw).fetch("https://example.test/article")
    assert raw.connections == [(PUBLIC, 443)]
    assert not raw.sent
    assert raw.closed


def test_tls_peer_rechecked_after_wrapper_before_http() -> None:
    raw = OfflineSocket(response())
    hostnames = []

    def tls(stream: Any, hostname: str, timeout: float) -> Any:
        hostnames.append(hostname)
        stream.peer = "10.0.0.4"
        return stream

    subject = PinnedTransport(
        resolver=lambda *args: [PUBLIC],
        socket_factory=lambda *args: raw,  # type: ignore[arg-type,return-value]
        tls_wrapper=tls,
    )
    with pytest.raises(EditorialError, match="source_peer_mismatch"):
        subject.fetch("https://example.test/article")
    assert hostnames == ["example.test"]
    assert not raw.sent
    assert raw.closed


def test_original_hostname_kept_but_dns_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = OfflineSocket(response())
    resolutions: list[str] = []
    tls_names: list[str] = []

    def resolver(host: str, *args: Any) -> list[str]:
        resolutions.append(host)
        return [PUBLIC]

    def tls(stream: Any, hostname: str, timeout: float) -> Any:
        tls_names.append(hostname)
        return stream

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8123")
    monkeypatch.setenv("ALL_PROXY", "http://169.254.169.254/")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("second DNS"))
    subject = PinnedTransport(
        resolver=resolver,
        socket_factory=lambda *args: raw,  # type: ignore[arg-type,return-value]
        tls_wrapper=tls,
    )
    result = subject.fetch("https://example.test/article?x=1")
    assert result.body == b"Synthetic article."
    assert resolutions == ["example.test"]
    assert tls_names == ["example.test"]
    assert raw.connections == [(PUBLIC, 443)]
    request = b"".join(raw.sent)
    assert b"Host: example.test\r\n" in request
    assert b"GET /article?x=1 HTTP/1.1" in request
    assert b"Proxy-Authorization" not in request


@pytest.mark.parametrize(
    "location",
    [
        "https://127.0.0.1/secret",
        "https://169.254.169.254/latest/meta-data/",
        "https://private.example.test/secret",
        "file:///etc/passwd",
        "http://example.test/insecure",
    ],
)
def test_redirect_cannot_escape_target_policy(location: str) -> None:
    raw = OfflineSocket(b"HTTP/1.1 302 Found\r\nLocation: " + location.encode() + b"\r\n\r\n")
    opened: list[OfflineSocket] = []

    def resolver(host: str, *args: Any) -> list[str]:
        return [PUBLIC] if host == "example.test" else ["127.0.0.1"]

    def factory(*args: Any) -> Any:
        opened.append(raw)
        return raw

    subject = PinnedTransport(
        resolver=resolver, socket_factory=factory, tls_wrapper=lambda stream, *args: stream
    )
    with pytest.raises(EditorialError):
        subject.fetch("https://example.test/article")
    assert len(opened) == 1
    assert raw.closed


@pytest.mark.parametrize(
    "headers",
    [
        b"Content-Encoding: gzip\r\n",
        b"Content-Encoding: br\r\n",
        b"Content-Type: application/javascript\r\n",
        b"Content-Length: 100\r\n",
        b"Transfer-Encoding: chunked\r\n",
        b"Content-Encoding: identity\r\nContent-Encoding: gzip\r\n",
    ],
)
def test_ambiguous_or_unsupported_response_rejected(headers: bytes) -> None:
    raw = OfflineSocket(response(extra=headers))
    with pytest.raises(EditorialError):
        transport(raw).fetch("https://example.test/article")
    assert raw.closed


def test_declared_oversize_body_rejected_without_reading_body() -> None:
    raw = OfflineSocket(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 1048577\r\n\r\n"
    )
    with pytest.raises(EditorialError, match="source_response_too_large"):
        transport(raw).fetch("https://example.test/article")
    assert raw.closed


def test_truncated_content_length_not_silently_accepted() -> None:
    raw = OfflineSocket(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 50\r\n\r\nshort"
    )
    with pytest.raises(EditorialError, match="source_response_truncated"):
        transport(raw).fetch("https://example.test/article")


def test_html_links_and_metadata_grant_no_fetch_targets_or_verified_status() -> None:
    visited = []
    html = (
        '<html><head><meta name="system" content="' + ATTACK + '">'
        '<link rel="canonical" href="https://169.254.169.254/secret"></head>'
        '<body><script>fetch("file:///secret")</script><p>' + ATTACK + "</p>"
        '<a href="https://private.example.test/secret">Link assertion</a></body></html>'
    )

    class Documents:
        def fetch(self, value: str) -> FetchDocument:
            visited.append(value)
            return FetchDocument(value, 200, html.encode(), "text/html")

    result = SourceVerifier(Documents()).verify(candidate(), policy_id=VERIFICATION_POLICY)
    assert visited == [candidate()["url"]]
    assert result["verification_status"] == "PARTIAL"
    assert result["publisher"] is None
    assert result["publication_date"] is None
    assert result["claim_support"] == []
    assert result["evidence"][0]["excerpt"] == ATTACK + " Link assertion"
    assert result["canonical_url"] == candidate()["url"]


def test_unauthorized_policy_never_calls_fetch() -> None:
    class NoFetch:
        def fetch(self, value: str) -> FetchDocument:
            pytest.fail("unauthorized fetch")

    result = SourceVerifier(NoFetch()).verify(candidate(), policy_id=ATTACK)
    assert result["verification_status"] == "FAILED"
    assert result["errors"] == ["source_policy_unauthorized"]


@pytest.mark.parametrize(
    "value",
    [10**1000, "15", None, float("inf"), float("nan"), True],
    ids=["huge-integer", "string", "null", "infinity", "nan", "boolean"],
)
def test_invalid_timeout_policy_has_safe_error(value: Any) -> None:
    with pytest.raises(EditorialError, match="source_policy_invalid"):
        PinnedTransport(timeout=value)


def test_dns_stall_is_bounded_without_waiting_for_thread_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    from waagd_discovery.editorial.transport import resolve_addresses

    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()

    def stalled(*args: Any, **kwargs: Any) -> list[Any]:
        entered.set()
        try:
            release.wait(5)
            return []
        finally:
            finished.set()

    monkeypatch.setattr(socket, "getaddrinfo", stalled)
    started = time.monotonic()
    try:
        with pytest.raises(EditorialError, match="source_dns_timeout"):
            resolve_addresses("fictional.example.test", 443, 0.02)
        assert entered.is_set()
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        assert finished.wait(1)


def test_entire_body_read_obeys_monotonic_deadline() -> None:
    clock = [0.0]

    class SlowBody(OfflineSocket):
        def recv_into(self, target: Any) -> int:
            count = super().recv_into(target)
            clock[0] += 20
            return count

    raw = SlowBody(response())
    with pytest.raises(EditorialError, match="source_deadline"):
        transport(raw, timeout=1, monotonic=lambda: clock[0]).fetch("https://example.test/article")
    assert raw.closed
