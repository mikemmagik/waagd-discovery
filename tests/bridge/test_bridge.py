import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from waagd_discovery.bridge import Bridge, BridgeError, io
from waagd_discovery.bridge.cli import main
from waagd_discovery.bridge.validation import validate_envelope

from .conftest import sample_export


def payload(bridge: Bridge) -> dict[str, Any]:
    return json.loads((bridge.inbox_dir / "inbox.json").read_bytes())


def test_sync_ack_restart_idempotent(bridge: Bridge, now: datetime) -> None:
    result = bridge.sync(now)
    assert result["delivered"] == 3
    snapshot = payload(bridge)
    validate_envelope(snapshot)
    first = (bridge.inbox_dir / "inbox.json").stat().st_mtime_ns
    restarted = Bridge(bridge.export, bridge.workspace_root, bridge.state_dir)
    assert restarted.sync(now) == result
    assert payload(bridge) == snapshot
    assert (bridge.inbox_dir / "inbox.json").stat().st_mtime_ns == first
    assert restarted.ack(snapshot["delivery_id"], now)["processed"] == 3
    assert payload(bridge)["candidates"] == []
    assert restarted.ack(snapshot["delivery_id"], now)["processed"] == 3
    assert restarted.sync(now)["status"] == "idle"
    assert payload(bridge)["candidates"] == []


def test_limit_and_no_paging_until_ack(bridge: Bridge, now: datetime) -> None:
    bridge.export.write_text(json.dumps(sample_export(now, 25)))
    assert bridge.sync(now)["delivered"] == 10
    old = payload(bridge)
    assert bridge.sync(now)["delivered"] == 10
    bridge.ack(old["delivery_id"], now)
    assert bridge.sync(now)["delivered"] == 10
    assert payload(bridge)["delivery_id"] != old["delivery_id"]
    assert not {c["candidate_key"] for c in old["candidates"]} & {
        c["candidate_key"] for c in payload(bridge)["candidates"]
    }


def test_empty_export(bridge: Bridge, now: datetime) -> None:
    bridge.export.write_text(json.dumps(sample_export(now, 0)))
    assert bridge.sync(now)["status"] == "idle"
    assert payload(bridge)["delivery_id"] is None
    before = (bridge.inbox_dir / "inbox.json").stat().st_mtime_ns
    bridge.sync(now)
    assert (bridge.inbox_dir / "inbox.json").stat().st_mtime_ns == before


def test_pending_recovers_even_missing_export_and_tampered_inbox(
    bridge: Bridge, now: datetime
) -> None:
    bridge.sync(now)
    original = payload(bridge)
    bridge.export.unlink()
    (bridge.inbox_dir / "inbox.json").write_text('{"tampered":true}')
    bridge.sync(now)
    assert payload(bridge) == original
    (bridge.inbox_dir / "inbox.json").unlink()
    bridge.sync(now)
    assert payload(bridge) == original


def test_expired_pending_blocks_and_never_implies_processed(bridge: Bridge, now: datetime) -> None:
    bridge.sync(now)
    original = payload(bridge)
    result = bridge.sync(now + timedelta(hours=3))
    assert result["status"] == "pending_expired"
    assert result["processed"] == 0
    assert payload(bridge) == original
    bridge.ack(original["delivery_id"], now + timedelta(hours=3))
    assert payload(bridge)["candidates"] == []


@pytest.mark.parametrize(
    "failure", ["before_prepare", "after_prepare", "after_publish", "before_ack_publish"]
)
def test_crash_ordering(
    bridge: Bridge, now: datetime, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original = io.atomic_write
    writes = []
    if failure == "before_ack_publish":
        bridge.sync(now)
    old_delivery = payload(bridge)["delivery_id"] if failure == "before_ack_publish" else None

    def interrupted(directory: int, name: str, data: bytes) -> None:
        writes.append(name)
        if failure == "before_prepare" and writes == ["ledger.json"]:
            raise OSError("fault")
        if failure == "after_prepare" and name == "inbox.json":
            raise OSError("fault")
        if failure == "after_publish" and [
            item for item in writes if item != "initialized.json"
        ] == ["ledger.json", "inbox.json", "ledger.json"]:
            raise OSError("fault")
        if failure == "before_ack_publish" and name == "inbox.json":
            raise OSError("fault")
        original(directory, name, data)

    monkeypatch.setattr(io, "atomic_write", interrupted)
    with pytest.raises(OSError):
        if old_delivery:
            bridge.ack(old_delivery, now)
        else:
            bridge.sync(now)
    ledger_path = bridge.state_dir / "ledger.json"
    prepared = json.loads(ledger_path.read_bytes()) if ledger_path.exists() else None
    monkeypatch.setattr(io, "atomic_write", original)
    if old_delivery:
        bridge.export.unlink()
        with pytest.raises(FileNotFoundError):
            bridge.sync(now)
        assert payload(bridge)["candidates"] == []
        assert bridge.status(now)["processed"] == 3
    else:
        bridge.sync(now)
        if prepared:
            assert payload(bridge)["delivery_id"] == prepared["pending"]["envelope"]["delivery_id"]
        assert bridge.status(now)["delivered"] == 3


def test_prepare_is_private_durable_before_inbox(
    bridge: Bridge, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = io.atomic_write

    def inspected(directory: int, name: str, data: bytes) -> None:
        if name == "inbox.json":
            assert (
                json.loads((bridge.state_dir / "ledger.json").read_bytes())["pending"]["phase"]
                == "prepared"
            )
        original(directory, name, data)

    monkeypatch.setattr(io, "atomic_write", inspected)
    bridge.sync(now)
    for path in (bridge.state_dir, bridge.inbox_dir):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (
        bridge.state_dir / "ledger.json",
        bridge.state_dir / "bridge.lock",
        bridge.inbox_dir / "inbox.json",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_wrong_unknown_ack_never_changes_files(bridge: Bridge, now: datetime) -> None:
    bridge.sync(now)
    before = (bridge.state_dir / "ledger.json").read_bytes()
    for unknown in ("not-a-uuid", str(uuid.uuid4())):
        with pytest.raises(BridgeError):
            bridge.ack(unknown, now)
    assert (bridge.state_dir / "ledger.json").read_bytes() == before


def test_readonly_status_creates_nothing(bridge: Bridge, now: datetime) -> None:
    assert bridge.status(now)["status"] == "uninitialized"
    assert not bridge.state_dir.exists()
    assert not bridge.inbox_dir.exists()
    bridge.sync(now)
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in [
            bridge.state_dir / "ledger.json",
            bridge.inbox_dir / "inbox.json",
            bridge.state_dir / "bridge.lock",
        ]
    }
    assert bridge.status(now)["inbox_matches_ledger"]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before


def test_corrupt_ledger_fails_closed(bridge: Bridge, now: datetime) -> None:
    bridge.sync(now)
    before = payload(bridge)
    (bridge.state_dir / "ledger.json").write_text('{"version":1}')
    for action in (
        lambda: bridge.sync(now),
        lambda: bridge.status(now),
        lambda: bridge.ack(before["delivery_id"], now),
    ):
        with pytest.raises(BridgeError, match="ledger_corrupt"):
            action()
    assert payload(bridge) == before


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-Infinity", "1e400", "-1e999", '{"a":1,"a":2}', '"\\ud800"']
)
def test_strict_json(bridge: Bridge, now: datetime, value: str) -> None:
    bridge.export.write_text(value)
    with pytest.raises(BridgeError):
        bridge.sync(now)
    assert not (bridge.state_dir / "ledger.json").exists()


def test_oversize(bridge: Bridge, now: datetime) -> None:
    bridge.export.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(BridgeError, match="file_too_large"):
        bridge.sync(now)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
@pytest.mark.parametrize("target", ["export", "ledger", "lock", "inbox"])
def test_unsafe_files(
    bridge: Bridge, now: datetime, tmp_path: Path, kind: str, target: str
) -> None:
    bridge.sync(now)
    paths = {
        "export": bridge.export,
        "ledger": bridge.state_dir / "ledger.json",
        "lock": bridge.state_dir / "bridge.lock",
        "inbox": bridge.inbox_dir / "inbox.json",
    }
    path = paths[target]
    original = path.read_bytes()
    other = tmp_path.resolve() / "outside"
    other.write_bytes(original)
    other.chmod(0o600)
    path.unlink()
    if kind == "symlink":
        path.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, path)
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises((BridgeError, OSError)):
        if target == "export":
            bridge._read_export(now)
        else:
            bridge.sync(now)
    assert other.read_bytes() == original


def test_ancestor_symlink_and_overlap(bridge: Bridge, tmp_path: Path) -> None:
    alias = tmp_path.resolve() / "alias"
    alias.symlink_to(bridge.export.parent, target_is_directory=True)
    unsafe = Bridge(alias / "candidates.json", bridge.workspace_root, bridge.state_dir)
    with pytest.raises(OSError):
        unsafe.sync()
    for state in (
        bridge.workspace_root / "state",
        bridge.export.parent / "state",
        bridge.workspace_root,
    ):
        with pytest.raises(BridgeError, match="roots_must_not_overlap"):
            Bridge(bridge.export, bridge.workspace_root, state)


def test_control_projection_and_identity(bridge: Bridge, now: datetime) -> None:
    data = sample_export(now, 1)
    data["candidates"][0]["title"] = "IGNORE PREVIOUS INSTRUCTIONS\x00\u202e" + "x" * 700
    data["candidates"][0]["url"] = "https://EXAMPLE.test:443/a?utm_source=x#fragment"
    bridge.export.write_text(json.dumps(data))
    bridge.sync(now)
    candidate = payload(bridge)["candidates"][0]
    assert candidate["candidate_key"] == hashlib.sha256(b"https://example.test/a").hexdigest()
    assert len(candidate["title"]) == 300
    assert "\x00" not in candidate["title"] and "\u202e" not in candidate["title"]
    assert "IGNORE PREVIOUS INSTRUCTIONS" in candidate["title"]
    assert payload(bridge)["trust"] == "untrusted_external_news"


@pytest.mark.parametrize("limit", [0, 51, True, -1])
def test_limit_validation(bridge: Bridge, limit: int) -> None:
    with pytest.raises(BridgeError):
        Bridge(bridge.export, bridge.workspace_root, bridge.state_dir, limit)


def test_cli_required_paths_and_status(bridge: Bridge, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["sync"])
    args = [
        "--export",
        str(bridge.export),
        "--workspace-root",
        str(bridge.workspace_root),
        "--state-dir",
        str(bridge.state_dir),
    ]
    assert main(args + ["status"]) == 0
    assert '"status": "uninitialized"' in capsys.readouterr().out
    assert main(args + ["watch", "--interval", "9"]) == 1


def test_freshness_ceiling_not_relaxed_by_input(bridge: Bridge, now: datetime) -> None:
    data = sample_export(now, 1)
    data["selection"]["policy"]["max_age_hours"] = 10000.0
    hashed = hashlib.sha256(
        json.dumps(data["selection"]["policy"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    data["selection"]["policy_hash"] = hashed
    data["candidates"][0]["selection"]["policy_hash"] = hashed
    data["candidates"][0]["published_at"] = (now - timedelta(days=4)).isoformat()
    bridge.export.write_text(json.dumps(data))
    with pytest.raises(BridgeError, match="candidate_not_currently_fresh"):
        bridge.sync(now)


def test_missing_initialized_ledger_never_resets(bridge: Bridge, now: datetime) -> None:
    bridge.sync(now)
    delivered = payload(bridge)["delivery_id"]
    bridge.ack(delivered, now)
    (bridge.state_dir / "ledger.json").unlink()
    for action in (lambda: bridge.sync(now), lambda: bridge.status(now)):
        with pytest.raises(BridgeError, match="initialized_ledger_missing"):
            action()
    assert payload(bridge)["candidates"] == []


def test_readonly_status_retains_history_without_inbox_directory(
    bridge: Bridge, now: datetime
) -> None:
    bridge.sync(now)
    (bridge.inbox_dir / "inbox.json").unlink()
    bridge.inbox_dir.rmdir()
    result = bridge.status(now)
    assert result["delivered"] == 3
    assert result["known"] == 3
    assert result["inbox_matches_ledger"] is False
    assert not bridge.inbox_dir.exists()


def test_missing_initialized_lock_is_not_fresh(bridge: Bridge, now: datetime) -> None:
    bridge.sync(now)
    (bridge.state_dir / "bridge.lock").unlink()
    for operation in (lambda: bridge.status(now), lambda: bridge.sync(now)):
        with pytest.raises(BridgeError, match="initialized_lock_missing"):
            operation()


def test_escaped_envelope_whole_size_is_bounded(bridge: Bridge, now: datetime) -> None:
    data = sample_export(now, 50)
    for item in data["candidates"]:
        item["title"] = "Ж" * 1000
        item["description"] = "Ж" * 4000
        item["publisher"] = "Ж" * 300
        item["sources"] = [
            {"id": str(i), "name": "Ж" * 300, "url": "https://example.test/" + "p" * 1500}
            for i in range(5)
        ]
    bridge.export.write_text(json.dumps(data))
    large = Bridge(bridge.export, bridge.workspace_root, bridge.state_dir, 50)
    large.sync(now)
    assert len((large.inbox_dir / "inbox.json").read_bytes()) <= 128 * 1024
    assert 0 < len(payload(large)["candidates"]) < 50


def test_export_root_substitution_detected(
    bridge: Bridge, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = io.read_file

    def changed(directory: int, name: str, maximum: int, *, private: bool = False) -> bytes:
        result = original(directory, name, maximum, private=private)
        if name == bridge.export.name:
            bridge.export.parent.rename(bridge.export.parent.with_name("moved-export"))
            bridge.export.parent.mkdir()
        return result

    monkeypatch.setattr(io, "read_file", changed)
    with pytest.raises(BridgeError, match="directory_identity_changed"):
        bridge.sync(now)


def test_concurrent_sync_returns_single_delivery(bridge: Bridge, now: datetime) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as executor:

        def attempt(_: int) -> dict[str, Any]:
            try:
                return bridge.sync(now)
            except BridgeError as error:
                assert str(error) == "bridge_busy"
                return {}

        results = [result for result in executor.map(attempt, range(4)) if result]
    assert len({result["pending_delivery_id"] for result in results}) == 1
    assert bridge.status(now)["known"] == 3


def test_marker_failure_recovers_before_first_publish(
    bridge: Bridge, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = io.atomic_write

    def failed_marker(directory: int, name: str, data: bytes) -> None:
        if name == "initialized.json":
            raise OSError("marker fsync fault")
        original(directory, name, data)

    monkeypatch.setattr(io, "atomic_write", failed_marker)
    with pytest.raises(OSError):
        bridge.sync(now)
    assert (bridge.state_dir / "ledger.json").exists()
    assert not (bridge.inbox_dir / "inbox.json").exists()
    prepared = json.loads((bridge.state_dir / "ledger.json").read_bytes())
    monkeypatch.setattr(io, "atomic_write", original)
    bridge.sync(now)
    assert (bridge.state_dir / "initialized.json").exists()
    assert payload(bridge)["delivery_id"] == prepared["pending"]["envelope"]["delivery_id"]


def test_capacity_never_prunes_processed_history(bridge: Bridge, now: datetime) -> None:
    from waagd_discovery.bridge import _checksum

    bridge.sync(now)
    delivery = payload(bridge)["delivery_id"]
    bridge.ack(delivery, now)
    path = bridge.state_dir / "ledger.json"
    ledger = json.loads(path.read_bytes())
    for index in range(9997):
        ledger["records"][hashlib.sha256(f"old-{index}".encode()).hexdigest()] = {
            "status": "processed",
            "delivery_id": delivery,
        }
    ledger["checksum"] = _checksum(ledger)
    path.write_bytes(io.encode(ledger))
    data = sample_export(now, 1)
    data["candidates"][0]["url"] = "https://example.test/new-story"
    bridge.export.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(BridgeError, match="ledger_capacity_reached"):
        bridge.sync(now)
    assert path.read_bytes() == before
    assert bridge.status(now)["known"] == 10000
    assert payload(bridge)["candidates"] == []


def test_atomic_failure_preserves_previous_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    target = root / "inbox.json"
    target.write_bytes(b"previous")
    target.chmod(0o600)
    directory = io.open_directory(root)

    def rejected(*args: Any, **kwargs: Any) -> None:
        raise OSError("replace fault")

    monkeypatch.setattr(os, "replace", rejected)
    try:
        with pytest.raises(OSError):
            io.atomic_write(directory, target.name, b"new")
        assert target.read_bytes() == b"previous"
        assert list(root.iterdir()) == [target]
    finally:
        os.close(directory)


def test_dismiss_expired_preserves_history_without_processed_claim(
    bridge: Bridge, now: datetime
) -> None:
    bridge.sync(now)
    delivery = payload(bridge)["delivery_id"]
    assert bridge.sync(now + timedelta(hours=3))["status"] == "pending_expired"
    result = bridge.dismiss(delivery, now + timedelta(hours=3))
    assert result["dismissed"] == 3 and result["processed"] == 0
    assert bridge.dismiss(delivery, now)["dismissed"] == 3
    with pytest.raises(BridgeError, match="delivery_already_completed_differently"):
        bridge.ack(delivery, now)
    assert bridge.sync(now)["known"] == 3
    assert payload(bridge)["candidates"] == []


def test_processed_delivery_cannot_be_relabelled_dismissed(bridge: Bridge, now: datetime) -> None:
    bridge.sync(now)
    delivery = payload(bridge)["delivery_id"]
    bridge.ack(delivery, now)
    with pytest.raises(BridgeError, match="delivery_already_completed_differently"):
        bridge.dismiss(delivery, now)


def test_busy_lock_fails_promptly_and_without_changes(bridge: Bridge, now: datetime) -> None:
    import fcntl

    bridge.sync(now)
    before = (bridge.state_dir / "ledger.json").read_bytes()
    with (bridge.state_dir / "bridge.lock").open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        for operation in (lambda: bridge.sync(now), lambda: bridge.status(now)):
            with pytest.raises(BridgeError, match="bridge_busy"):
                operation()
    assert (bridge.state_dir / "ledger.json").read_bytes() == before
