"""Autonomous-stage contract tests for the Bridge sync adapter."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waagd_discovery.autonomous_cycle import TrustedStageCommand
from waagd_discovery.bridge import Bridge, BridgeError
from waagd_discovery.bridge.cli import main as bridge_main
from waagd_discovery.bridge.stage_cli import main as stage_main

from .conftest import sample_export


def _bridge(tmp_path: Path) -> Bridge:
    root = tmp_path.resolve()
    exports = root / "exports"
    exports.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    export = exports / "candidates.json"
    export.write_text(json.dumps(sample_export(datetime.now(UTC))))
    return Bridge(export, workspace, root / "bridge-state", schema_version=2)


def _arguments(bridge: Bridge) -> list[str]:
    return [
        "--export",
        str(bridge.export),
        "--workspace-root",
        str(bridge.workspace_root),
        "--state-dir",
        str(bridge.state_dir),
        "--schema-version",
        "2",
    ]


def _module_stage(root: Path, module: str, arguments: list[str]) -> TrustedStageCommand:
    executable = root / (module.rsplit(".", maxsplit=1)[-1] + ".py")
    executable.write_text(
        f"#!{sys.executable}\nfrom {module} import main\nraise SystemExit(main())\n"
    )
    executable.chmod(0o700)
    return TrustedStageCommand(
        executable,
        tuple(arguments),
        timeout_seconds=30,
    )


def test_bridge_sync_summary_is_not_the_autonomous_stage_protocol(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = _bridge(tmp_path)
    arguments = _arguments(bridge)

    assert bridge_main([*arguments, "sync"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "delivered"
    assert summary["pending_delivery_id"]

    # This reproduces the production failure: a healthy public Bridge CLI
    # summary is deliberately not a generic autonomous-stage success token.
    assert _module_stage(tmp_path, "waagd_discovery.bridge.cli", [*arguments, "sync"]).run() == (
        "operator_required"
    )
    assert (
        _module_stage(tmp_path, "waagd_discovery.bridge.stage_cli", arguments).run() == "completed"
    )

    after = bridge.status(datetime.now(UTC))
    assert after["status"] == "delivered"
    assert after["pending_delivery_id"] == summary["pending_delivery_id"]
    assert after["delivered"] == 3


def test_stage_adapter_accepts_a_proven_empty_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge = _bridge(tmp_path)
    bridge.export.write_text(json.dumps(sample_export(datetime.now(UTC), count=0)))

    assert stage_main(_arguments(bridge)) == 0
    assert capsys.readouterr().out == '{"status":"completed"}\n'
    assert bridge.status(datetime.now(UTC))["status"] == "idle"


@pytest.mark.parametrize(
    "summary",
    [
        {},
        {"status": "error"},
        {
            "status": "pending_expired",
            "pending_delivery_id": "e3ebd328-b49f-4927-a394-1c1671e67d05",
            "prepared": 0,
            "delivered": 3,
            "processed": 0,
            "dismissed": 0,
            "known": 3,
        },
        {
            "status": "delivered",
            "pending_delivery_id": "not-a-delivery-id",
            "prepared": 0,
            "delivered": 3,
            "processed": 0,
            "dismissed": 0,
            "known": 3,
        },
        {
            "status": "delivered",
            "pending_delivery_id": "e3ebd328-b49f-4927-a394-1c1671e67d05",
            "prepared": 1,
            "delivered": 2,
            "processed": 0,
            "dismissed": 0,
            "known": 3,
        },
        {
            "status": "idle",
            "pending_delivery_id": None,
            "prepared": 0,
            "delivered": 0,
            "processed": 0,
            "dismissed": 0,
            "known": 1,
        },
    ],
)
def test_stage_adapter_rejects_unconfirmed_or_expired_bridge_summaries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    summary: dict[str, object],
) -> None:
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(Bridge, "sync", lambda _self: summary)

    assert stage_main(_arguments(bridge)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "operator_required",
        "code": "bridge_sync_unconfirmed",
    }


def test_stage_adapter_fails_closed_when_bridge_cannot_attest_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge(tmp_path)

    def failed_sync(_self: Bridge) -> dict[str, object]:
        raise BridgeError("synthetic")

    monkeypatch.setattr(Bridge, "sync", failed_sync)
    assert stage_main(_arguments(bridge)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "operator_required",
        "code": "bridge_sync_failed",
    }
