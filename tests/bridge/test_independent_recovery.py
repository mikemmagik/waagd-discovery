"""Black-box restart/crash/ack probes independent of implementation test fixtures."""

import copy
import json
import os
import subprocess
import sys
from datetime import timedelta
from uuid import uuid4

import pytest

from waagd_discovery.bridge import Bridge, BridgeError

from .helpers import NOW, export_document


@pytest.fixture
def layout(tmp_path):
    root = tmp_path.resolve()
    workspace = root / "fake-workspace"
    exports = root / "exports"
    workspace.mkdir(mode=0o700)
    exports.mkdir(mode=0o700)
    export = exports / "candidates.json"
    export.write_text(json.dumps(export_document()))
    return export, workspace, root / "private-state"


def inbox(layout):
    return layout[1] / "waagd-discovery" / "inbox.json"


def test_qa_pending_restart_keeps_identity_even_export_gone(layout):
    first = Bridge(*layout).sync(now=NOW)
    before = inbox(layout).read_bytes()
    initial_stat = inbox(layout).stat()
    layout[0].unlink()
    second = Bridge(*layout).sync(now=NOW + timedelta(minutes=5))
    assert first["pending_delivery_id"] == second["pending_delivery_id"]
    assert inbox(layout).read_bytes() == before
    assert inbox(layout).stat().st_mtime_ns == initial_stat.st_mtime_ns
    assert second["known"] == 1 and second["delivered"] == 1


def test_qa_ack_and_restarted_metadata_change_do_not_redeliver(layout):
    first = Bridge(*layout).sync(now=NOW)
    Bridge(*layout).ack(first["pending_delivery_id"], now=NOW)
    document = export_document()
    document["candidates"][0].update(
        id=777, title="Metadata changed", url="https://EXAMPLE.org:443/article?utm_campaign=x#part"
    )
    layout[0].write_text(json.dumps(document))
    after = Bridge(*layout).sync(now=NOW)
    assert after["processed"] == 1 and after["known"] == 1
    assert after["pending_delivery_id"] is None
    assert json.loads(inbox(layout).read_bytes())["candidates"] == []


def test_qa_unknown_ack_does_not_change_durable_history_or_inbox(layout):
    Bridge(*layout).sync(now=NOW)
    ledger = layout[2] / "ledger.json"
    before = ledger.read_bytes(), inbox(layout).read_bytes()
    with pytest.raises(BridgeError, match="unknown"):
        Bridge(*layout).ack(str(uuid4()), now=NOW)
    assert before == (ledger.read_bytes(), inbox(layout).read_bytes())


def test_qa_expired_pending_blocks_next_batch_until_explicit_ack(layout):
    first = Bridge(*layout).sync(now=NOW)
    document = export_document()
    new = copy.deepcopy(document["candidates"][0])
    new.update(id=2, url="https://example.org/next")
    document["candidates"].append(new)
    document["selection"]["counts"]["eligible"] = 2
    layout[0].write_text(json.dumps(document))
    old = inbox(layout).read_bytes()
    pending = Bridge(*layout).sync(now=NOW + timedelta(hours=3))
    assert pending["status"] == "pending_expired"
    assert pending["known"] == 1
    assert inbox(layout).read_bytes() == old
    acked = Bridge(*layout).ack(first["pending_delivery_id"], now=NOW + timedelta(hours=3))
    assert acked["processed"] == 1 and acked["pending_delivery_id"] is None


def test_qa_corrupt_ledger_never_resets_and_replays(layout):
    Bridge(*layout).sync(now=NOW)
    ledger = layout[2] / "ledger.json"
    ledger.write_bytes(b'{"corrupt": true}')
    before = inbox(layout).read_bytes()
    with pytest.raises(BridgeError, match="corrupt"):
        Bridge(*layout).sync(now=NOW)
    assert ledger.read_bytes() == b'{"corrupt": true}'
    assert inbox(layout).read_bytes() == before


def test_qa_lost_initialized_ledger_never_resets_and_replays(layout):
    first = Bridge(*layout).sync(now=NOW)
    Bridge(*layout).ack(first["pending_delivery_id"], now=NOW)
    ledger = layout[2] / "ledger.json"
    ledger.unlink()
    before = inbox(layout).read_bytes()
    with pytest.raises(BridgeError):
        Bridge(*layout).sync(now=NOW)
    assert not ledger.exists()
    assert inbox(layout).read_bytes() == before


def test_qa_status_missing_state_does_not_create_any_directory(layout):
    before = sorted(str(path) for path in layout[0].parent.parent.rglob("*"))
    assert Bridge(*layout).status(now=NOW)["status"] == "uninitialized"
    after = sorted(str(path) for path in layout[0].parent.parent.rglob("*"))
    assert before == after
    assert not layout[2].exists()
    assert not inbox(layout).parent.exists()


def test_qa_initialized_status_preserves_all_file_bytes_and_metadata(layout):
    Bridge(*layout).sync(now=NOW)
    files = [path for root in (layout[1], layout[2]) for path in root.rglob("*") if path.is_file()]
    before = {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_mode)
        for path in files
    }
    result = Bridge(*layout).status(now=NOW)
    assert result["status"] == "delivered"
    assert result["inbox_matches_ledger"] is True
    assert before == {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_mode)
        for path in files
    }


@pytest.mark.parametrize("placement", ["workspace", "exports", "parent"])
def test_qa_private_history_cannot_overlap_exposed_directories(layout, placement):
    export, workspace, _ = layout
    state = {
        "workspace": workspace / "private",
        "exports": export.parent / "private",
        "parent": workspace.parent,
    }[placement]
    with pytest.raises(BridgeError, match="overlap"):
        Bridge(export, workspace, state)


def test_qa_bridge_cli_does_not_import_database_or_network_clients():
    program = """
import sys
import waagd_discovery.bridge.cli
assert 'sqlite3' not in sys.modules
assert 'waagd_discovery.storage' not in sys.modules
assert 'waagd_discovery.http' not in sys.modules
assert 'httpx' not in sys.modules
assert 'docker' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, env=os.environ.copy()
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("crash_after_publication", [False, True])
def test_qa_prepare_and_publication_interruptions_reuse_exact_delivery(
    layout, monkeypatch, crash_after_publication
):
    bridge = Bridge(*layout)
    if crash_after_publication:
        original_save = bridge._save
        saves = 0

        def interrupted_save(directory, ledger):
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("simulated power loss after inbox replace")
            original_save(directory, ledger)

        monkeypatch.setattr(bridge, "_save", interrupted_save)
    else:

        def interrupted_publish(directory, snapshot):
            raise OSError("simulated power loss after durable PREPARE")

        monkeypatch.setattr(bridge, "_publish", interrupted_publish)
    with pytest.raises(OSError):
        bridge.sync(now=NOW)
    prepared = json.loads((layout[2] / "ledger.json").read_text())
    assert prepared["pending"]["phase"] == "prepared"
    expected = prepared["pending"]["envelope"]
    layout[0].unlink()  # Recovery cannot depend on an input still being present.
    result = Bridge(*layout).sync(now=NOW + timedelta(minutes=1))
    assert result["pending_delivery_id"] == expected["delivery_id"]
    assert result["delivered"] == 1 and result["known"] == 1
    assert json.loads(inbox(layout).read_text()) == expected


def test_qa_interrupted_ack_is_durable_before_inbox_clearing(layout, monkeypatch):
    first = Bridge(*layout).sync(now=NOW)
    bridge = Bridge(*layout)

    def interrupted_publish(directory, snapshot):
        raise OSError("simulated interruption before clearing inbox")

    monkeypatch.setattr(bridge, "_publish", interrupted_publish)
    with pytest.raises(OSError):
        bridge.ack(first["pending_delivery_id"], now=NOW)
    ledger = json.loads((layout[2] / "ledger.json").read_text())
    assert ledger["pending"] is None
    assert all(record["status"] == "processed" for record in ledger["records"].values())
    assert json.loads(inbox(layout).read_text())["delivery_id"] == first["pending_delivery_id"]
    layout[0].unlink()
    with pytest.raises(OSError):
        Bridge(*layout).sync(now=NOW)
    assert json.loads(inbox(layout).read_text())["candidates"] == []
    assert Bridge(*layout).status(now=NOW)["processed"] == 1
    assert Bridge(*layout).ack(first["pending_delivery_id"], now=NOW)["processed"] == 1


def test_qa_status_retains_history_when_workspace_inbox_directory_is_missing(layout):
    first = Bridge(*layout).sync(now=NOW)
    inbox(layout).unlink()
    inbox(layout).parent.rmdir()
    result = Bridge(*layout).status(now=NOW)
    assert result["pending_delivery_id"] == first["pending_delivery_id"]
    assert result["known"] == 1 and result["delivered"] == 1
    assert result["inbox_matches_ledger"] is False
    assert not inbox(layout).parent.exists()


def test_qa_concurrent_processes_share_one_delivery_and_history(layout):
    program = """
import json, sys
from pathlib import Path
from datetime import datetime
from waagd_discovery.bridge import Bridge, BridgeError
try:
    result = Bridge(*(Path(value) for value in sys.argv[1:])).sync(
        now=datetime.fromisoformat('2026-10-18T12:00:00+00:00'))
except BridgeError as error:
    if str(error) != 'bridge_busy':
        raise
    result = {'status': 'bridge_busy'}
print(json.dumps(result))
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program, *(str(path) for path in layout)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    successful = [result for result in results if result["status"] != "bridge_busy"]
    assert successful
    assert len({result["pending_delivery_id"] for result in successful}) == 1
    assert all(result["known"] == 1 and result["delivered"] == 1 for result in successful)
    assert (
        Bridge(*layout).sync(now=NOW)["pending_delivery_id"] == successful[0]["pending_delivery_id"]
    )
    assert len(json.loads(inbox(layout).read_text())["candidates"]) == 1


def test_qa_expired_dismissal_preserves_history_without_claiming_processing(layout):
    first = Bridge(*layout).sync(now=NOW)
    late = NOW + timedelta(hours=3)
    result = Bridge(*layout).dismiss(first["pending_delivery_id"], now=late)
    assert result["dismissed"] == 1 and result["processed"] == 0 and result["known"] == 1
    assert result["pending_delivery_id"] is None
    ledger = layout[2] / "ledger.json"
    before = ledger.read_bytes()
    repeated = Bridge(*layout).dismiss(first["pending_delivery_id"], now=late)
    assert repeated["dismissed"] == 1 and ledger.read_bytes() == before
    with pytest.raises(BridgeError, match="differently"):
        Bridge(*layout).ack(first["pending_delivery_id"], now=late)
    document = export_document()
    document["generated_at"] = late.isoformat()
    next_candidate = copy.deepcopy(document["candidates"][0])
    next_candidate.update(id=2, url="https://example.org/next")
    document["candidates"].append(next_candidate)
    document["selection"]["counts"]["eligible"] = 2
    layout[0].write_text(json.dumps(document))
    following = Bridge(*layout).sync(now=late)
    assert following["known"] == 2 and following["dismissed"] == 1 and following["processed"] == 0
    assert following["delivered"] == 1
    assert [item["url"] for item in json.loads(inbox(layout).read_text())["candidates"]] == [
        "https://example.org/next"
    ]


@pytest.mark.parametrize("operation", ["status", "sync"])
def test_qa_busy_kernel_lock_is_bounded_and_never_mutates_state(layout, operation):
    import fcntl

    Bridge(*layout).sync(now=NOW)
    ledger = layout[2] / "ledger.json"
    before = ledger.read_bytes(), inbox(layout).read_bytes()
    program = """
import json, sys
from pathlib import Path
from datetime import datetime
from waagd_discovery.bridge import Bridge, BridgeError
try:
    bridge = Bridge(*(Path(value) for value in sys.argv[2:]))
    getattr(bridge, sys.argv[1])(now=datetime.fromisoformat('2026-10-18T12:00:00+00:00'))
except BridgeError as error:
    print(json.dumps({'error': str(error)}))
    sys.exit(7)
"""
    with (layout[2] / "bridge.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = subprocess.run(
            [sys.executable, "-c", program, operation, *(str(path) for path in layout)],
            capture_output=True,
            text=True,
            timeout=3,
        )
    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout)["error"] == "bridge_busy"
    assert before == (ledger.read_bytes(), inbox(layout).read_bytes())
