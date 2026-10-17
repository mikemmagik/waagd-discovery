"""Independent Sprint 3 adversarial checks; all databases and locks are disposable."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waagd_discovery.cli import main
from waagd_discovery.models import DiscoveryError
from waagd_discovery.operations import snapshot
from waagd_discovery.storage import Repository
from waagd_discovery.storage.migrations import MIGRATIONS

REFERENCE = datetime(2026, 10, 18, 8, tzinfo=UTC)


@contextmanager
def held_kernel_lock(db, suffix):
    """Hold the real kernel lock in another process, even with no scheduler state."""
    program = """
import fcntl
import sys
import time
with open(sys.argv[1], "a+b") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("LOCKED", flush=True)
    time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", program, str(db) + suffix],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def legacy_three(path):
    """A genuine old schema, constructed without invoking current Repository."""
    with closing(sqlite3.connect(path)) as connection, connection:
        for statements in MIGRATIONS[:3]:
            for statement in statements:
                connection.execute(statement)
        connection.execute("PRAGMA user_version=3")
        connection.execute(
            "INSERT INTO sources VALUES "
            "('qa', 'QA', 'https://example.org/rss', 'rss', 1, NULL, NULL)"
        )
        connection.execute(
            """INSERT INTO runs VALUES ('legacy-running', ?, NULL, 'running')""",
            (REFERENCE.isoformat(),),
        )


@pytest.mark.parametrize("command", ["status", "health"])
def test_qa_diagnostics_missing_database_are_read_only(tmp_path, capsys, command):
    db = tmp_path / "absent" / "state.sqlite3"
    assert main(["--db", str(db), command]) == 1
    assert not db.parent.exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("command", ["status", "health"])
def test_qa_diagnostics_refuse_future_schema_without_changes(tmp_path, capsys, command):
    db = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("PRAGMA user_version=999")
        connection.execute("CREATE TABLE future(value TEXT)")
        connection.execute("INSERT INTO future VALUES ('keep')")
    before = db.read_bytes()
    assert main(["--db", str(db), command]) == 1
    assert db.read_bytes() == before
    assert not Path(str(db) + ".lock").exists()
    assert not Path(str(db) + ".scheduler.lock").exists()
    assert "Traceback" not in capsys.readouterr().err


def test_qa_schema3_snapshot_does_not_recover_or_upgrade_original(tmp_path):
    db = tmp_path / "schema3.sqlite3"
    legacy_three(db)
    before = db.read_bytes()
    with snapshot(db) as repository:
        assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert repository.connection.execute("SELECT status FROM runs").fetchone()[0] == "running"
        assert (
            repository.connection.execute("SELECT count(*) FROM run_interruptions").fetchone()[0]
            == 0
        )
        repository.verify()
    assert db.read_bytes() == before
    assert not Path(str(db) + ".lock").exists()
    assert not Path(str(db) + ".scheduler.lock").exists()


def test_qa_schema4_upgrade_retains_legacy_rows_and_has_immutable_recovery_audit(tmp_path):
    db = tmp_path / "schema3.sqlite3"
    legacy_three(db)
    with Repository(db) as repository:
        assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert tuple(repository.connection.execute("SELECT * FROM runs").fetchone()) == (
            "legacy-running",
            REFERENCE.isoformat(),
            None,
            "running",
        )
        assert repository.get_source("qa").name == "QA"
        repository.connection.execute(
            "INSERT INTO run_interruptions VALUES ('legacy-running', ?, 'qa_orphan')",
            (REFERENCE.isoformat(),),
        )
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute("DELETE FROM run_interruptions")
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute("UPDATE run_interruptions SET reason='changed'")
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute("INSERT INTO service_state VALUES(2, '{}')")
        repository.verify()


def configuration(tmp_path):
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "sources:\n"
        "  - id: qa\n    name: QA\n    url: https://example.org/rss\n"
        "  - id: later\n    name: Later\n    url: https://example.net/rss\n"
    )
    policy = tmp_path / "filter.yaml"
    policy.write_text("filter: {}")
    return registry, policy


class NotModifiedClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def fetch(self, source):
        from waagd_discovery.models import FetchResult

        return FetchResult(304)


def test_qa_real_operation_lock_prevents_recovering_active_manual_run(tmp_path, monkeypatch):
    from waagd_discovery.collection import run_once
    from waagd_discovery.diagnostics import service_status

    db = tmp_path / "state.sqlite3"
    registry, policy = configuration(tmp_path)
    with Repository(db) as repository:
        orphan = repository.start_run()
    monkeypatch.setattr("waagd_discovery.collection.HttpClient", NotModifiedClient)
    with held_kernel_lock(db, ".lock"):
        status = service_status(db)
        assert status["operation_state"] == "active"
        assert status["running_runs"][0]["id"] == orphan
        with pytest.raises(DiscoveryError, match="database_busy"):
            run_once(db, registry, policy)
        with snapshot(db) as repository:
            assert (
                repository.connection.execute("SELECT status FROM runs").fetchone()[0] == "running"
            )
            assert (
                repository.connection.execute("SELECT count(*) FROM run_interruptions").fetchone()[
                    0
                ]
                == 0
            )
    status = service_status(db)
    assert status["operation_state"] == "orphaned"
    result = run_once(db, registry, policy)
    assert result["recovered_runs"] == [orphan]
    assert result["status"] == "success"
    second = run_once(db, registry, policy)
    assert second["recovered_runs"] == []
    with snapshot(db) as repository:
        assert (
            repository.connection.execute("SELECT count(*) FROM run_interruptions").fetchone()[0]
            == 1
        )
        assert (
            repository.connection.execute(
                "SELECT status FROM runs WHERE id=?", (orphan,)
            ).fetchone()[0]
            == "interrupted"
        )
        repository.verify()


def test_qa_symlink_manual_database_uses_same_operation_lock(tmp_path, monkeypatch):
    from waagd_discovery.collection import run_once

    db = tmp_path / "state.sqlite3"
    registry, policy = configuration(tmp_path)
    with Repository(db):
        pass
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(db)
    monkeypatch.setattr("waagd_discovery.collection.HttpClient", NotModifiedClient)
    with held_kernel_lock(db, ".lock"):
        with pytest.raises(DiscoveryError, match="database_busy"):
            run_once(alias, registry, policy)
    assert not Path(str(alias) + ".lock").exists()


def test_qa_live_service_healthy_despite_feed429_and_persisted_stopped_state(tmp_path, capsys):
    from waagd_discovery.diagnostics import service_status
    from waagd_discovery.models import Source

    db = tmp_path / "state.sqlite3"
    source = Source("qa", "QA", "https://example.org/rss")
    with Repository(db) as repository:
        repository.sync_sources([source])
        for _ in range(3):
            run = repository.start_run()
            repository.fail_source(run, source, "http_status_429")
            repository.finish_run(run, "failed")
        repository.connection.execute(
            "INSERT INTO service_state VALUES(1, ?)", ('{"status":"stopped"}',)
        )
    before = db.read_bytes()
    with held_kernel_lock(db, ".scheduler.lock"):
        status = service_status(db)
        assert status["healthy"] is True
        assert status["scheduler_alive"] is True
        assert status["scheduler"]["status"] == "stopped"
        assert status["source_problems"][0]["persistent_problem"] is True
        assert status["source_problems"][0]["consecutive_failures"] == 3
        assert status["source_problems"][0]["last_error"] == "http_status_429"
        assert main(["--db", str(db), "health"]) == 0
    assert main(["--db", str(db), "health"]) == 1
    assert db.read_bytes() == before
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("command,exit_code", [("status", 0), ("health", 1)])
def test_qa_schema3_diagnostics_report_orphan_without_upgrading_or_recovering(
    tmp_path, capsys, command, exit_code
):
    db = tmp_path / "schema3.sqlite3"
    legacy_three(db)
    before = db.read_bytes()
    assert main(["--db", str(db), command]) == exit_code
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 3
    assert payload["operation_state"] == "orphaned"
    assert payload["recent_recoveries"] == []
    assert payload["scheduler"] is None
    assert db.read_bytes() == before
    assert not Path(str(db) + ".lock").exists()
    assert not Path(str(db) + ".scheduler.lock").exists()


def test_qa_sigkill_committed_first_source_survives_and_recovers_once(tmp_path, monkeypatch):
    import os
    import signal
    import time

    from waagd_discovery.collection import run_once
    from waagd_discovery.diagnostics import service_status

    db = tmp_path / "state.sqlite3"
    registry, policy = configuration(tmp_path)
    ready = tmp_path / "second-source-entered"
    program = """
import sys
import time
from pathlib import Path
from waagd_discovery import collection
from waagd_discovery.models import FetchResult
RSS = b'''<rss version="2.0"><channel><title>QA</title><item>
<link>https://example.org/story</link><guid>preserved-guid</guid><title>Preserved</title>
<description>Technical metadata description long enough for eligibility.</description>
<pubDate>Sun, 18 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>'''
class Client:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def fetch(self, source):
        if source.id == "later":
            Path(sys.argv[4]).write_text("first source committed")
            time.sleep(30)
        return FetchResult(200, RSS)
collection.HttpClient = Client
collection.run_once(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(db), str(registry), str(policy), str(ready)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    monkeypatch.setattr("waagd_discovery.collection.HttpClient", NotModifiedClient)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child did not reach the second source after committing first"
        status = service_status(db)
        assert status["operation_state"] == "active"
        assert status["latest_counts"]["items_added"] == 1
        interrupted_id = status["latest_run"]["id"]
        with pytest.raises(DiscoveryError, match="database_busy"):
            run_once(db, registry, policy)
        process.kill()
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    status = service_status(db)
    assert status["operation_state"] == "orphaned"
    assert status["recent_recoveries"] == []
    result = run_once(db, registry, policy)
    assert result["recovered_runs"] == [interrupted_id]
    assert result["status"] == "success"
    assert result["items_added"] == 0
    with snapshot(db) as repository:
        assert repository.stats()["articles"] == 1
        assert repository.stats()["observations"] == 1
        assert repository.connection.execute("SELECT count(*) FROM source_guids").fetchone()[0] == 1
        assert repository.connection.execute("SELECT count(*) FROM url_aliases").fetchone()[0] == 1
        old_outcomes = repository.connection.execute(
            "SELECT source_id, items_added FROM source_runs WHERE run_id=?", (interrupted_id,)
        ).fetchall()
        assert [tuple(row) for row in old_outcomes] == [("qa", 1)]
        recovery = dict(repository.connection.execute("SELECT * FROM run_interruptions").fetchone())
        assert recovery["run_id"] == interrupted_id
        assert recovery["reason"] == "operation_lock_reacquired"
        assert datetime.fromisoformat(recovery["detected_at"]).utcoffset().total_seconds() == 0
        repository.verify()
    assert run_once(db, registry, policy)["recovered_runs"] == []


@pytest.mark.parametrize("suffix", [".lock", ".scheduler.lock", "-wal", "-shm"])
def test_qa_export_through_db_alias_cannot_replace_canonical_lock_or_sidecar(
    tmp_path, capsys, suffix
):
    db = tmp_path / "real.sqlite3"
    registry, policy = configuration(tmp_path)
    with Repository(db):
        pass
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(db)
    output = Path(str(db) + suffix)
    output.write_bytes(b"protected inode")
    before_inode = output.stat().st_ino
    args = ["--db", str(alias), "--sources", str(registry), "--policy", str(policy)]
    assert main([*args, "export", "--output", str(output)]) == 1
    assert output.read_bytes() == b"protected inode"
    assert output.stat().st_ino == before_inode
    assert "export_target_protected" in capsys.readouterr().err


def test_qa_real_sigterm_during_scheduled_fetch_stops_and_keeps_committed_source(tmp_path):
    import os
    import time

    from waagd_discovery.diagnostics import service_status

    db = tmp_path / "state.sqlite3"
    registry, policy = configuration(tmp_path)
    scheduler = tmp_path / "scheduler.yaml"
    scheduler.write_text("scheduler: {interval_seconds: 1, heartbeat_seconds: 1}")
    ready = tmp_path / "second-source-entered"
    program = """
import sys
import time
from pathlib import Path
from waagd_discovery import collection
from waagd_discovery.cli import main
from waagd_discovery.models import FetchResult
RSS = b'''<rss version="2.0"><channel><title>QA</title><item>
<link>https://example.org/story</link><title>Preserved</title>
<description>Technical metadata description long enough for eligibility.</description>
<pubDate>Sun, 18 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>'''
class Client:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def fetch(self, source):
        if source.id == "later":
            Path(sys.argv[5]).write_text("first source committed")
            time.sleep(30)
        return FetchResult(200, RSS)
collection.HttpClient = Client
sys.exit(main(["--db", sys.argv[1], "--sources", sys.argv[2], "--policy", sys.argv[3],
               "--scheduler-config", sys.argv[4], "scheduler"]))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            program,
            str(db),
            str(registry),
            str(policy),
            str(scheduler),
            str(ready),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "scheduler did not reach blocking second source"
        status = service_status(db)
        assert status["scheduler_alive"] is True
        assert status["operation_state"] == "active"
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        stopped = json.loads(stdout)
        assert stopped["status"] == "stopped"
        assert stopped["last_error"] == "collection_interrupted"
        assert stopped["last_result"]["status"] == "interrupted"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    status = service_status(db)
    assert status["scheduler_alive"] is False
    assert status["operation_active"] is False
    assert status["operation_state"] == "idle"
    with snapshot(db) as repository:
        assert repository.stats()["runs"] == 1
        assert repository.stats()["articles"] == 1
        assert repository.stats()["observations"] == 1
        assert (
            repository.connection.execute("SELECT status FROM runs").fetchone()[0] == "interrupted"
        )
        assert (
            repository.connection.execute("SELECT count(*) FROM run_interruptions").fetchone()[0]
            == 0
        )
        repository.verify()


@pytest.mark.parametrize("suffix", [".lock", ".scheduler.lock"])
def test_qa_alias_export_keeps_live_kernel_lock_inode_and_liveness(tmp_path, capsys, suffix):
    from waagd_discovery.locking import lock_is_held

    db = tmp_path / "real.sqlite3"
    registry, policy = configuration(tmp_path)
    with Repository(db):
        pass
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(db)
    output = Path(str(db) + suffix)
    with held_kernel_lock(db, suffix):
        identity = output.stat().st_ino
        assert lock_is_held(output) is True
        assert (
            main(
                [
                    "--db",
                    str(alias),
                    "--sources",
                    str(registry),
                    "--policy",
                    str(policy),
                    "export",
                    "--output",
                    str(output),
                ]
            )
            == 1
        )
        assert output.stat().st_ino == identity
        assert lock_is_held(output) is True
    assert lock_is_held(output) is False
    assert "export_target_protected" in capsys.readouterr().err
