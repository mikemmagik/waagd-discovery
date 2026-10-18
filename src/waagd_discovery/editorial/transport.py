"""Read-only article transport: public DNS pinning and pre-send peer checks.

Resolver/socket/TLS injection is for offline tests, never a configuration switch.
No proxy, cookies, credentials, executable content or second hostname resolution.
"""

import http.client
import io
import ipaddress
import math
import queue
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, cast
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import idna

from waagd_discovery.editorial.contracts import EditorialError

_DNS_WORKERS = threading.BoundedSemaphore(4)
_REDIRECTS = {301, 302, 303, 307, 308}
_DENIED_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "168.63.129.16/32",  # Azure platform/metadata virtual address is globally classified.
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/32",
    )
)


@dataclass(frozen=True)
class FetchDocument:
    url: str
    status: int
    body: bytes
    content_type: str


@dataclass(frozen=True)
class _Target:
    url: str
    scheme: str
    host: str
    port: int
    path: str


def public_address(value: str) -> bool:
    try:
        if "%" in value:
            return False
        address = ipaddress.ip_address(value)
        return (
            address.is_global
            and not address.is_multicast
            and not address.is_reserved
            and not any(address in network for network in _DENIED_NETWORKS)
        )
    except ValueError:
        return False


def _target(value: str, allow_http: bool) -> _Target:
    try:
        if len(value) > 2048 or "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise ValueError
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.fragment or "%" in parsed.hostname:
            raise ValueError
        host = parsed.hostname.lower().rstrip(".")
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = idna.encode(host, uts46=False).decode("ascii")
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if port != (443 if parsed.scheme == "https" else 80):
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
        normalized = urlunsplit((parsed.scheme, authority, path, query, ""))
        if len(normalized) > 2048:
            raise ValueError
        return _Target(normalized, parsed.scheme, host, port, path + ("?" + query if query else ""))
    except (ValueError, UnicodeError, TypeError):
        raise EditorialError("source_url_unsafe") from None


def resolve_addresses(host: str, port: int, timeout: float) -> list[str]:
    """Bound waiting even when the OS resolver stalls; cap abandoned workers."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    result: queue.Queue[list[str] | None] = queue.Queue(maxsize=1)
    deadline = time.monotonic() + timeout
    if not _DNS_WORKERS.acquire(timeout=timeout):
        raise EditorialError("source_dns_timeout")

    def worker() -> None:
        try:
            try:
                records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                addresses = list(dict.fromkeys(str(item[4][0]) for item in records))
            except (OSError, UnicodeError):
                result.put(None)
            else:
                result.put(addresses)
        finally:
            _DNS_WORKERS.release()

    try:
        threading.Thread(target=worker, name="source-dns", daemon=True).start()
    except RuntimeError:
        _DNS_WORKERS.release()
        raise EditorialError("source_dns_failed") from None
    try:
        addresses = result.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise EditorialError("source_dns_timeout") from None
    if not addresses:
        raise EditorialError("source_dns_failed")
    return addresses


class _DeadlineReader(io.RawIOBase):
    def __init__(self, connection: "_DeadlineSocket") -> None:
        self.connection = connection

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self.connection.refresh_timeout()
        size = self.connection.raw.recv_into(buffer)
        self.connection.budget[0] += size
        if self.connection.budget[0] > 1024 * 1024:
            raise EditorialError("source_response_too_large")
        return size

    def close(self) -> None:
        if not self.closed:
            self.connection.release_reader()
        super().close()


class _DeadlineSocket:
    """Every underlying read observes the operation deadline, including headers."""

    def __init__(
        self, raw: socket.socket, deadline: float, clock: Callable[[], float], budget: list[int]
    ) -> None:
        self.raw, self.deadline, self.clock = raw, deadline, clock
        self.budget = budget
        self.readers = 0
        self.closing = False

    def refresh_timeout(self) -> None:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise EditorialError("source_deadline")
        self.raw.settimeout(remaining)

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":
            raise EditorialError("source_transport_failed")
        self.readers += 1
        return io.BufferedReader(_DeadlineReader(self))

    def release_reader(self) -> None:
        self.readers -= 1
        if self.closing and not self.readers:
            self.raw.close()

    def sendall(self, data: bytes) -> None:
        self.refresh_timeout()
        self.raw.sendall(data)

    def close(self) -> None:
        self.closing = True
        if not self.readers:
            self.raw.close()


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, dial: Callable[[], _DeadlineSocket]) -> None:
        super().__init__(host)
        self.dial = dial

    def connect(self) -> None:
        self.sock = cast(socket.socket, self.dial())


class PinnedTransport:
    def __init__(
        self,
        *,
        allow_http: bool = False,
        timeout: float = 15,
        max_bytes: int = 1024 * 1024,
        max_redirects: int = 3,
        resolver: Callable[[str, int, float], list[str]] = resolve_addresses,
        socket_factory: Callable[[int, int], socket.socket] = socket.socket,
        tls_wrapper: Callable[[socket.socket, str, float], socket.socket] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            invalid_timeout = (
                type(timeout) not in {int, float}
                or not math.isfinite(timeout)
                or not 0 < timeout <= 30
            )
        except (TypeError, ValueError, OverflowError):
            invalid_timeout = True
        if (
            type(allow_http) is not bool
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 1024 * 1024
            or type(max_redirects) is not int
            or not 0 <= max_redirects <= 3
            or invalid_timeout
        ):
            raise EditorialError("source_policy_invalid")
        self.allow_http, self.timeout = allow_http, timeout
        self.max_bytes, self.max_redirects = max_bytes, max_redirects
        self.resolver, self.socket_factory = resolver, socket_factory
        self.tls_wrapper, self.clock = tls_wrapper, monotonic

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise EditorialError("source_deadline")
        return remaining

    def _dial(self, target: _Target, deadline: float, budget: list[int]) -> _DeadlineSocket:
        addresses = self.resolver(target.host, target.port, self._remaining(deadline))
        if not addresses or len(addresses) > 64 or not all(public_address(a) for a in addresses):
            raise EditorialError("source_address_unsafe")
        address = next((a for a in addresses if ":" not in a), addresses[0])
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        raw = self.socket_factory(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(self._remaining(deadline))
            raw.connect((address, target.port))
            self._check_peer(raw, address)
            if target.scheme == "https":
                raw.settimeout(self._remaining(deadline))
                if self.tls_wrapper is None:
                    raw = ssl.create_default_context().wrap_socket(raw, server_hostname=target.host)
                else:
                    raw = self.tls_wrapper(raw, target.host, self._remaining(deadline))
                self._check_peer(raw, address)
            self._remaining(deadline)
            return _DeadlineSocket(raw, deadline, self.clock, budget)
        except BaseException:
            raw.close()
            raise

    @staticmethod
    def _check_peer(raw: socket.socket, address: str) -> None:
        try:
            peer = str(raw.getpeername()[0])
            if not public_address(peer) or ipaddress.ip_address(peer) != ipaddress.ip_address(
                address
            ):
                raise ValueError
        except (OSError, ValueError, IndexError, TypeError):
            raise EditorialError("source_peer_mismatch") from None

    def fetch(self, value: str) -> FetchDocument:
        deadline = self.clock() + self.timeout
        budget = [0]
        target = _target(value, self.allow_http)
        try:
            for hop in range(self.max_redirects + 1):
                connection = _PinnedConnection(
                    target.host, partial(self._dial, target, deadline, budget)
                )
                try:
                    authority = f"[{target.host}]" if ":" in target.host else target.host
                    connection.request(
                        "GET",
                        target.path,
                        headers={
                            "Host": authority,
                            "Connection": "close",
                            "Accept-Encoding": "identity",
                            "Accept": "text/html, text/plain",
                            "User-Agent": "WAAGD-SourceVerifier/1",
                        },
                    )
                    response = connection.getresponse()
                    try:
                        if not 100 <= response.status <= 599:
                            raise EditorialError("source_status_invalid")
                        headers = response.getheaders()
                        if sum(len(k) + len(v) for k, v in headers) > 32768:
                            raise EditorialError("source_headers_too_large")
                        if response.status in _REDIRECTS:
                            locations = response.headers.get_all("Location", [])
                            if len(locations) != 1 or hop >= self.max_redirects:
                                raise EditorialError("source_redirect_invalid")
                            following = _target(urljoin(target.url, locations[0]), self.allow_http)
                            if target.scheme == "https" and following.scheme != "https":
                                raise EditorialError("source_redirect_downgrade")
                            target = following
                            continue
                        if response.status != 200:
                            return FetchDocument(target.url, response.status, b"", "")
                        types = response.headers.get_all("Content-Type", [])
                        mime = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
                        if mime not in {"text/plain", "text/html"}:
                            raise EditorialError("source_mime_unsupported")
                        encodings = response.headers.get_all("Content-Encoding", [])
                        if encodings and (
                            len(encodings) != 1 or encodings[0].strip().lower() != "identity"
                        ):
                            raise EditorialError("source_encoding_unsupported")
                        lengths = response.headers.get_all("Content-Length", [])
                        transfer = response.headers.get_all("Transfer-Encoding", [])
                        if (
                            len(lengths) > 1
                            or (lengths and transfer)
                            or (
                                transfer
                                and (len(transfer) != 1 or transfer[0].lower() != "chunked")
                            )
                            or (lengths and (not lengths[0].isascii() or not lengths[0].isdigit()))
                        ):
                            raise EditorialError("source_length_invalid")
                        if lengths and int(lengths[0]) > self.max_bytes:
                            raise EditorialError("source_response_too_large")
                        body = bytearray()
                        while True:
                            self._remaining(deadline)
                            chunk = response.read1(min(65536, self.max_bytes - len(body) + 1))
                            if not chunk:
                                break
                            body.extend(chunk)
                            if len(body) > self.max_bytes:
                                raise EditorialError("source_response_too_large")
                        self._remaining(deadline)
                        if lengths and len(body) != int(lengths[0]):
                            raise EditorialError("source_response_truncated")
                        return FetchDocument(target.url, response.status, bytes(body), mime)
                    finally:
                        response.close()
                finally:
                    connection.close()
        except EditorialError:
            raise
        except TimeoutError:
            raise EditorialError("source_timeout") from None
        except ssl.SSLError:
            raise EditorialError("source_tls_failed") from None
        except (OSError, http.client.HTTPException, UnicodeError, ValueError, OverflowError):
            raise EditorialError("source_network_failed") from None
        raise EditorialError("source_redirect_invalid")
