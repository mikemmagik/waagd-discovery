"""Publication export remains offline and never holds a publisher credential."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_independent_contracts import NOW, candidate, decision, inbox, verification

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError, make_request
from waagd_discovery.editorial.publication import export_file, exportable
from waagd_discovery.editorial.store import EditorialStore


def _store(path: Path, *, status: str = "VERIFIED", ready: bool = True) -> Path:
    value = inbox()
    item = candidate()
    result = verification(status)
    # A ready decision is valid only with verified material.  Persisting a
    # syntactically valid but semantically impossible result is tested below as
    # a corrupt-state failure, never as a normal non-export case.
    output = decision("READY_FOR_REVIEW" if ready else "NEEDS_VERIFICATION")
    output["risks"] = []
    request = make_request(item, result, simulation=False)
    with EditorialStore(path, simulation=False) as store:
        store.register(value, NOW.isoformat())
        store.save_verification(item["candidate_key"], 1, result, NOW.isoformat())
        store.save_decision(
            item["candidate_key"],
            1,
            request,
            output,
            value["delivery_id"],
            NOW.isoformat(),
        )
    return path


def test_verified_ready_decision_exports_exact_idempotent_intent(tmp_path: Path) -> None:
    path = _store(tmp_path.resolve() / "store" / "editorial.sqlite3")
    with EditorialStore(path, simulation=False, readonly=True) as store:
        intents = exportable(store)
    assert len(intents) == 1
    intent = intents[0]
    assert intent["identity"] == f"auto:{candidate()['candidate_key']}:1"
    assert intent["mode"] == "verified_auto"
    assert intent["payload"]["type"] == "article"
    assert intent["payload"]["origin"] == "auto"
    assert (
        intent["payload"]["source_item_id"]
        == "waagd:" + hashlib.sha256(intent["identity"].encode()).hexdigest()
    )
    encoded = json.dumps(
        intent["payload"],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert intent["content_digest"] == hashlib.sha256(encoded).hexdigest()
    assert {item["url"] for item in intent["payload"]["references"]} == {
        candidate()["url"],
        "https://independent.example.test/attestation",
    }


@pytest.mark.parametrize(
    "status,ready", [("PARTIAL", False), ("FAILED", False), ("VERIFIED", False)]
)
def test_unverified_or_nonready_result_is_not_exported(
    tmp_path: Path, status: str, ready: bool
) -> None:
    path = _store(tmp_path.resolve() / "store" / "editorial.sqlite3", status=status, ready=ready)
    with EditorialStore(path, simulation=False, readonly=True) as store:
        assert exportable(store) == []


def test_risk_or_unknown_claim_blocks_auto_export(tmp_path: Path) -> None:
    path = _store(tmp_path.resolve() / "store" / "editorial.sqlite3")
    with EditorialStore(path, simulation=False) as store:
        raw = store.connection.execute("SELECT result FROM decisions").fetchone()[0]
        value = io.decode(raw.encode())
        value["risks"] = ["Editorial concern requires review."]
        with store.transaction():
            store.connection.execute("UPDATE decisions SET result=?", (io.encode(value).decode(),))
    with EditorialStore(path, simulation=False, readonly=True) as store:
        assert exportable(store) == []


def test_export_is_private_atomic_and_rejects_simulation_store(tmp_path: Path) -> None:
    path = _store(tmp_path.resolve() / "store" / "editorial.sqlite3")
    output = tmp_path.resolve() / "publisher" / "publication-intents.json"
    assert export_file(path, output) == {
        "status": "exported",
        "count": 1,
        "path": "publication-intents.json",
    }
    assert output.stat().st_mode & 0o777 == 0o600
    assert io.decode(output.read_bytes())["intents"][0]["mode"] == "verified_auto"
    simulation = tmp_path.resolve() / "simulation" / "editorial.sqlite3"
    with EditorialStore(simulation, simulation=True):
        pass
    with pytest.raises(EditorialError, match="publication_simulation_forbidden"):
        export_file(simulation, output)


def test_corrupt_persisted_publication_data_fails_closed(tmp_path: Path) -> None:
    path = _store(tmp_path.resolve() / "store" / "editorial.sqlite3")
    with EditorialStore(path, simulation=False) as store:
        raw = store.connection.execute("SELECT result FROM verifications").fetchone()[0]
        value = copy.deepcopy(io.decode(raw.encode()))
        value["evidence"][0]["url"] = "javascript:alert(1)"
        with store.transaction():
            store.connection.execute(
                "UPDATE verifications SET result=?", (io.encode(value).decode(),)
            )
    with EditorialStore(path, simulation=False, readonly=True) as store:
        with pytest.raises(EditorialError, match="publication_store_invalid"):
            exportable(store)
