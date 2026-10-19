"""Minimal host-side transfer of a verified publication batch into private state."""

import os
from pathlib import Path
from typing import Any

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.publication import validate_publication_intent

_NAME = "publication-intent.json"
_MAXIMUM = 256 * 1024


def _valid_batch(value: Any) -> bool:
    try:
        validate_publication_intent(value)
        return True
    except EditorialError:
        return False


def transfer(source: Path, state_directory: Path) -> dict[str, object]:
    """Copy one opaque validated batch with descriptor-relative atomic replacement."""
    io.path_parts(source)
    io.path_parts(state_directory)
    if source.name != _NAME or source.parent == state_directory:
        raise EditorialError("publication_bridge_path_invalid")
    origin = io.open_directory(source.parent, private=True)
    destination = io.open_directory(state_directory, private=True)
    try:
        data = io.read_file(origin, _NAME, _MAXIMUM, private=True)
        if not _valid_batch(io.decode(data)):
            raise EditorialError("publication_bridge_batch_invalid")
        io.atomic_write(destination, _NAME, data)
    except io.BridgeError:
        raise EditorialError("publication_bridge_transfer_failed") from None
    finally:
        os.close(destination)
        os.close(origin)
    return {"status": "transferred", "file": _NAME}
