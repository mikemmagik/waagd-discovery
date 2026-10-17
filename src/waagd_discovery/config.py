"""A small, strictly validated trusted source registry."""

import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from waagd_discovery.models import DiscoveryError, Source


def load_sources(path: Path) -> list[Source]:
    try:
        if path.stat().st_size > 262_144:
            raise DiscoveryError("registry_too_large")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DiscoveryError("registry_unreadable") from exc
    if not isinstance(data, dict) or set(data) != {"sources"}:
        raise DiscoveryError("registry_invalid")
    rows = data["sources"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise DiscoveryError("registry_invalid")
    result: list[Source] = []
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"id", "name", "url", "type", "enabled"}:
            raise DiscoveryError("registry_invalid")
        if any(not isinstance(row.get(k), str) for k in ("id", "name", "url")):
            raise DiscoveryError("registry_invalid")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", row["id"]) or row["id"] in ids:
            raise DiscoveryError("registry_duplicate_or_invalid_id")
        if not row["name"].strip() or len(row["name"]) > 300 or len(row["url"]) > 4096:
            raise DiscoveryError("registry_invalid")
        if row.get("type", "rss") not in ("rss", "atom"):
            raise DiscoveryError("registry_unsupported_type")
        if not isinstance(row.get("enabled", True), bool):
            raise DiscoveryError("registry_invalid")
        try:
            url = urlsplit(row["url"])
            if url.scheme not in ("http", "https") or not url.hostname or url.username is not None:
                raise ValueError
            if url.fragment or any(c.isspace() for c in row["url"]):
                raise ValueError
            _ = url.port
        except ValueError as exc:
            raise DiscoveryError("registry_invalid_url") from exc
        ids.add(row["id"])
        result.append(Source(**row))
    return result
