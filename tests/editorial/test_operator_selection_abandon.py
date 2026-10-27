"""Operator-only terminal abandon for an expired ambiguous selection."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_independent_bridge import read_inbox, seed_bridge
from test_independent_contracts import NOW

from waagd_discovery.bridge import Bridge
from waagd_discovery.editorial.adapter import MockEditor
from waagd_discovery.editorial.cli import main
from waagd_discovery.editorial.contracts import EditorialError, digest
from waagd_discovery.editorial.controller import Controller
from waagd_discovery.editorial.store import EditorialStore
from waagd_discovery.editorial.verifier import SyntheticVerifier


class NoSelection:
    def __init__(self) -> None:
        self.calls = 0

    def select(self, _batch: object) -> object:
        self.calls += 1
        pytest.fail("operator abandon must not call selection")


def _ambiguous_selection(
    tmp_path: Path,
) -> tuple[Bridge, dict[str, Any], Path, Controller, NoSelection]:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    store_path = tmp_path.resolve() / "editorial" / "editorial.sqlite3"
    with EditorialStore(store_path, simulation=False) as store:
        store.register(envelope, NOW.isoformat())
        store.start_selection(
            envelope["delivery_id"],
            NOW.date().isoformat(),
            [{"candidate_key": envelope["candidates"][0]["candidate_key"]}],
            {},
            NOW.isoformat(),
        )
        attempt = store.start_attempt(envelope["delivery_id"], None, "selection", NOW.isoformat())
        store.begin_selection_call(envelope["delivery_id"])
        store.finish_attempt(attempt, NOW.isoformat(), "selection_outcome_unknown")
        store.mark_delivery(
            envelope["delivery_id"], "operator_required", "selection_outcome_unknown"
        )
    selector = NoSelection()
    controller = Controller(
        bridge.inbox_dir / "inbox.json",
        store_path,
        bridge,
        SyntheticVerifier(),
        MockEditor(),
        simulation=False,
        protected_roots=(bridge.state_dir, bridge.export.parent),
        selector=selector,
    )
    return bridge, envelope, store_path, controller, selector


def _expired() -> object:
    return NOW + timedelta(hours=3)


def _cli_arguments(bridge: Bridge, store_path: Path, delivery: str) -> list[str]:
    return [
        "--inbox",
        str(bridge.inbox_dir / "inbox.json"),
        "--store",
        str(store_path),
        "--export",
        str(bridge.export),
        "--workspace-root",
        str(bridge.workspace_root),
        "--state-dir",
        str(bridge.state_dir),
        "--no-simulation",
        "abandon-ambiguous-selection",
        "--delivery-id",
        delivery,
        "--operator-confirm",
    ]


def test_operator_abandon_expired_ambiguous_selection_is_terminal_and_side_effect_free(
    tmp_path: Path,
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(tmp_path)

    result = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "selection_abandoned"
    assert result["error_code"] == "selection_outcome_unknown_abandoned"
    assert result["operational"]["operator_required"] is False
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute(
            "SELECT status,error_code,ack_status FROM deliveries"
        ).fetchone() == ("selection_abandoned", "selection_outcome_unknown_abandoned", "not_ready")
        assert connection.execute("SELECT status FROM selection_runs").fetchone() == ("in_flight",)
        assert connection.execute(
            "SELECT operation,status,error_code FROM attempts"
        ).fetchone() == ("selection", "failed", "selection_outcome_unknown")
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM ack_outbox").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM publication_jobs").fetchone() == (0,)


@pytest.mark.parametrize("side_effect", ["publication", "ack", "site"])
def test_operator_abandon_refuses_any_downstream_side_effect(
    tmp_path: Path, side_effect: str
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(tmp_path)
    with EditorialStore(store_path, simulation=False) as store, store.transaction():
        if side_effect == "ack":
            store.connection.execute(
                "INSERT INTO ack_outbox VALUES (?,?,?, ?,NULL,NULL)",
                (envelope["delivery_id"], digest(envelope), "pending", NOW.isoformat()),
            )
        else:
            status = "delivered" if side_effect == "site" else "pending"
            store.connection.execute(
                "INSERT INTO publication_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    envelope["candidates"][0]["candidate_key"],
                    1,
                    envelope["delivery_id"],
                    "f" * 64,
                    "{}",
                    status,
                    "delivered" if side_effect == "site" else None,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    None,
                ),
            )

    result = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "operator_required"
    assert result["error_code"] == "selection_outcome_unknown"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_operator_abandon_refuses_digest_or_bridge_authority_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, _store_path, controller, selector = _ambiguous_selection(tmp_path)
    original_envelope = json.loads(json.dumps(envelope))
    envelope["candidates"][0]["title"] = "Tampered fixture title"
    (bridge.inbox_dir / "inbox.json").write_text(json.dumps(envelope))

    mismatch = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert mismatch["status"] == "operator_required"
    assert mismatch["error_code"] == "selection_abandon_inbox_mismatch"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"

    (bridge.inbox_dir / "inbox.json").write_text(json.dumps(original_envelope))
    original = Bridge.inspect_delivery
    monkeypatch.setattr(Bridge, "inspect_delivery", lambda _self, _delivery: {})
    unknown = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert unknown["status"] == "operator_required"
    assert unknown["error_code"] == "selection_abandon_authority_unknown"
    assert selector.calls == 0
    assert original(bridge, envelope["delivery_id"])["status"] == "pending"


def test_operator_abandon_refuses_before_expiry_and_without_confirmation(tmp_path: Path) -> None:
    bridge, envelope, _store_path, controller, selector = _ambiguous_selection(tmp_path)

    with pytest.raises(EditorialError, match="^selection_abandon_requires_expiry$"):
        controller.abandon_ambiguous_selection(
            envelope["delivery_id"], operator_confirm=True, now=NOW
        )
    with pytest.raises(EditorialError, match="^selection_abandon_confirmation_required$"):
        controller.abandon_ambiguous_selection(
            envelope["delivery_id"], operator_confirm=False, now=_expired()
        )
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_operator_abandon_recovers_after_bridge_commit_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(tmp_path)
    original = bridge.abandon_ambiguous_selection

    def commit_then_interrupt(*args: object, **kwargs: object) -> dict[str, Any]:
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "abandon_ambiguous_selection", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        controller.abandon_ambiguous_selection(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("SELECT status FROM deliveries").fetchone() == (
            "selection_abandon_pending",
        )
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"

    monkeypatch.setattr(bridge, "abandon_ambiguous_selection", original)
    recovered = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert recovered["status"] == "selection_abandoned"
    assert selector.calls == 0


def test_operator_abandon_keeps_pending_delivery_on_ambiguous_bridge_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, _store_path, controller, selector = _ambiguous_selection(tmp_path)
    monkeypatch.setattr(
        bridge,
        "abandon_ambiguous_selection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fixture failure")),
    )

    result = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert result["status"] == "operator_required"
    assert result["error_code"] == "selection_outcome_unknown_abandon_uncertain"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"
    assert controller.process(_expired())["status"] == "operator_required"
    assert selector.calls == 0


def test_operator_abandon_is_idempotent_without_gateway_or_bridge_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, _store_path, controller, selector = _ambiguous_selection(tmp_path)
    assert (
        controller.abandon_ambiguous_selection(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )["status"]
        == "selection_abandoned"
    )

    monkeypatch.setattr(
        bridge,
        "abandon_ambiguous_selection",
        lambda *_args, **_kwargs: pytest.fail("completed abandon must not repeat Bridge mutation"),
    )
    repeated = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert repeated["status"] == "selection_abandoned"
    assert selector.calls == 0


def test_operator_abandon_cli_requires_explicit_confirmation_and_never_uses_gateway(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge, envelope, store_path, _controller, _selector = _ambiguous_selection(tmp_path)
    arguments = _cli_arguments(bridge, store_path, envelope["delivery_id"])

    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "selection_abandoned"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"

    confirm = tmp_path / "confirm"
    confirm.mkdir()
    bridge, envelope, store_path, _controller, _selector = _ambiguous_selection(confirm)
    arguments = _cli_arguments(bridge, store_path, envelope["delivery_id"])
    arguments.remove("--operator-confirm")
    assert main(arguments) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "selection_abandon_confirmation_required",
        "status": "error",
    }
