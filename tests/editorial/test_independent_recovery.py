"""Genuine SIGKILL recovery and durable-result-before-ACK adversarial checks."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_independent_bridge import read_inbox, seed_bridge
from test_independent_contracts import NOW, decision, verification

from waagd_discovery.bridge import Bridge
from waagd_discovery.editorial.contracts import PROTECTED_DELIVERIES, EditorialError
from waagd_discovery.editorial.controller import Controller


class FixturePort:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.ack_calls = 0

    def inspect_delivery(self, identifier: str) -> dict[str, Any]:
        return self.bridge.inspect_delivery(identifier)

    def ack_if_current(self, identifier: str, expected_digest: str) -> dict[str, Any]:
        self.ack_calls += 1
        return self.bridge.ack_if_current(identifier, expected_digest, NOW)


class FixtureVerifier:
    def verify(self, candidate: dict[str, Any], *, policy_id: str) -> dict[str, Any]:
        return verification()


class FixtureEditor:
    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        return decision()


def controller(
    bridge: Bridge, store: Path, *, allow_ack: bool = True
) -> tuple[Controller, FixturePort]:
    port = FixturePort(bridge)
    return Controller(
        bridge.inbox_dir / "inbox.json",
        store,
        port,
        FixtureVerifier(),
        FixtureEditor(),
        simulation=False,
        allow_ack=allow_ack,
        protected_roots=(bridge.state_dir, bridge.export.parent),
    ), port


_CHILD = r"""
import json
import os
import sys
import time
from pathlib import Path
from waagd_discovery.bridge import Bridge
from waagd_discovery.editorial.controller import Controller
from waagd_discovery.editorial.store import EditorialStore
from test_independent_contracts import NOW, decision, verification
root = Path(sys.argv[1])
stage = sys.argv[2]
bridge = Bridge(
    root / "exports/candidates.json", root / "fake-workspace",
    root / "private-bridge", schema_version=2,
)

def append(name):
    descriptor = os.open(root / name, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, b"call\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def pause():
    append("boundary")
    while True:
        time.sleep(1)

class Verifier:
    def verify(self, candidate, *, policy_id):
        append("verifier-calls")
        if stage == "verification-started":
            pause()
        return verification()

class Editor:
    def evaluate(self, request):
        append("editor-calls")
        if stage == "editorial-started":
            pause()
        return decision()

class Port:
    def inspect_delivery(self, identifier):
        return bridge.inspect_delivery(identifier)
    def ack_if_current(self, identifier, expected_digest):
        result = bridge.ack_if_current(identifier, expected_digest, NOW)
        if stage == "ack-committed":
            pause()
        return result

if stage in {"verification-saved", "decision-saved"}:
    method = "save_verification" if stage == "verification-saved" else "save_decision"
    original = getattr(EditorialStore, method)
    def commit_then_pause(self, *args, **kwargs):
        original(self, *args, **kwargs)
        pause()
    setattr(EditorialStore, method, commit_then_pause)
controller = Controller(
    bridge.inbox_dir / "inbox.json", root / "editorial/store.sqlite3", Port(), Verifier(), Editor(),
    simulation=False, allow_ack=True, protected_roots=(bridge.state_dir, bridge.export.parent),
)
print(json.dumps(controller.process(NOW)), flush=True)
"""


def child(root: Path, stage: str) -> subprocess.Popen[bytes]:
    source = Path(__file__).resolve().parents[2] / "src"
    helper = Path(__file__).resolve().parent
    environment = {"PATH": os.defpath, "PYTHONPATH": os.pathsep.join((str(source), str(helper)))}
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(root), stage],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        cwd=root,
    )


@pytest.mark.parametrize(
    "stage,verifier_calls,editor_calls",
    [
        ("verification-started", 2, 1),
        ("verification-saved", 1, 1),
        ("editorial-started", 1, 2),
        ("decision-saved", 1, 1),
        ("ack-committed", 1, 1),
    ],
)
def test_sigkill_recovers_durable_results_before_ack(
    tmp_path: Path, stage: str, verifier_calls: int, editor_calls: int
) -> None:
    bridge = seed_bridge(tmp_path)
    delivery = read_inbox(bridge)["delivery_id"]
    root = tmp_path.resolve()
    process = child(root, stage)
    try:
        deadline = time.monotonic() + 10
        while not (root / "boundary").exists():
            if process.poll() is not None:
                out, err = process.communicate()
                pytest.fail(f"child exited before durable boundary: {out!r} {err!r}")
            if time.monotonic() > deadline:
                pytest.fail("durable-boundary child timeout")
            time.sleep(0.01)
        process.kill()
        process.wait(timeout=5)
        assert process.returncode == -9
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    resumed = child(root, "resume")
    out, err = resumed.communicate(timeout=10)
    assert resumed.returncode == 0, (out, err)
    status = json.loads(out)
    assert status["status"] in {"acknowledged", "inbox_empty"}
    assert bridge.inspect_delivery(delivery)["status"] == "acknowledged"
    assert (root / "verifier-calls").read_bytes().splitlines() == [b"call"] * verifier_calls
    assert (root / "editor-calls").read_bytes().splitlines() == [b"call"] * editor_calls
    with closing(sqlite3.connect(root / "editorial/store.sqlite3")) as connection, connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM ack_outbox").fetchone()[0] == "acknowledged"
        assert (
            connection.execute("SELECT COUNT(*) FROM attempts WHERE status='started'").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE status='interrupted'"
            ).fetchone()[0]
            >= 1
        )


@pytest.mark.parametrize("corruption", ["missing", "malformed", "wrong-candidate"])
def test_outbox_cannot_ack_missing_or_invalid_saved_results(
    tmp_path: Path, corruption: str
) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    store = tmp_path.resolve() / "editorial/store.sqlite3"
    initial, port = controller(bridge, store, allow_ack=False)
    assert initial.process(NOW)["status"] == "results_saved"
    assert port.ack_calls == 0
    with closing(sqlite3.connect(store)) as connection, connection:
        if corruption == "missing":
            connection.execute("DELETE FROM decisions")
        elif corruption == "malformed":
            connection.execute("UPDATE decisions SET result='{}'")
        else:
            row = connection.execute("SELECT request,result FROM decisions").fetchone()
            request, result = json.loads(row[0]), json.loads(row[1])
            request["candidate"]["url"] = "https://different.example.test/other-candidate"
            import hashlib

            key = hashlib.sha256(request["candidate"]["url"].encode()).hexdigest()
            request["candidate"]["candidate_key"] = key
            request["verification"]["candidate_key"] = key
            result["candidate_key"] = key
            result["source"] = request["candidate"]["url"]
            result["checked_sources"] = []
            connection.execute(
                "UPDATE decisions SET request=?,result=?", (json.dumps(request), json.dumps(result))
            )
    resumed, port = controller(bridge, store)
    try:
        resumed.process(NOW)
    except EditorialError:
        pass
    assert port.ack_calls == 0
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"


def test_protected_id_is_rejected_before_bridge_store_verifier_or_editor(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    envelope["delivery_id"] = next(iter(PROTECTED_DELIVERIES))
    path = bridge.inbox_dir / "inbox.json"
    path.write_text(json.dumps(envelope))
    before = (bridge.state_dir / "ledger.json").read_bytes(), path.read_bytes()
    store = tmp_path.resolve() / "editorial/store.sqlite3"
    instance, port = controller(bridge, store)
    with pytest.raises(EditorialError, match="protected_delivery"):
        instance.process(NOW)
    assert not store.parent.exists()
    assert port.ack_calls == 0
    assert ((bridge.state_dir / "ledger.json").read_bytes(), path.read_bytes()) == before


def test_expired_pending_batch_is_operator_required_without_ack_or_dismiss(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    before = (bridge.state_dir / "ledger.json").read_bytes()
    store = tmp_path.resolve() / "editorial/store.sqlite3"
    instance, port = controller(bridge, store)
    result = instance.process(NOW + timedelta(hours=3))
    assert result["status"] == "operator_required"
    assert result["error_code"] == "delivery_expired"
    assert port.ack_calls == 0
    assert not store.parent.exists()
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"
    assert (bridge.state_dir / "ledger.json").read_bytes() == before


def test_acknowledged_delivery_without_editorial_history_cannot_be_regenerated(
    tmp_path: Path,
) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    from waagd_discovery.editorial.contracts import digest

    bridge.ack_if_current(envelope["delivery_id"], digest(envelope), NOW)
    # Simulate stale publication after ACK while an entirely new Editorial Store
    # has no proof of the original decisions. The private receipt is authoritative.
    (bridge.inbox_dir / "inbox.json").write_text(json.dumps(envelope))
    store = tmp_path.resolve() / "new-editorial/store.sqlite3"

    class NeverGenerate:
        def verify(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            pytest.fail("re-fetch after acknowledged delivery without editorial history")

        def evaluate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            pytest.fail("re-generate after acknowledged delivery without editorial history")

    port = FixturePort(bridge)
    instance = Controller(
        bridge.inbox_dir / "inbox.json",
        store,
        port,
        NeverGenerate(),
        NeverGenerate(),
        simulation=False,
        allow_ack=True,
        protected_roots=(bridge.state_dir, bridge.export.parent),
    )
    try:
        outcome = instance.process(NOW)
    except EditorialError:
        pass
    else:
        assert outcome["status"] == "operator_required"
    assert port.ack_calls == 0
    assert not store.exists()


def test_atomic_bridge_expiry_rejection_immediately_requires_operator(tmp_path: Path) -> None:
    bridge = seed_bridge(tmp_path)
    envelope = read_inbox(bridge)
    store = tmp_path.resolve() / "editorial/store.sqlite3"

    class ExpiringPort(FixturePort):
        def ack_if_current(self, identifier: str, expected_digest: str) -> dict[str, Any]:
            self.ack_calls += 1
            # Receipt inspection saw a live delivery; expiry crossed while entering
            # the Bridge's locked commit, which independently rechecks real time.
            return self.bridge.ack_if_current(identifier, expected_digest, NOW + timedelta(hours=3))

    port = ExpiringPort(bridge)
    instance = Controller(
        bridge.inbox_dir / "inbox.json",
        store,
        port,
        FixtureVerifier(),
        FixtureEditor(),
        simulation=False,
        allow_ack=True,
        protected_roots=(bridge.state_dir, bridge.export.parent),
    )
    result = instance.process(NOW)
    assert result["status"] == "operator_required"
    assert result["completed"] == result["total"] == 1
    assert port.ack_calls == 1
    assert bridge.inspect_delivery(envelope["delivery_id"])["status"] == "pending"
