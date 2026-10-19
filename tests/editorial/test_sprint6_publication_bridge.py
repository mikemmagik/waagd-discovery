"""The host-side hand-off has no agent, network or publisher capability."""

import os
from pathlib import Path

import pytest

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.publication_bridge import transfer


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _batch() -> bytes:
    return io.encode(
        {
            "schema_version": 1,
            "producer": "waagd-discovery-publication-v1",
            "intents": [],
        }
    )


def test_transfer_is_private_atomic_and_preserves_validated_bytes(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source = _private(root / "export")
    state = _private(root / "state")
    data = _batch()
    (source / "publication-intents.json").write_bytes(data)
    (source / "publication-intents.json").chmod(0o600)
    assert transfer(source / "publication-intents.json", state) == {
        "status": "transferred",
        "file": "publication-intents.json",
    }
    target = state / "publication-intents.json"
    assert target.read_bytes() == data
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", ["bad_batch", "permissive_source", "same_directory"])
def test_transfer_rejects_untrusted_paths_and_batches(tmp_path: Path, mutation: str) -> None:
    root = tmp_path.resolve()
    source = _private(root / "export")
    state = _private(root / "state")
    path = source / "publication-intents.json"
    path.write_bytes(_batch() if mutation != "bad_batch" else b"{}")
    path.chmod(0o600 if mutation != "permissive_source" else 0o644)
    if mutation == "same_directory":
        path = state / "publication-intents.json"
        path.write_bytes(_batch())
        path.chmod(0o600)
        source = state
    with pytest.raises(EditorialError):
        transfer(path, state)


def test_transfer_never_uses_ambient_umask(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source = _private(root / "export")
    state = _private(root / "state")
    path = source / "publication-intents.json"
    path.write_bytes(_batch())
    path.chmod(0o600)
    original = os.umask(0o077)
    try:
        transfer(path, state)
    finally:
        os.umask(original)
    assert (state / "publication-intents.json").stat().st_mode & 0o777 == 0o600
