"""Expired gateway-stage recovery stays inside the Controller quarantine path."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_independent_bridge import read_inbox, seed_bridge
from test_independent_contracts import NOW

from waagd_discovery.bridge import Bridge
from waagd_discovery.editorial.cli import _check_gateway_batch, main
from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.store import EditorialStore


def _arguments(tmp_path: Path, bridge: Bridge) -> tuple[list[str], Path, Path]:
    root = tmp_path.resolve()
    config = root / "editorial.yaml"
    config.write_text(
        "editorial: {enabled: true}\nnetwork: {enabled: true}\nllm: {enabled: true}\n"
    )
    policy_directory = root / "policy"
    policy_directory.mkdir(mode=0o700)
    policy_directory.chmod(0o700)
    policy = policy_directory / "trusted.yaml"
    policy.write_text(Path("config/trusted_publishers.yaml").read_text())
    policy.chmod(0o600)
    marker = root / "gateway-called"
    gateway = root / "fake-openclaw"
    gateway.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "raise SystemExit(99)\n"
    )
    gateway.chmod(0o700)
    return (
        [
            "--config",
            str(config),
            "--inbox",
            str(bridge.inbox_dir / "inbox.json"),
            "--store",
            str(root / "editorial" / "editorial.sqlite3"),
            "--export",
            str(bridge.export),
            "--workspace-root",
            str(bridge.workspace_root),
            "--state-dir",
            str(bridge.state_dir),
            "--no-simulation",
            "--allow-ack",
            "--allow-gateway",
            "--gateway-executable",
            str(gateway),
            "--gateway-max-candidates",
            "1",
            "--gateway-max-cost-usd",
            "0.10",
            "--trusted-publishers",
            str(policy),
            "gateway-stage",
        ],
        root / "editorial" / "editorial.sqlite3",
        marker,
    )


def test_gateway_stage_expired_delivery_quarantines_without_gateway_or_ack(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    arguments, store_path, marker = _arguments(tmp_path, bridge)

    assert main(arguments) == 0
    assert capsys.readouterr().out == '{"status":"completed"}\n'
    assert not marker.exists()
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("SELECT status FROM deliveries").fetchone() == ("quarantined",)
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM ack_outbox").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM publication_jobs").fetchone() == (0,)


def test_gateway_stage_expired_delivery_with_gateway_attempt_stays_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    arguments, store_path, marker = _arguments(tmp_path, bridge)
    with EditorialStore(store_path, simulation=False) as store:
        store.register(envelope, NOW.isoformat())
        store.start_attempt(
            envelope["delivery_id"],
            envelope["candidates"][0]["candidate_key"],
            "editorial_gateway",
            NOW.isoformat(),
        )

    assert main(arguments) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "operator_required",
        "code": "editorial_stage_unconfirmed",
    }
    assert not marker.exists()
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_gateway_stage_expired_delivery_with_digest_mismatch_stays_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    envelope["candidates"][0]["title"] = "Tampered but syntactically valid fixture"
    (bridge.inbox_dir / "inbox.json").write_text(json.dumps(envelope))
    arguments, _store_path, marker = _arguments(tmp_path, bridge)

    assert main(arguments) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "operator_required",
        "code": "editorial_stage_unconfirmed",
    }
    assert not marker.exists()
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_gateway_stage_expired_delivery_with_unknown_bridge_authority_stays_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    arguments, _store_path, marker = _arguments(tmp_path, bridge)
    original = Bridge.inspect_delivery
    monkeypatch.setattr(Bridge, "inspect_delivery", lambda _self, _delivery: {})

    assert main(arguments) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "operator_required",
        "code": "editorial_stage_unconfirmed",
    }
    assert not marker.exists()
    assert original(bridge, envelope["delivery_id"])["status"] == "pending"


def test_gateway_batch_keeps_expiry_closed_except_for_gateway_stage(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    inbox = bridge.inbox_dir / "inbox.json"

    with pytest.raises(EditorialError, match="^delivery_expired$"):
        _check_gateway_batch(inbox, 1)
    _check_gateway_batch(inbox, 1, allow_expired=True)


def test_gateway_stage_expiry_exception_does_not_relax_duplicate_guard(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    inbox = bridge.inbox_dir / "inbox.json"
    envelope = read_inbox(bridge)
    envelope["candidates"].append(dict(envelope["candidates"][0]))
    inbox.write_text(json.dumps(envelope))

    with pytest.raises(EditorialError, match="^duplicate_candidate_key$"):
        _check_gateway_batch(inbox, 2, allow_expired=True)
