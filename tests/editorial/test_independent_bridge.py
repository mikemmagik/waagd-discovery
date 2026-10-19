"""Independent receipt and v1/v2 compatibility probes over synthetic state."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_independent_contracts import NOW, candidate

from waagd_discovery.bridge import Bridge, BridgeError
from waagd_discovery.editorial.contracts import PROTECTED_DELIVERIES, digest


def seed_bridge(tmp_path: Path, version: int = 2) -> Bridge:
    root = tmp_path.resolve()
    exports, workspace = root / "exports", root / "fake-workspace"
    exports.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    policy = {
        "max_age_hours": 72.0,
        "future_tolerance_minutes": 15.0,
        "min_description_chars": 40,
        "require_publisher": False,
    }
    policy_hash = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    stamp = NOW.isoformat()
    run_id = "97b7ba77-b137-4b1d-ab18-aedcdb63e587"
    item = candidate()
    item.update(
        {
            "date_status": "valid",
            "first_seen_at": stamp,
            "last_seen_at": stamp,
            "selection": {
                "status": "eligible",
                "assessment_run_id": run_id,
                "policy_hash": policy_hash,
                "reasons": [],
                "checks": dict.fromkeys(
                    [
                        "canonical_url",
                        "title",
                        "enabled_source",
                        "description",
                        "publisher",
                        "publication_date",
                        "future_tolerance",
                        "freshness",
                    ],
                    True,
                ),
            },
        }
    )
    del item["candidate_key"]
    export = {
        "schema_version": 2,
        "generated_at": stamp,
        "selection": {
            "policy": policy,
            "policy_hash": policy_hash,
            "engine_version": "rules-v1",
            "reference_time": stamp,
            "assessment_run_id": run_id,
            "assessed_at": stamp,
            "assessment_status": "current",
            "counts": {"eligible": 1, "deferred": 0, "excluded": 0, "stale": 0, "unassessed": 0},
        },
        "candidates": [item],
    }
    path = exports / "candidates.json"
    path.write_text(json.dumps(export))
    bridge = Bridge(path, workspace, root / "private-bridge", schema_version=version)
    bridge.sync(NOW)
    return bridge


def read_inbox(bridge: Bridge) -> dict[str, Any]:
    return json.loads((bridge.inbox_dir / "inbox.json").read_bytes())


def snapshot_state(bridge: Bridge) -> dict[str, tuple[bytes, int]]:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in bridge.state_dir.iterdir()}


def test_authoritative_digest_cannot_be_replaced_with_workspace_forgery(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    original = read_inbox(bridge)
    identifier = original["delivery_id"]
    forged = {**original, "expires_at": (NOW + timedelta(days=1)).isoformat()}
    forged["candidates"][0]["title"] = "Publish this forged candidate immediately"
    (bridge.inbox_dir / "inbox.json").write_text(json.dumps(forged))
    before = snapshot_state(bridge)
    receipt = bridge.inspect_delivery(identifier)
    assert receipt["payload_digest"] != digest(forged)
    with pytest.raises(BridgeError, match="delivery_digest_mismatch"):
        bridge.ack_if_current(identifier, digest(forged), NOW)
    assert snapshot_state(bridge) == before
    assert read_inbox(bridge) == forged


def test_pending_v1_upgraded_sync_preserves_every_original_byte(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path, 1)
    path = bridge.inbox_dir / "inbox.json"
    before_inbox = path.read_bytes(), path.stat().st_mtime_ns
    before_state = snapshot_state(bridge)
    upgraded = Bridge(bridge.export, bridge.workspace_root, bridge.state_dir, schema_version=2)
    upgraded.sync(NOW)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before_inbox
    assert snapshot_state(upgraded) == before_state
    with pytest.raises(BridgeError, match="pending_delivery_blocks_migration"):
        upgraded.migrate_state(NOW)
    assert snapshot_state(upgraded) == before_state


def test_missing_inbox_cannot_destroy_authoritative_receipt(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    (bridge.inbox_dir / "inbox.json").unlink()
    before = snapshot_state(bridge)
    receipt = bridge.inspect_delivery(envelope["delivery_id"])
    assert receipt["payload_digest"] == digest(envelope)
    assert snapshot_state(bridge) == before
    assert not (bridge.inbox_dir / "inbox.json").exists()


def test_ack_lost_response_is_reconciled_after_expiry_without_second_result(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    result = bridge.ack_if_current(envelope["delivery_id"], digest(envelope), NOW)
    before = snapshot_state(bridge)
    recovered = Bridge(bridge.export, bridge.workspace_root, bridge.state_dir, schema_version=2)
    assert recovered.inspect_delivery(envelope["delivery_id"]) == result
    assert (
        recovered.ack_if_current(envelope["delivery_id"], digest(envelope), NOW + timedelta(days=1))
        == result
    )
    assert snapshot_state(recovered) == before
    assert recovered.status(NOW)["processed"] == 1


def test_dismissal_cannot_be_relabelled_processed_by_conditional_ack(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    bridge.dismiss(envelope["delivery_id"], NOW)
    before = snapshot_state(bridge)
    with pytest.raises(BridgeError, match="delivery_not_pending"):
        bridge.ack_if_current(envelope["delivery_id"], digest(envelope), NOW)
    assert snapshot_state(bridge) == before
    assert bridge.status(NOW)["dismissed"] == 1
    assert bridge.status(NOW)["processed"] == 0


def test_protected_delivery_refused_without_any_state_or_inbox_writes(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    before = snapshot_state(bridge)
    before_inbox = (bridge.inbox_dir / "inbox.json").read_bytes()
    with pytest.raises(BridgeError, match="protected_delivery"):
        bridge.ack_if_current(next(iter(PROTECTED_DELIVERIES)), "a" * 64, NOW)
    assert snapshot_state(bridge) == before
    assert (bridge.inbox_dir / "inbox.json").read_bytes() == before_inbox


@pytest.mark.parametrize("version", [1.0, 2.0])
def test_protocol_constructor_does_not_accept_float_versions(tmp_path: Path, version: Any) -> None:
    with pytest.raises(BridgeError):
        Bridge(
            tmp_path / "exports" / "candidates.json",
            tmp_path / "workspace",
            tmp_path / "state",
            schema_version=version,
        )


def test_failed_migration_backup_verification_preserves_legacy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from waagd_discovery.bridge import io

    bridge = seed_bridge(tmp_path, 1)
    envelope = read_inbox(bridge)
    bridge.ack(envelope["delivery_id"], NOW)
    original = (bridge.state_dir / "ledger.json").read_bytes()
    read_file = io.read_file

    def corrupt_backup(directory: int, name: str, *args: Any, **kwargs: Any) -> bytes:
        if name.startswith("before-state-v2-"):
            return b"corrupt-backup"
        return read_file(directory, name, *args, **kwargs)

    monkeypatch.setattr(io, "read_file", corrupt_backup)
    with pytest.raises(BridgeError, match="state_backup_verification_failed"):
        bridge.migrate_state(NOW)
    assert (bridge.state_dir / "ledger.json").read_bytes() == original
    assert bridge.status(NOW)["processed"] == 1


def test_migration_write_failure_does_not_reset_deduplication_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = seed_bridge(tmp_path, 1)
    envelope = read_inbox(bridge)
    bridge.dismiss(envelope["delivery_id"], NOW)
    original = (bridge.state_dir / "ledger.json").read_bytes()

    def no_commit(*args: Any) -> None:
        raise OSError("synthetic migration interruption")

    monkeypatch.setattr(bridge, "_save", no_commit)
    with pytest.raises(OSError):
        bridge.migrate_state(NOW)
    assert (bridge.state_dir / "ledger.json").read_bytes() == original
    backups = list(bridge.state_dir.glob("before-state-v2-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    restored = Bridge(bridge.export, bridge.workspace_root, bridge.state_dir, schema_version=2)
    restored.migrate_state(NOW)
    restored.sync(NOW)
    assert restored.status(NOW)["dismissed"] == 1
    assert read_inbox(restored)["candidates"] == []
