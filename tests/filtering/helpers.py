from datetime import UTC, datetime
from typing import Any

from waagd_discovery.models import ArticleCandidate, FetchResult, Source
from waagd_discovery.storage import Repository

REFERENCE = datetime(2026, 10, 17, 12, tzinfo=UTC)
SOURCE = Source("one", "First source", "https://feeds.example.com/one.xml")
SECOND = Source("two", "Second source", "https://feeds.example.com/two.xml")


def article(url: str = "https://example.com/article", **changes: Any) -> ArticleCandidate:
    fields = {
        "original_url": url,
        "title": "An article",
        "description": "A" * 40,
        "publisher": None,
        "published_at": REFERENCE,
        "date_status": "valid",
    }
    fields.update(changes)
    return ArticleCandidate(**fields)


def seed(repository: Repository, items: list[ArticleCandidate], source: Source = SOURCE) -> None:
    run_id = repository.start_run()
    added, existing, failed = repository.save_source(run_id, source, items, FetchResult(200))
    assert added + existing == len(items) and failed == 0
    repository.finish_run(run_id, "success")
