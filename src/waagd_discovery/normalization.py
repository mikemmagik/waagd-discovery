"""Conservative, deterministic HTTP URL identity without changing path semantics."""

import ipaddress
import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import idna

_TRACKING_KEYS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid"}
)
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")


def normalize_url(url: str) -> str:
    """Remove fragments and tracking keys, retaining opaque query/path spelling.

    A query is not parsed and re-encoded: signed URLs, repeated keys, blank
    values, percent encoding, and their order can all carry meaning.
    """
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ValueError("invalid_url")
    if any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise ValueError("invalid_url")
    if "\\" in url or _BAD_PERCENT.search(url):
        raise ValueError("invalid_url")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            raise ValueError("invalid_url")
        if parts.username is not None or parts.password is not None:
            raise ValueError("invalid_url")
        hostname = parts.hostname
        port = parts.port
        if not hostname or "%" in hostname:
            raise ValueError("invalid_url")
        if ":" in hostname:
            host = f"[{ipaddress.IPv6Address(hostname).compressed}]"
        else:
            host = idna.encode(hostname, uts46=True).decode("ascii").lower()
            # Keep the authority syntax strict even after Unicode processing.
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError("invalid_url")
            labels = host.rstrip(".").split(".")
            if len(host) > 254 or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in labels
            ):
                raise ValueError("invalid_url")
        if parts.netloc.endswith(":") or port == 0:
            raise ValueError("invalid_url")
        scheme = parts.scheme.lower()
        authority = host
        if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
            authority += f":{port}"
        query_parts = []
        for parameter in parts.query.split("&"):
            key = unquote_plus(parameter.partition("=")[0]).lower()
            if key.startswith("utm_") or key in _TRACKING_KEYS:
                continue
            query_parts.append(parameter)
        return urlunsplit((scheme, authority, parts.path, "&".join(query_parts), ""))
    except (UnicodeError, ValueError) as error:
        raise ValueError("invalid_url") from error
