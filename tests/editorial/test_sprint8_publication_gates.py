"""ACK-bound publication eligibility checks using only synthetic store state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from test_sprint8_publication_progression import _policy, _store

from waagd_discovery.editorial.delivery import PublicationDelivery
from waagd_discovery.editorial.publication import exportable, prepare_next_publication
from waagd_discovery.editorial.site_import import SiteImportOutcome
from waagd_discovery.editorial.store import EditorialStore


class _Importer:
    def __init__(self) -> None:
        self.calls = 0

    def import_intent(self, _intent_file: Path) -> SiteImportOutcome:
        self.calls += 1
        return SiteImportOutcome("created")


def _path(tmp_path: Path) -> Path:
    return _store(tmp_path.resolve() / "store" / "editorial.sqlite3")


def test_acknowledged_complete_delivery_exposes_only_ready_candidates(tmp_path: Path) -> None:
    path = _path(tmp_path)
    with EditorialStore(path, simulation=False, readonly=True) as store:
        assert len(exportable(store, policy=_policy())) == 2


def test_pending_or_operator_required_delivery_exposes_no_new_intent(tmp_path: Path) -> None:
    path = _path(tmp_path)
    with EditorialStore(path, simulation=False) as store:
        delivery = store.connection.execute("SELECT delivery_id FROM deliveries").fetchone()[0]
        with store.transaction():
            store.connection.execute(
                "UPDATE deliveries SET status='operator_required',ack_status='not_ready' "
                "WHERE delivery_id=?",
                (delivery,),
            )
            store.connection.execute(
                "UPDATE ack_outbox SET status='pending' WHERE delivery_id=?", (delivery,)
            )
    with EditorialStore(path, simulation=False) as store:
        assert exportable(store, policy=_policy()) == []
        assert prepare_next_publication(store, policy=_policy()) is None


def test_missing_decision_or_fidelity_blocks_every_ready_candidate_in_delivery(
    tmp_path: Path,
) -> None:
    path = _path(tmp_path)
    with EditorialStore(path, simulation=False) as store:
        key = store.connection.execute(
            "SELECT candidate_key FROM delivery_candidates ORDER BY candidate_key LIMIT 1"
        ).fetchone()[0]
        with store.transaction():
            store.connection.execute(
                "DELETE FROM decisions WHERE candidate_key=? AND revision=1", (key,)
            )
    with EditorialStore(path, simulation=False) as store:
        assert exportable(store, policy=_policy()) == []

    # A complete delivery still requires a completed Fidelity stage for every
    # READY candidate before it can expose a Site identity.
    fidelity_root = tmp_path / "fidelity"
    fidelity_root.mkdir()
    path = _path(fidelity_root)
    with EditorialStore(path, simulation=False) as store:
        with store.transaction():
            store.connection.execute("DELETE FROM attempts WHERE operation='fidelity'")
        assert exportable(store, policy=_policy()) == []


def test_delivery_cannot_submit_when_shared_export_gate_is_closed(tmp_path: Path) -> None:
    path = _path(tmp_path)
    importer = _Importer()
    with EditorialStore(path, simulation=False) as store:
        delivery = store.connection.execute("SELECT delivery_id FROM deliveries").fetchone()[0]
        with store.transaction():
            store.connection.execute(
                "UPDATE ack_outbox SET status='pending' WHERE delivery_id=?", (delivery,)
            )
    result = PublicationDelivery(
        path,
        tmp_path.resolve() / "export" / "publication-intent.json",
        _policy(),
        importer,
    ).run(datetime.now(UTC))
    assert result == {
        "status": "completed",
        "code": "no_eligible_intents",
        "delivered": 0,
        "held": 0,
    }
    assert importer.calls == 0


def test_preexisting_pending_job_cannot_bypass_a_later_ack_hold(tmp_path: Path) -> None:
    path = _path(tmp_path)
    with EditorialStore(path, simulation=False) as store:
        prepared = prepare_next_publication(store, policy=_policy())
        assert prepared is not None and prepared["status"] == "pending"
        delivery = store.connection.execute("SELECT delivery_id FROM deliveries").fetchone()[0]
        with store.transaction():
            store.connection.execute(
                "UPDATE deliveries SET status='operator_required',ack_status='not_ready' "
                "WHERE delivery_id=?",
                (delivery,),
            )
            store.connection.execute(
                "UPDATE ack_outbox SET status='pending' WHERE delivery_id=?", (delivery,)
            )
        assert prepare_next_publication(store, policy=_policy()) is None
        assert store.publication_job() is not None
