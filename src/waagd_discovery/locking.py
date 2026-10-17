"""Local-volume advisory serialization for all supported mutating commands."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from waagd_discovery.models import DiscoveryError


@contextmanager
def database_lock(path: Path) -> Iterator[None]:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DiscoveryError("database_busy") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lock_is_held(path: Path) -> bool:
    """Probe a cooperating process's kernel lock without creating/changing files."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
