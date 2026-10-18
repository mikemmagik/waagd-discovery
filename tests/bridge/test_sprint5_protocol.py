"""Additive protocol and conditional receipts preserve the v0.4.1 boundary."""

import hashlib
import json
import stat
import uuid
from datetime import timedelta

import pytest

from waagd_discovery.bridge import Bridge, BridgeError
from waagd_discovery.bridge.io import encode
from waagd_discovery.bridge.validation import VERIFICATION_POLICY, WARNING, validate_envelope

from .conftest import sample_export


def make_bridge(tmp_path, now, version=2):
    root = tmp_path.resolve()
    workspace = root / "workspace"
    workspace.mkdir()
    exports = root / "exports"
    exports.mkdir()
    export = exports / "candidates.json"
    export.write_text(json.dumps(sample_export(now)))
    return Bridge(export, workspace, root / "private", schema_version=version)


def inbox(bridge):
    return json.loads((bridge.inbox_dir / "inbox.json").read_bytes())


def test_v1_default_bytes_and_pending_survive_v2_selection(tmp_path, now):
    first = make_bridge(tmp_path, now, 1)
    first.sync(now)
    original = (first.inbox_dir / "inbox.json").read_bytes()
    assert inbox(first)["handling_policy"] == WARNING
    assert "verification_policy" not in inbox(first)
    upgraded = Bridge(first.export, first.workspace_root, first.state_dir, schema_version=2)
    upgraded.sync(now)
    assert (first.inbox_dir / "inbox.json").read_bytes() == original
    assert json.loads((first.state_dir / "ledger.json").read_bytes())["version"] == 1
    with pytest.raises(BridgeError, match="pending_delivery_blocks_migration"):
        upgraded.migrate_state(now)
    first.ack(inbox(first)["delivery_id"], now)
    with pytest.raises(BridgeError, match="state_migration_required"):
        upgraded.sync(now)


def test_v2_policy_and_authoritative_receipt(tmp_path, now):
    bridge = make_bridge(tmp_path, now)
    bridge.sync(now)
    snapshot = inbox(bridge)
    validate_envelope(snapshot)
    assert snapshot["bridge_schema_version"] == 2
    assert snapshot["verification_policy"] == VERIFICATION_POLICY
    receipt = bridge.inspect_delivery(snapshot["delivery_id"])
    assert receipt["payload_digest"] == hashlib.sha256(encode(snapshot)).hexdigest()
    assert receipt["schema_version"] == receipt["state_version"] == 2
    assert receipt["status"] == "pending"
    for field, bad in (("verification_policy", "allow_shell"), ("handling_policy", WARNING)):
        changed = {**snapshot, field: bad}
        with pytest.raises(BridgeError):
            validate_envelope(changed)


def test_conditional_ack_rejects_wrong_digest_expiry_and_dismissal(tmp_path, now):
    bridge = make_bridge(tmp_path, now)
    bridge.sync(now)
    snapshot = inbox(bridge)
    identifier = snapshot["delivery_id"]
    digest = hashlib.sha256(encode(snapshot)).hexdigest()
    before = (bridge.state_dir / "ledger.json").read_bytes()
    with pytest.raises(BridgeError, match="delivery_digest_mismatch"):
        bridge.ack_if_current(identifier, "0" * 64, now)
    with pytest.raises(BridgeError, match="delivery_expired"):
        bridge.ack_if_current(identifier, digest, now + timedelta(hours=3))
    assert (bridge.state_dir / "ledger.json").read_bytes() == before
    bridge.dismiss(identifier, now)
    assert bridge.inspect_delivery(identifier)["status"] == "dismissed"
    with pytest.raises(BridgeError, match="delivery_not_pending"):
        bridge.ack_if_current(identifier, digest, now)


def test_ack_committed_before_expiry_can_reconcile_after_expiry(tmp_path, now, monkeypatch):
    bridge = make_bridge(tmp_path, now)
    bridge.sync(now)
    identifier = inbox(bridge)["delivery_id"]
    digest = bridge.inspect_delivery(identifier)["payload_digest"]

    def failed_publication(*args):
        raise OSError("test crash after durable ACK")

    monkeypatch.setattr(bridge, "_publish", failed_publication)
    with pytest.raises(OSError):
        bridge.ack_if_current(identifier, digest, now)
    restarted = Bridge(bridge.export, bridge.workspace_root, bridge.state_dir, schema_version=2)
    receipt = restarted.ack_if_current(identifier, digest, now + timedelta(hours=3))
    assert receipt["status"] == "acknowledged"
    assert receipt["acknowledged_at"] == now.isoformat()
    assert inbox(restarted)["candidates"] == []
    assert restarted.status(now)["processed"] == 3
    restarted.sync(now)
    assert restarted.status(now)["processed"] == 3
    assert inbox(restarted)["candidates"] == []


def test_inspect_is_readonly_without_inbox(tmp_path, now):
    bridge = make_bridge(tmp_path, now)
    bridge.sync(now)
    identifier = inbox(bridge)["delivery_id"]
    expected = bridge.inspect_delivery(identifier)
    (bridge.inbox_dir / "inbox.json").unlink()
    bridge.inbox_dir.rmdir()
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in bridge.state_dir.iterdir()}
    assert bridge.inspect_delivery(identifier) == expected
    assert {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in bridge.state_dir.iterdir()
    } == before
    assert not bridge.inbox_dir.exists()


def test_explicit_idle_migration_preserves_history_and_verified_backup(tmp_path, now):
    bridge = make_bridge(tmp_path, now, 1)
    bridge.sync(now)
    identifier = inbox(bridge)["delivery_id"]
    bridge.ack(identifier, now)
    before = (bridge.state_dir / "ledger.json").read_bytes()
    bridge.migrate_state(now)
    after = json.loads((bridge.state_dir / "ledger.json").read_bytes())
    assert after["version"] == 2 and after["receipts"] == {}
    assert after["records"] == json.loads(before)["records"]
    assert after["acknowledged_ids"] == [identifier]
    backups = list(bridge.state_dir.glob("before-state-v2-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert bridge.inspect_delivery(identifier)["payload_digest"] is None
    bridge.migrate_state(now)
    assert list(bridge.state_dir.glob("before-state-v2-*.json")) == backups


def test_migrated_fresh_store_can_conditionally_ack_v1_without_fetching(tmp_path, now):
    bridge = make_bridge(tmp_path, now, 1)
    bridge.migrate_state(now)
    bridge.sync(now)
    snapshot = inbox(bridge)
    assert snapshot["bridge_schema_version"] == 1
    receipt = bridge.inspect_delivery(snapshot["delivery_id"])
    assert (
        bridge.ack_if_current(snapshot["delivery_id"], receipt["payload_digest"], now)["status"]
        == "acknowledged"
    )


@pytest.mark.parametrize("version", [0, 3, True, "2", 2.0, [], {}])
def test_unsupported_versions(version, tmp_path, now):
    with pytest.raises(BridgeError):
        make_bridge(tmp_path, now, version)


def test_protected_ack_creates_nothing_and_unknown_inspect_is_readonly(tmp_path, now):
    bridge = make_bridge(tmp_path, now)
    assert bridge.inspect_delivery(str(uuid.uuid4()))["status"] == "unknown"
    with pytest.raises(BridgeError, match="protected_delivery"):
        bridge.ack_if_current("dbf6d7a2-395e-40c6-8e91-7866aab89e53", "0" * 64, now)
    assert not bridge.state_dir.exists() and not bridge.inbox_dir.exists()
