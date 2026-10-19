"""The host-side hand-off has no agent, network or publisher capability."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.publication_bridge import transfer


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _batch() -> bytes:
    from waagd_discovery.editorial.publication import _content_hash

    value = {
        "version": 1,
        "intent_id": "auto-test-intent",
        "origin": "auto",
        "authorization": {"kind": "discovery", "approved": True},
        "editorial": {
            "decision_id": "decision-test",
            "source_event_id": "source-test",
            "source_attribution": "Fixture",
            "approved_at": "2026-10-20T00:00:00.000Z",
            "publication_policy": {"version": "policy-v1", "outcome": "approved"},
        },
        "post": {
            "type": "news",
            "origin": "auto",
            "url": "https://example.test/story",
            "canonical_url": "https://example.test/story",
            "source_item_id": "waagd:" + hashlib.sha256(b"auto-test-intent").hexdigest(),
            "title": "Title",
            "source": "WAAGD",
            "preview": None,
            "embed": None,
            "body": "Body",
            "references": [{"url": "https://example.test/evidence", "title": "Evidence"}],
        },
    }
    value["content_hash"] = _content_hash(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def test_transfer_is_private_atomic_and_preserves_validated_bytes(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source = _private(root / "export")
    state = _private(root / "state")
    data = _batch()
    (source / "publication-intent.json").write_bytes(data)
    (source / "publication-intent.json").chmod(0o600)
    assert transfer(source / "publication-intent.json", state) == {
        "status": "transferred",
        "file": "publication-intent.json",
    }
    target = state / "publication-intent.json"
    assert target.read_bytes() == data
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "mutation", ["bad_batch", "tampered_hash", "permissive_source", "same_directory"]
)
def test_transfer_rejects_untrusted_paths_and_batches(tmp_path: Path, mutation: str) -> None:
    root = tmp_path.resolve()
    source = _private(root / "export")
    state = _private(root / "state")
    path = source / "publication-intent.json"
    data = _batch()
    if mutation == "bad_batch":
        data = b"{}"
    elif mutation == "tampered_hash":
        fixture = (
            Path(__file__).parent / "fixtures" / "publication-intent-v1-invalid-content-hash.json"
        )
        data = fixture.read_bytes()
    path.write_bytes(data)
    path.chmod(0o600 if mutation != "permissive_source" else 0o644)
    if mutation == "same_directory":
        path = state / "publication-intent.json"
        path.write_bytes(_batch())
        path.chmod(0o600)
        source = state
    with pytest.raises(EditorialError):
        transfer(path, state)


def test_transfer_never_uses_ambient_umask(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source = _private(root / "export")
    state = _private(root / "state")
    path = source / "publication-intent.json"
    path.write_bytes(_batch())
    path.chmod(0o600)
    original = os.umask(0o077)
    try:
        transfer(path, state)
    finally:
        os.umask(original)
    assert (state / "publication-intent.json").stat().st_mode & 0o777 == 0o600
