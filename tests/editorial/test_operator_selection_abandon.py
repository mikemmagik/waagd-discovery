"""Operator-only terminal abandon for an expired ambiguous selection."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_independent_bridge import read_inbox, seed_bridge
from test_independent_contracts import NOW

from waagd_discovery.bridge import Bridge
from waagd_discovery.editorial.adapter import MockEditor
from waagd_discovery.editorial.cli import main
from waagd_discovery.editorial.contracts import (
    VERIFICATION_POLICY,
    EditorialError,
    digest,
    make_request,
    trusted_decision,
)
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
    *,
    candidate_count: int = 1,
    completed_verifications: bool = False,
    selection_error: str = "selection_outcome_unknown",
) -> tuple[Bridge, dict[str, Any], Path, Controller, NoSelection]:
    bridge = seed_bridge(tmp_path)
    if candidate_count > 1:
        initial = read_inbox(bridge)
        bridge.dismiss(initial["delivery_id"], NOW)
        export = json.loads(bridge.export.read_text())
        source = export["candidates"][0]
        export["candidates"] = []
        for index in range(candidate_count):
            candidate = deepcopy(source)
            candidate["id"] = index + 1
            candidate["url"] = f"https://example.test/selection-abandon/{index}"
            candidate["title"] = f"Synthetic selection candidate {index}"
            export["candidates"].append(candidate)
        export["selection"]["counts"]["eligible"] = candidate_count
        bridge.export.write_text(json.dumps(export))
        bridge.sync(NOW)
    envelope = read_inbox(bridge)
    store_path = tmp_path.resolve() / "editorial" / "editorial.sqlite3"
    with EditorialStore(store_path, simulation=False) as store:
        store.register(envelope, NOW.isoformat())
        store.start_selection(
            envelope["delivery_id"],
            NOW.date().isoformat(),
            [{"candidate_key": candidate["candidate_key"]} for candidate in envelope["candidates"]],
            {},
            NOW.isoformat(),
        )
        if completed_verifications:
            for candidate in envelope["candidates"]:
                verification = store.start_attempt(
                    envelope["delivery_id"],
                    candidate["candidate_key"],
                    "verification",
                    NOW.isoformat(),
                )
                store.finish_attempt(verification, NOW.isoformat())
        attempt = store.start_attempt(envelope["delivery_id"], None, "selection", NOW.isoformat())
        store.begin_selection_call(envelope["delivery_id"])
        store.finish_attempt(attempt, NOW.isoformat(), selection_error)
        if selection_error != "selection_outcome_unknown":
            store.finish_selection(envelope["delivery_id"], [], NOW.isoformat(), selection_error)
        store.mark_delivery(envelope["delivery_id"], "operator_required", selection_error)
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


def _downgrade_partial_batch_store_to_v5(path: Path) -> None:
    """Model the accepted production Store before the v1.0.7 schema migration."""

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE attempt_diagnostics")
        connection.execute("PRAGMA user_version=5")


def _partial_editorial_batch(
    tmp_path: Path,
    *,
    production_shape: bool = False,
) -> tuple[Bridge, dict[str, Any], Path, Controller]:
    bridge, envelope, store_path, controller, _selector = _ambiguous_selection(
        tmp_path, candidate_count=10 if production_shape else 2, completed_verifications=True
    )
    selected = [candidate["candidate_key"] for candidate in envelope["candidates"][:2]]
    with EditorialStore(store_path, simulation=False) as store:
        revisions = dict(store.required(envelope["delivery_id"]))
        if production_shape:
            verifier = SyntheticVerifier()
            for candidate in envelope["candidates"]:
                key = candidate["candidate_key"]
                store.save_verification(
                    key,
                    revisions[key],
                    verifier.verify(candidate, policy_id=VERIFICATION_POLICY),
                    NOW.isoformat(),
                )
        selection = store.connection.execute(
            "SELECT attempt_id FROM attempts WHERE operation='selection'"
        ).fetchone()[0]
        store.finish_attempt(selection, NOW.isoformat())
        store.finish_selection(envelope["delivery_id"], selected, NOW.isoformat())
        completed = selected[0]
        editorial = store.start_attempt(
            envelope["delivery_id"], completed, "editorial_gateway", NOW.isoformat()
        )
        fidelity = store.start_attempt(
            envelope["delivery_id"], completed, "fidelity", NOW.isoformat()
        )
        store.finish_attempts(((editorial, None), (fidelity, None)), NOW.isoformat())
        with store.transaction():
            store.connection.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                (completed, revisions[completed], "READY_FOR_REVIEW", "{}", "{}", NOW.isoformat()),
            )
        store.mark_selection_editorial(envelope["delivery_id"], completed, "completed")
        failed = selected[1]
        failed_attempt = store.start_attempt(
            envelope["delivery_id"], failed, "editorial_gateway", NOW.isoformat()
        )
        store.finish_gateway_attempt(
            failed_attempt,
            NOW.isoformat(),
            "editorial_gateway_attestation_invalid",
            {
                "failure_category": "gateway_model_route_unverified",
                "child_exit_status": 0,
                "elapsed_ms": 189000,
                "terminal_receipt": False,
                "route_attestation": "failed",
                "cost_attestation": "not_checked",
                "tool_attestation": "unknown",
                "terminal_disposition": None,
            },
        )
        store.mark_selection_editorial(envelope["delivery_id"], failed, "failed")
        if production_shape:
            # Six unselected PARTIAL verifications follow GatewayEditor's
            # deterministic no-LLM policy path. The remaining two were
            # rejected by trusted selection without an editorial attempt.
            for candidate in envelope["candidates"][2:8]:
                key = candidate["candidate_key"]
                revision = revisions[key]
                verification = store.verification(key, revision)
                assert verification is not None
                request = make_request(candidate, verification, simulation=False)
                decision = trusted_decision(request, now=NOW)
                attempt = store.start_attempt(
                    envelope["delivery_id"], key, "editorial_gateway", NOW.isoformat()
                )
                store.save_decision(key, revision, request, decision, None, NOW.isoformat())
                store.finish_attempt(attempt, NOW.isoformat())
            for candidate in envelope["candidates"][8:]:
                controller._excluded(
                    store,
                    candidate,
                    revisions[candidate["candidate_key"]],
                    envelope["delivery_id"],
                    NOW,  # noqa: SLF001
                )
        store.mark_delivery(
            envelope["delivery_id"], "operator_required", "editorial_gateway_attestation_invalid"
        )
    return bridge, envelope, store_path, controller


def _cli_arguments(
    bridge: Bridge, store_path: Path, delivery: str, command: str = "abandon-ambiguous-selection"
) -> list[str]:
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
        command,
        "--delivery-id",
        delivery,
        "--operator-confirm",
    ]


def test_partial_editorial_abandon_preserves_fidelity_pass_and_releases_expired_batch(
    tmp_path: Path,
) -> None:
    bridge, envelope, store_path, controller = _partial_editorial_batch(tmp_path)

    result = controller.abandon_partial_editorial_batch(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "partial_editorial_batch_abandoned"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM ack_outbox").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM publication_jobs").fetchone() == (0,)
        assert connection.execute(
            "SELECT failure_category,elapsed_ms,route_attestation FROM attempt_diagnostics"
        ).fetchone() == ("gateway_model_route_unverified", 189000, "failed")


def test_partial_editorial_abandon_accepts_production_nonselected_policy_history_via_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge, envelope, store_path, _controller = _partial_editorial_batch(
        tmp_path, production_shape=True
    )

    assert (
        main(
            _cli_arguments(
                bridge,
                store_path,
                envelope["delivery_id"],
                command="abandon-partial-editorial-batch",
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "partial_editorial_batch_abandoned"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE operation='verification' AND status='completed'"
        ).fetchone() == (10,)
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE operation='selection' AND status='completed'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE operation='editorial_gateway' "
            "AND status='completed'"
        ).fetchone() == (7,)
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE operation='editorial_gateway' AND status='failed'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE decision='NEEDS_VERIFICATION'"
        ).fetchone() == (6,)
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone() == (9,)
        assert connection.execute("SELECT COUNT(*) FROM publication_jobs").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM ack_outbox").fetchone() == (0,)


def test_partial_editorial_abandon_cli_migrates_v5_production_batch_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge, envelope, store_path, _controller = _partial_editorial_batch(
        tmp_path, production_shape=True
    )
    _downgrade_partial_batch_store_to_v5(store_path)
    with closing(sqlite3.connect(store_path)) as connection:
        before_decisions = connection.execute(
            "SELECT candidate_key,revision,decision,request,result,created_at FROM decisions "
            "ORDER BY candidate_key,revision"
        ).fetchall()
        before_attempts = connection.execute(
            "SELECT attempt_id,delivery_id,candidate_key,operation,status,started_at,finished_at,"
            "error_code FROM attempts ORDER BY attempt_id"
        ).fetchall()
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)

    arguments = _cli_arguments(
        bridge, store_path, envelope["delivery_id"], command="abandon-partial-editorial-batch"
    )
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "partial_editorial_batch_abandoned"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "partial_editorial_batch_abandoned"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert (
            connection.execute(
                "SELECT candidate_key,revision,decision,request,result,created_at FROM decisions "
                "ORDER BY candidate_key,revision"
            ).fetchall()
            == before_decisions
        )
        assert (
            connection.execute(
                "SELECT attempt_id,delivery_id,candidate_key,operation,status,started_at,"
                "finished_at,"
                "error_code FROM attempts ORDER BY attempt_id"
            ).fetchall()
            == before_attempts
        )
        assert connection.execute("SELECT COUNT(*) FROM publication_jobs").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM ack_outbox").fetchone() == (0,)


def test_partial_editorial_abandon_v5_preflight_does_not_migrate_before_ttl(
    tmp_path: Path,
) -> None:
    _bridge, envelope, store_path, controller = _partial_editorial_batch(
        tmp_path, production_shape=True
    )
    _downgrade_partial_batch_store_to_v5(store_path)

    with pytest.raises(EditorialError, match="^partial_editorial_abandon_requires_expiry$"):
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=NOW
        )
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)


def test_partial_editorial_abandon_v5_migration_failure_leaves_bridge_and_history_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, store_path, controller = _partial_editorial_batch(
        tmp_path, production_shape=True
    )
    _downgrade_partial_batch_store_to_v5(store_path)
    with closing(sqlite3.connect(store_path)) as connection:
        before = connection.execute("SELECT COUNT(*) FROM decisions").fetchone()
    original = EditorialStore.transaction

    @contextmanager
    def fail_migration(_store: EditorialStore):
        raise sqlite3.OperationalError("synthetic migration failure")
        yield

    monkeypatch.setattr(EditorialStore, "transaction", fail_migration)
    result = controller.abandon_partial_editorial_batch(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    monkeypatch.setattr(EditorialStore, "transaction", original)

    assert result["status"] == "operator_required"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone() == before


def test_partial_editorial_abandon_v5_refuses_corrupt_bridge_receipt_without_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, store_path, controller = _partial_editorial_batch(
        tmp_path, production_shape=True
    )
    _downgrade_partial_batch_store_to_v5(store_path)
    receipt = bridge.inspect_delivery(envelope["delivery_id"])
    receipt["payload_digest"] = "0" * 64
    monkeypatch.setattr(bridge, "inspect_delivery", lambda _delivery: receipt)

    result = controller.abandon_partial_editorial_batch(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["error_code"] == "partial_editorial_abandon_authority_unknown"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)


def test_partial_editorial_abandon_v5_recovers_bridge_commit_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, store_path, controller = _partial_editorial_batch(
        tmp_path, production_shape=True
    )
    _downgrade_partial_batch_store_to_v5(store_path)
    original = bridge.abandon_ambiguous_selection

    def commit_then_interrupt(*args: object, **kwargs: object) -> dict[str, Any]:
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "abandon_ambiguous_selection", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute("SELECT status FROM deliveries").fetchone() == (
            "partial_editorial_abandon_pending",
        )
    monkeypatch.setattr(bridge, "abandon_ambiguous_selection", original)
    assert (
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )["status"]
        == "partial_editorial_batch_abandoned"
    )


@pytest.mark.parametrize("corruption", ["failed_attempt", "wrong_decision"])
def test_partial_editorial_abandon_rejects_nonselected_ambiguous_or_inconsistent_history(
    tmp_path: Path, corruption: str
) -> None:
    bridge, envelope, store_path, controller = _partial_editorial_batch(
        tmp_path, production_shape=True
    )
    key = envelope["candidates"][2]["candidate_key"]
    with EditorialStore(store_path, simulation=False) as store, store.transaction():
        if corruption == "failed_attempt":
            store.connection.execute(
                "UPDATE attempts SET status='failed',error_code='gateway_outcome_unknown' "
                "WHERE delivery_id=? AND candidate_key=? AND operation='editorial_gateway'",
                (envelope["delivery_id"], key),
            )
        else:
            store.connection.execute(
                "UPDATE decisions SET decision='REJECTED' WHERE candidate_key=? "
                "AND revision=(SELECT revision FROM delivery_candidates "
                "WHERE delivery_id=? AND candidate_key=?)",
                (key, envelope["delivery_id"], key),
            )

    result = controller.abandon_partial_editorial_batch(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "operator_required"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_partial_editorial_abandon_refuses_unexpired_unknown_or_downstream_batch(
    tmp_path: Path,
) -> None:
    bridge, envelope, store_path, controller = _partial_editorial_batch(tmp_path)
    with pytest.raises(EditorialError, match="^partial_editorial_abandon_requires_expiry$"):
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=NOW
        )
    with EditorialStore(store_path, simulation=False) as store:
        extra = store.start_attempt(
            envelope["delivery_id"], None, "editorial_gateway", NOW.isoformat()
        )
        del extra
    assert (
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )["status"]
        == "operator_required"
    )
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_partial_editorial_abandon_is_idempotent_after_bridge_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, _store_path, controller = _partial_editorial_batch(tmp_path)
    assert (
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )["status"]
        == "partial_editorial_batch_abandoned"
    )


def test_partial_editorial_abandon_refuses_downstream_evidence(tmp_path: Path) -> None:
    bridge, envelope, store_path, controller = _partial_editorial_batch(tmp_path)
    with EditorialStore(store_path, simulation=False) as store, store.transaction():
        store.connection.execute(
            "INSERT INTO ack_outbox VALUES (?,?,?, ?,NULL,NULL)",
            (envelope["delivery_id"], digest(envelope), "pending", NOW.isoformat()),
        )
    result = controller.abandon_partial_editorial_batch(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert result["status"] == "operator_required"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_partial_editorial_abandon_recovers_after_bridge_commit_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, store_path, controller = _partial_editorial_batch(tmp_path)
    original = bridge.abandon_ambiguous_selection

    def commit_then_interrupt(*args: object, **kwargs: object) -> dict[str, Any]:
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "abandon_ambiguous_selection", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("SELECT status FROM deliveries").fetchone() == (
            "partial_editorial_abandon_pending",
        )
    monkeypatch.setattr(bridge, "abandon_ambiguous_selection", original)
    assert (
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )["status"]
        == "partial_editorial_batch_abandoned"
    )
    monkeypatch.setattr(
        bridge,
        "abandon_ambiguous_selection",
        lambda *_args, **_kwargs: pytest.fail("terminal recovery must not repeat Bridge mutation"),
    )
    assert (
        controller.abandon_partial_editorial_batch(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )["status"]
        == "partial_editorial_batch_abandoned"
    )


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


def test_operator_abandon_allows_completed_verification_history_for_ten_candidates(
    tmp_path: Path,
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(
        tmp_path, candidate_count=10, completed_verifications=True
    )

    result = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "selection_abandoned"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE operation='verification' "
            "AND status='completed' AND error_code IS NULL"
        ).fetchone() == (10,)
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE operation='selection' "
            "AND status='failed' AND error_code='selection_outcome_unknown'"
        ).fetchone() == (1,)


def test_operator_abandon_selection_attestation_failure_with_verification_history(
    tmp_path: Path,
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(
        tmp_path,
        candidate_count=10,
        completed_verifications=True,
        selection_error="selection_attestation_missing",
    )

    result = controller.abandon_selection_attestation(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "selection_attestation_abandoned"
    assert result["error_code"] == "selection_attestation_missing_abandoned"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("SELECT status,error_code FROM deliveries").fetchone() == (
            "selection_attestation_abandoned",
            "selection_attestation_missing_abandoned",
        )
        assert connection.execute("SELECT status,error_code FROM selection_runs").fetchone() == (
            "failed",
            "selection_attestation_missing",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE operation='verification' AND status='completed'"
        ).fetchone() == (10,)
        assert connection.execute("SELECT COUNT(*) FROM ack_outbox").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM publication_jobs").fetchone() == (0,)


def test_selection_attestation_abandon_rejects_unexpired_extra_attempt_or_downstream_state(
    tmp_path: Path,
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(
        tmp_path, selection_error="selection_attestation_missing"
    )
    with pytest.raises(EditorialError, match="^selection_abandon_requires_expiry$"):
        controller.abandon_selection_attestation(
            envelope["delivery_id"], operator_confirm=True, now=NOW
        )
    with EditorialStore(store_path, simulation=False) as store:
        extra = store.start_attempt(envelope["delivery_id"], None, "selection", NOW.isoformat())
        store.finish_attempt(extra, NOW.isoformat(), "selection_attestation_missing")
    result = controller.abandon_selection_attestation(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert result["status"] == "operator_required"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_selection_attestation_abandon_is_idempotent_after_bridge_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, _store_path, controller, selector = _ambiguous_selection(
        tmp_path, selection_error="selection_attestation_missing"
    )
    assert (
        controller.abandon_selection_attestation(
            envelope["delivery_id"], operator_confirm=True, now=_expired()
        )["status"]
        == "selection_attestation_abandoned"
    )
    monkeypatch.setattr(
        bridge,
        "abandon_ambiguous_selection",
        lambda *_args, **_kwargs: pytest.fail("completed recovery must not repeat Bridge mutation"),
    )
    repeated = controller.abandon_selection_attestation(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert repeated["status"] == "selection_attestation_abandoned"
    assert selector.calls == 0


@pytest.mark.parametrize("side_effect", ["ack", "publication"])
def test_selection_attestation_abandon_rejects_downstream_side_effect(
    tmp_path: Path, side_effect: str
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(
        tmp_path, selection_error="selection_attestation_missing"
    )
    with EditorialStore(store_path, simulation=False) as store, store.transaction():
        if side_effect == "ack":
            store.connection.execute(
                "INSERT INTO ack_outbox VALUES (?,?,?, ?,NULL,NULL)",
                (envelope["delivery_id"], digest(envelope), "pending", NOW.isoformat()),
            )
        else:
            store.connection.execute(
                "INSERT INTO publication_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    envelope["candidates"][0]["candidate_key"],
                    1,
                    envelope["delivery_id"],
                    "f" * 64,
                    "{}",
                    "pending",
                    None,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    None,
                ),
            )
    result = controller.abandon_selection_attestation(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert result["status"] == "operator_required"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_selection_attestation_abandon_rejects_inbox_mismatch_and_recovers_bridge_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(
        tmp_path, selection_error="selection_attestation_missing"
    )
    original_envelope = deepcopy(envelope)
    envelope["candidates"][0]["title"] = "tampered fixture"
    (bridge.inbox_dir / "inbox.json").write_text(json.dumps(envelope))
    mismatch = controller.abandon_selection_attestation(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert mismatch["error_code"] == "selection_abandon_inbox_mismatch"
    (bridge.inbox_dir / "inbox.json").write_text(json.dumps(original_envelope))

    original = bridge.abandon_ambiguous_selection

    def commit_then_interrupt(*args: object, **kwargs: object) -> dict[str, Any]:
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "abandon_ambiguous_selection", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        controller.abandon_selection_attestation(
            original_envelope["delivery_id"], operator_confirm=True, now=_expired()
        )
    with closing(sqlite3.connect(store_path)) as connection:
        assert connection.execute("SELECT status FROM deliveries").fetchone() == (
            "selection_attestation_abandon_pending",
        )
    monkeypatch.setattr(bridge, "abandon_ambiguous_selection", original)
    recovered = controller.abandon_selection_attestation(
        original_envelope["delivery_id"], operator_confirm=True, now=_expired()
    )
    assert recovered["status"] == "selection_attestation_abandoned"
    assert selector.calls == 0


def test_operator_abandon_refuses_extra_selection_attempt(tmp_path: Path) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(tmp_path)
    with EditorialStore(store_path, simulation=False) as store:
        extra = store.start_attempt(envelope["delivery_id"], None, "selection", NOW.isoformat())
        store.finish_attempt(extra, NOW.isoformat(), "selection_outcome_unknown")

    result = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "operator_required"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


@pytest.mark.parametrize("status,error", [("started", None), ("failed", "verification_unknown")])
def test_operator_abandon_refuses_unfinished_or_unknown_verification_attempt(
    tmp_path: Path, status: str, error: str | None
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(tmp_path)
    with EditorialStore(store_path, simulation=False) as store:
        attempt = store.start_attempt(
            envelope["delivery_id"],
            envelope["candidates"][0]["candidate_key"],
            "verification",
            NOW.isoformat(),
        )
        if status != "started":
            store.finish_attempt(attempt, NOW.isoformat(), error)

    result = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "operator_required"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_operator_abandon_refuses_verification_for_another_delivery_candidate(
    tmp_path: Path,
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(tmp_path)
    other = deepcopy(envelope)
    other["delivery_id"] = str(uuid.uuid4())
    other["candidates"][0]["candidate_key"] = "e" * 64
    other["candidates"][0]["url"] = "https://example.test/other-delivery-candidate"
    with EditorialStore(store_path, simulation=False) as store:
        store.register(other, NOW.isoformat())
        attempt = store.start_attempt(
            envelope["delivery_id"],
            other["candidates"][0]["candidate_key"],
            "verification",
            NOW.isoformat(),
        )
        store.finish_attempt(attempt, NOW.isoformat())

    result = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "operator_required"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


@pytest.mark.parametrize("operation", ["editorial", "editorial_gateway", "fidelity", "export"])
def test_operator_abandon_refuses_nonverification_downstream_attempt(
    tmp_path: Path, operation: str
) -> None:
    bridge, envelope, store_path, controller, selector = _ambiguous_selection(tmp_path)
    with EditorialStore(store_path, simulation=False) as store:
        attempt = store.start_attempt(
            envelope["delivery_id"],
            envelope["candidates"][0]["candidate_key"],
            operation,
            NOW.isoformat(),
        )
        store.finish_attempt(attempt, NOW.isoformat())

    result = controller.abandon_ambiguous_selection(
        envelope["delivery_id"], operator_confirm=True, now=_expired()
    )

    assert result["status"] == "operator_required"
    assert selector.calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


@pytest.mark.parametrize("side_effect", ["publication", "ack", "site", "decision", "export"])
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
        elif side_effect == "decision":
            store.connection.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                (
                    envelope["candidates"][0]["candidate_key"],
                    1,
                    "REJECTED",
                    "{}",
                    "{}",
                    NOW.isoformat(),
                ),
            )
        elif side_effect == "export":
            store.connection.execute(
                "UPDATE selection_items SET export_status='exported' WHERE run_id="
                "(SELECT run_id FROM selection_runs WHERE delivery_id=?)",
                (envelope["delivery_id"],),
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


def test_selection_attestation_abandon_cli_is_operator_only_and_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge, envelope, store_path, _controller, _selector = _ambiguous_selection(
        tmp_path, selection_error="selection_attestation_missing"
    )
    arguments = _cli_arguments(
        bridge,
        store_path,
        envelope["delivery_id"],
        command="abandon-selection-attestation",
    )

    assert main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "selection_attestation_abandoned"
    assert result["error_code"] == "selection_attestation_missing_abandoned"
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "dismissed"
