"""Durable, offline progression checks for automatic Site submissions."""

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_independent_contracts import NOW, candidate, decision, inbox, verification

from waagd_discovery.editorial.contracts import EditorialError, make_request
from waagd_discovery.editorial.publication import prepare_next_publication
from waagd_discovery.editorial.publication_policy import PublicationPolicy
from waagd_discovery.editorial.store import EditorialStore


def _policy() -> PublicationPolicy:
    return PublicationPolicy(automatic_enabled=True, maximum_age=timedelta(days=31))


def _item(suffix: str) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    value = copy.deepcopy(candidate())
    url = f"https://example.test/fictional-council-{suffix}"
    key = hashlib.sha256(url.encode()).hexdigest()
    value["url"] = url
    value["candidate_key"] = key
    result = copy.deepcopy(verification("VERIFIED"))
    result["candidate_key"] = key
    result["canonical_url"] = url
    result["evidence"][0]["url"] = url
    output = copy.deepcopy(decision("READY_FOR_REVIEW"))
    output["candidate_key"] = key
    output["source"] = url
    output["checked_sources"] = [url]
    output["risks"] = []
    return value, result, output


def _store(path: Path) -> Path:
    envelope = inbox()
    first = _item("first")
    second = _item("second")
    envelope["candidates"] = [first[0], second[0]]
    with EditorialStore(path, simulation=False) as store:
        store.register(envelope, NOW.isoformat())
        for item, result, output in (first, second):
            request = make_request(item, result, simulation=False)
            store.save_verification(item["candidate_key"], 1, result, NOW.isoformat())
            store.save_decision(
                item["candidate_key"], 1, request, output, envelope["delivery_id"], NOW.isoformat()
            )
            fidelity = store.start_attempt(
                envelope["delivery_id"], item["candidate_key"], "fidelity", NOW.isoformat()
            )
            store.finish_attempt(fidelity, NOW.isoformat())
        store.acknowledged(envelope["delivery_id"], NOW.isoformat())
    return path


def test_two_deliveries_advance_only_after_site_receipt(tmp_path: Path) -> None:
    path = _store(tmp_path.resolve() / "store" / "editorial.sqlite3")
    clock = datetime.now(UTC).isoformat()
    with EditorialStore(path, simulation=False) as store:
        first = prepare_next_publication(store, policy=_policy(), clock=clock)
        assert first is not None and first["status"] == "pending"
        first_id = first["intent"]["intent_id"]
        assert prepare_next_publication(store, policy=_policy(), clock=clock) == first
        store.begin_publication_submission(first_id, clock)
        store.record_publication_outcome(first_id, "created", clock)
        second = prepare_next_publication(store, policy=_policy(), clock=clock)
        assert second is not None and second["status"] == "pending"
        assert second["intent"]["intent_id"] != first_id
        store.begin_publication_submission(second["intent"]["intent_id"], clock)
        store.record_publication_outcome(second["intent"]["intent_id"], "duplicate", clock)
        assert prepare_next_publication(store, policy=_policy(), clock=clock) is None


def test_interrupted_submission_requires_receipt_reconciliation(tmp_path: Path) -> None:
    path = _store(tmp_path.resolve() / "store" / "editorial.sqlite3")
    clock = datetime.now(UTC).isoformat()
    with EditorialStore(path, simulation=False) as store:
        prepared = prepare_next_publication(store, policy=_policy(), clock=clock)
        assert prepared is not None
        intent_id = prepared["intent"]["intent_id"]
        store.begin_publication_submission(intent_id, clock)
    with EditorialStore(path, simulation=False) as store:
        assert store.recover_publication_submissions(clock) == 1
        recovered = prepare_next_publication(store, policy=_policy(), clock=clock)
        assert recovered is not None
        assert recovered["status"] == "uncertain"
        assert recovered["intent"] == prepared["intent"]
        with pytest.raises(EditorialError, match="publication_submission_not_ready"):
            store.begin_publication_submission(intent_id, clock)
        store.record_publication_outcome(intent_id, "duplicate", clock)
        assert prepare_next_publication(store, policy=_policy(), clock=clock) is not None


def test_known_failure_never_changes_identity_and_does_not_block_next_story(tmp_path: Path) -> None:
    path = _store(tmp_path.resolve() / "store" / "editorial.sqlite3")
    clock = datetime.now(UTC).isoformat()
    with EditorialStore(path, simulation=False) as store:
        first = prepare_next_publication(store, policy=_policy(), clock=clock)
        assert first is not None
        intent_id = first["intent"]["intent_id"]
        store.begin_publication_submission(intent_id, clock)
        store.record_publication_outcome(intent_id, "failed", clock)
        second = prepare_next_publication(store, policy=_policy(), clock=clock)
        assert second is not None
        assert second["intent"]["intent_id"] != intent_id
        store.retry_failed_publication(intent_id, clock)
        # Retrying the failed item preserves its original identity and bytes.
        store.begin_publication_submission(intent_id, clock)
        store.record_publication_outcome(intent_id, "held", clock)
