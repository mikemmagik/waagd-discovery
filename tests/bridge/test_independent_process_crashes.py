"""Real SIGKILL recovery at durable transaction boundaries, using fake roots only."""

import json
import signal
import subprocess
import sys
import time

import pytest

from waagd_discovery.bridge import Bridge

from .helpers import NOW, export_document

PROGRAM = r"""
import os, signal, sys
from datetime import datetime
from pathlib import Path
from waagd_discovery.bridge import Bridge
export, workspace, state, ready = (Path(value) for value in sys.argv[2:6])
bridge = Bridge(export, workspace, state)
now = datetime.fromisoformat('2026-10-18T12:00:00+00:00')
checkpoint = sys.argv[1]
def pause_after_durability():
    with ready.open('w') as stream:
        stream.write(checkpoint)
        stream.flush()
        os.fsync(stream.fileno())
    while True:
        signal.pause()
if checkpoint == 'published_before_delivered':
    original_save = bridge._save
    saves = 0
    def stopped_save(directory, ledger):
        global saves
        saves += 1
        if saves == 2:
            pause_after_durability()
        original_save(directory, ledger)
    bridge._save = stopped_save
else:
    def stopped_publish(directory, snapshot):
        pause_after_durability()
    bridge._publish = stopped_publish
if checkpoint == 'ack_before_empty_publish':
    bridge.ack(sys.argv[6], now=now)
else:
    bridge.sync(now=now)
"""


def kill_at_checkpoint(paths, checkpoint, ready, delivery_id=None):
    arguments = [
        sys.executable,
        "-c",
        PROGRAM,
        checkpoint,
        *(str(path) for path in paths),
        str(ready),
    ]
    if delivery_id is not None:
        arguments.append(delivery_id)
    child = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if child.poll() is not None:
                stdout, stderr = child.communicate()
                pytest.fail(f"Child exited before checkpoint: {stdout} {stderr}")
            if time.monotonic() > deadline:
                pytest.fail("Child did not reach durable checkpoint")
            time.sleep(0.01)
        assert ready.read_text() == checkpoint
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


@pytest.fixture
def fake_roots(tmp_path):
    root = tmp_path.resolve()
    exports = root / "export"
    workspace = root / "fake-workspace"
    exports.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    source = exports / "candidates.json"
    source.write_text(json.dumps(export_document()))
    return source, workspace, root / "private-state"


@pytest.mark.parametrize("checkpoint", ["prepared_before_publish", "published_before_delivered"])
def test_qa_sigkill_reuses_prepared_payload_and_releases_kernel_lock(fake_roots, checkpoint):
    source, workspace, state = fake_roots
    ready = state.parent / "ready"
    kill_at_checkpoint(fake_roots, checkpoint, ready)
    ledger = json.loads((state / "ledger.json").read_text())
    assert ledger["pending"]["phase"] == "prepared"
    expected = ledger["snapshot"]
    inbox = workspace / "waagd-discovery" / "inbox.json"
    if checkpoint == "prepared_before_publish":
        assert not inbox.exists()
    else:
        assert json.loads(inbox.read_text()) == expected
    source.unlink()
    restarted = Bridge(*fake_roots).sync(now=NOW)
    assert restarted["pending_delivery_id"] == expected["delivery_id"]
    assert restarted["known"] == 1 and restarted["delivered"] == 1
    assert json.loads(inbox.read_text()) == expected
    repeated = Bridge(*fake_roots).sync(now=NOW)
    assert repeated["pending_delivery_id"] == expected["delivery_id"]
    assert json.loads((state / "ledger.json").read_text())["pending"]["phase"] == "delivered"


def test_qa_sigkill_after_durable_ack_never_redelivers_processed_candidates(fake_roots):
    _, workspace, state = fake_roots
    first = Bridge(*fake_roots).sync(now=NOW)
    ready = state.parent / "ready"
    kill_at_checkpoint(fake_roots, "ack_before_empty_publish", ready, first["pending_delivery_id"])
    ledger = json.loads((state / "ledger.json").read_text())
    assert ledger["pending"] is None and ledger["snapshot"]["candidates"] == []
    assert all(record["status"] == "processed" for record in ledger["records"].values())
    inbox = workspace / "waagd-discovery" / "inbox.json"
    assert json.loads(inbox.read_text())["delivery_id"] == first["pending_delivery_id"]
    restarted = Bridge(*fake_roots).sync(now=NOW)
    assert restarted["processed"] == 1 and restarted["known"] == 1
    assert restarted["pending_delivery_id"] is None
    assert json.loads(inbox.read_text())["candidates"] == []
    assert Bridge(*fake_roots).ack(first["pending_delivery_id"], now=NOW)["processed"] == 1
