"""Shared immutable contracts. All datetimes are timezone-aware UTC."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    type: str = "rss"
    enabled: bool = True
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class ArticleCandidate:
    original_url: str
    title: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    date_status: Literal["valid", "missing", "invalid"] = "missing"
    external_guid: str | None = None


@dataclass(frozen=True)
class HttpSettings:
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    max_bytes: int = 2_097_152
    max_redirects: int = 5
    retries: int = 2
    backoff: float = 0.5
    max_retry_after: float = 30.0
    user_agent: str = "WAAGD-Discovery/0.1 (+https://github.com/mikemmagik/waagd-discovery)"


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


@dataclass(frozen=True)
class ParseResult:
    items: list[ArticleCandidate] = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


class DiscoveryError(Exception):
    """Safe fixed error code; never include upstream response bodies or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
