from collections.abc import Iterator

import pytest

from waagd_discovery.storage import Repository

from .helpers import SECOND, SOURCE


@pytest.fixture
def repository() -> Iterator[Repository]:
    with Repository(":memory:") as instance:
        instance.sync_sources([SOURCE, SECOND])
        yield instance
