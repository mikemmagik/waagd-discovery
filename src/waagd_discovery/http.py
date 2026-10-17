"""Bounded feed requests with public-address DNS pinning and safe error codes."""

import ipaddress
import math
import queue
import socket
import threading
import time
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType

import httpx

from waagd_discovery.models import DiscoveryError, FetchResult, HttpSettings, Source

_REDIRECTS = {301, 302, 303, 307, 308}
_DEFAULT_SETTINGS = HttpSettings()


def _validate_url(value: str) -> httpx.URL:
    try:
        if len(value) > 4096 or any(ord(character) < 33 for character in value):
            raise ValueError
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            raise ValueError
        if "%" in url.host or url.fragment or not 1 <= (url.port or 443) <= 65535:
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise DiscoveryError("http_url") from None
    return url


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global and not address.is_multicast and not address.is_reserved


def _resolve_public(host: str, port: int, timeout: float) -> str:
    """Resolve once, reject mixed public/private answers, and return a pinned IP.

    A daemon worker bounds caller DNS latency even if the OS resolver stalls.
    At most one resolver worker is used per request; the client never retries DNS
    timeouts, preventing an unresponsive resolver from accumulating retry workers.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not _public_ip(str(address)):
            raise DiscoveryError("http_private_address")
        return str(address)
    result: queue.Queue[list[str] | None] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(str(record[4][0]) for record in records))
        except OSError:
            result.put(None)
        else:
            result.put(addresses)

    threading.Thread(target=resolve, name="feed-dns", daemon=True).start()
    try:
        addresses = result.get(timeout=timeout)
    except queue.Empty:
        raise DiscoveryError("http_dns_timeout") from None
    if not addresses:
        raise DiscoveryError("http_dns")
    if not all(_public_ip(value) for value in addresses):
        raise DiscoveryError("http_private_address")
    # Prefer IPv4 on hosts that advertise IPv6 without having usable IPv6 routing.
    return next((value for value in addresses if ":" not in value), addresses[0])


class _PublicTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, connect_timeout: float, pin: bool) -> None:
        self._inner = inner
        self._connect_timeout = connect_timeout
        self._pin = pin

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = _validate_url(str(request.url))
        address = _resolve_public(
            url.host, url.port or (443 if url.scheme == "https" else 80), self._connect_timeout
        )
        if not self._pin:
            # An injected transport has no sockets, but still exercises validation.
            return self._inner.handle_request(request)
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = url.host
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        # httpcore connects to this literal address; TLS verifies the original
        # hostname via sni_hostname. A second DNS lookup cannot change the peer.
        return self._inner.handle_request(pinned)

    def close(self) -> None:
        self._inner.close()


class HttpClient:
    """Synchronous, read-only HTTP client for configured feed sources."""

    def __init__(
        self,
        settings: HttpSettings = _DEFAULT_SETTINGS,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (
            settings.max_bytes < 1
            or settings.max_redirects < 0
            or settings.retries < 0
            or any(
                not math.isfinite(value) or value <= 0
                for value in (
                    settings.connect_timeout,
                    settings.read_timeout,
                )
            )
            or any(
                not math.isfinite(value) or value < 0
                for value in (
                    settings.backoff,
                    settings.max_retry_after,
                )
            )
        ):
            raise ValueError("invalid HTTP settings")
        self.settings = settings
        self._client = httpx.Client(
            transport=_PublicTransport(
                transport
                or httpx.HTTPTransport(
                    trust_env=False,
                    retries=0,
                    # Pool origins use pinned IPs. Do not reuse a TLS connection
                    # across distinct domain names that happen to share an IP.
                    limits=httpx.Limits(max_keepalive_connections=0),
                ),
                settings.connect_timeout,
                pin=transport is None,
            ),
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": settings.user_agent, "Accept-Encoding": "identity"},
        )

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch(self, source: Source) -> FetchResult:
        original = _validate_url(source.url)
        headers: dict[str, str] = {}
        for name, value in (
            ("If-None-Match", source.etag),
            ("If-Modified-Since", source.last_modified),
        ):
            if (
                value
                and len(value) <= 4096
                and value.isascii()
                and not any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                headers[name] = value
        for attempt in range(self.settings.retries + 1):
            delay: float | None = None
            url = original
            try:
                for redirect in range(self.settings.max_redirects + 1):
                    # Do not send saved cookies or source validators across origins.
                    request_headers = headers if _origin(url) == _origin(original) else {}
                    self._client.cookies.clear()
                    deadline = time.monotonic() + (
                        self.settings.connect_timeout + self.settings.read_timeout
                    )
                    with self._client.stream("GET", url, headers=request_headers) as response:
                        status = response.status_code
                        if status in _REDIRECTS:
                            location = response.headers.get("location")
                            if not location or redirect == self.settings.max_redirects:
                                raise DiscoveryError("http_redirect")
                            try:
                                next_url = _validate_url(str(url.join(location)))
                            except (ValueError, httpx.InvalidURL):
                                raise DiscoveryError("http_redirect") from None
                            if url.scheme == "https" and next_url.scheme != "https":
                                raise DiscoveryError("http_redirect_downgrade")
                            url = next_url
                            continue
                        if status == 429 or 500 <= status <= 599:
                            if attempt == self.settings.retries:
                                raise DiscoveryError(f"http_status_{status}")
                            delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                            break
                        if status not in {200, 304}:
                            raise DiscoveryError(f"http_status_{status}")
                        body = b"" if status == 304 else self._read_body(response, deadline)
                        return FetchResult(
                            status_code=status,
                            body=body,
                            etag=_safe_validator(response.headers.get("etag")),
                            last_modified=_safe_validator(response.headers.get("last-modified")),
                        )
            except httpx.TimeoutException:
                if attempt == self.settings.retries:
                    raise DiscoveryError("http_timeout") from None
                delay = self._retry_delay(None, attempt)
            except httpx.HTTPError:
                if attempt == self.settings.retries:
                    raise DiscoveryError("http_network") from None
                delay = self._retry_delay(None, attempt)
            if delay is not None:
                time.sleep(delay)
        raise DiscoveryError("http_network")  # Defensive: all attempts return or raise.

    def _retry_delay(self, header: str | None, attempt: int) -> float:
        if header is not None:
            try:
                if header.strip().isdigit():
                    value = float(header)
                else:
                    date = parsedate_to_datetime(header)
                    if date.tzinfo is None:
                        raise ValueError
                    value = max(0.0, (date - datetime.now(UTC)).total_seconds())
                if math.isfinite(value):
                    return min(value, self.settings.max_retry_after)
            except (ValueError, TypeError, OverflowError):
                pass
        return min(self.settings.backoff * (2.0 ** min(attempt, 60)), self.settings.max_retry_after)

    def _read_body(self, response: httpx.Response, deadline: float) -> bytes:
        length = response.headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                raise DiscoveryError("http_content_length") from None
            if size < 0:
                raise DiscoveryError("http_content_length")
            if size > self.settings.max_bytes:
                raise DiscoveryError("http_size")
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if response.is_stream_consumed:
            # httpx eagerly decodes responses constructed with content= in mock
            # transports. Normal network responses are always streamed raw.
            encoding = "identity"
        decoder = None
        if encoding in {"gzip", "x-gzip", "deflate"}:
            decoder = zlib.decompressobj(31 if encoding != "deflate" else zlib.MAX_WBITS)
        elif encoding != "identity":
            raise DiscoveryError("http_encoding")
        body = bytearray()
        wire_bytes = 0
        try:
            for chunk in _raw_chunks(response):
                if time.monotonic() > deadline:
                    raise DiscoveryError("http_deadline")
                wire_bytes += len(chunk)
                if wire_bytes > self.settings.max_bytes:
                    raise DiscoveryError("http_size")
                data = (
                    decoder.decompress(chunk, self.settings.max_bytes - len(body) + 1)
                    if decoder
                    else chunk
                )
                body.extend(data)
                if len(body) > self.settings.max_bytes:
                    raise DiscoveryError("http_size")
                if decoder and decoder.unused_data:
                    raise DiscoveryError("http_encoding")
            if decoder and not decoder.eof:
                raise DiscoveryError("http_encoding")
            if time.monotonic() > deadline:
                raise DiscoveryError("http_deadline")
        except zlib.error:
            raise DiscoveryError("http_encoding") from None
        return bytes(body)


def _raw_chunks(response: httpx.Response) -> Iterator[bytes]:
    # MockTransport responses constructed with content= may be pre-consumed.
    if response.is_stream_consumed:
        yield response.content
    else:
        # Keep native network chunk boundaries so even a slow one-byte stream
        # reaches the deadline check; a chunk_size would buffer before yielding.
        yield from response.iter_raw()


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)


def _safe_validator(value: str | None) -> str | None:
    if (
        value
        and len(value) <= 4096
        and value.isascii()
        and all(32 <= ord(character) < 127 for character in value)
    ):
        return value
    return None
