#!/usr/bin/env python3
"""Actual-container lifecycle acceptance using only disposable offline fixtures."""

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("WAAGD_SMOKE_IMAGE", "waagd-discovery:scheduler-smoke")
IDENTIFIER = "waagd-scheduler-smoke-" + uuid.uuid4().hex[:12]
VOLUME = IDENTIFIER + "-data"
CONTAINER = IDENTIFIER
STARTED = time.monotonic()


def docker(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], text=True, capture_output=True, check=False, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
    return result


def wait_for(predicate: Any, label: str, timeout: int = 60) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.25)
    raise AssertionError(f"Timed out: {label}")


def flags(scratch: Path) -> list[str]:
    return [
        "--network",
        "none",
        "--init",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--mount",
        f"type=volume,src={VOLUME},dst=/data",
        "--mount",
        f"type=bind,src={scratch / 'sources.yaml'},dst=/app/config/sources.yaml,readonly",
        "--mount",
        f"type=bind,src={scratch / 'scheduler.yaml'},dst=/app/config/scheduler.yaml,readonly",
    ]


def helper(scratch: Path, code: str) -> str:
    return docker(
        "run", "--rm", *flags(scratch), "--entrypoint", "python", IMAGE, "-c", code
    ).stdout


def state(scratch: Path) -> dict[str, Any]:
    code = """import json,sqlite3
from pathlib import Path
p=Path('/data/discovery.sqlite3')
if not p.exists():
 print('{}')
else:
 c=sqlite3.connect('file:/data/discovery.sqlite3?mode=ro',uri=True)
 tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 if 'run_interruptions' not in tables:
  print('{}')
 else:
  counts={k:c.execute('SELECT count(*) FROM '+k).fetchone()[0] for k in
   ('articles','observations','runs','source_runs','run_interruptions')}
  counts['success']=c.execute("SELECT count(*) FROM runs WHERE status='success'").fetchone()[0]
  counts['running']=c.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0]
  query="SELECT count(*) FROM runs WHERE status='interrupted'"
  counts['interrupted']=c.execute(query).fetchone()[0]
  batch=c.execute('SELECT eligible_count FROM filter_batches ORDER BY sequence DESC LIMIT 1')
  latest=batch.fetchone()
  counts['eligible']=latest[0] if latest else 0
  counts['session']=c.execute('SELECT state_json FROM service_state WHERE id=1').fetchone()
  counts['session']=json.loads(counts['session'][0]) if counts['session'] else {}
  print(json.dumps(counts))
"""
    return json.loads(helper(scratch, code))


def command(scratch: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return docker("run", "--rm", *flags(scratch), IMAGE, *args, check=check)


def mark(scratch: Path, name: str, present: bool) -> None:
    code = f"from pathlib import Path;p=Path('/data/{name}');"
    code += "p.touch()" if present else "p.unlink(missing_ok=True)"
    helper(scratch, code)


def marked(scratch: Path, name: str) -> bool:
    return (
        helper(scratch, f"from pathlib import Path;print(Path('/data/{name}').exists())").strip()
        == "True"
    )


def start(scratch: Path) -> None:
    docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        *flags(scratch),
        "--restart",
        "unless-stopped",
        "--stop-timeout",
        "30",
        "--health-cmd",
        "waagd-discovery health",
        "--health-interval",
        "2s",
        "--health-timeout",
        "5s",
        "--health-retries",
        "3",
        "--mount",
        f"type=bind,src={ROOT / 'scripts/scheduler-smoke-fixture.py'},dst=/fixture.py,readonly",
        "--entrypoint",
        "python",
        IMAGE,
        "/fixture.py",
        "scheduler",
    )


def main() -> None:
    if os.environ.get("WAAGD_SMOKE_SKIP_BUILD") != "1":
        docker("build", "--tag", IMAGE, str(ROOT), timeout=600)
    docker("image", "inspect", IMAGE)
    docker("volume", "create", "--label", "com.waagd-discovery.smoke=true", VOLUME)
    with tempfile.TemporaryDirectory(prefix="waagd-scheduler-fixture-") as directory:
        scratch = Path(directory)
        (scratch / "sources.yaml").write_text("""sources:
  - id: first
    name: Offline first
    url: https://example.org/first.xml
  - id: second
    name: Offline second
    url: https://example.org/second.xml
""")
        (scratch / "scheduler.yaml").write_text(
            "scheduler:\n  interval_seconds: 2\n  heartbeat_seconds: 1\n"
        )
        try:
            start(scratch)
            wait_for(lambda: state(scratch).get("success", 0) >= 2, "two real cycles")
            initial = state(scratch)
            assert initial["articles"] == initial["eligible"] == 2, initial
            assert initial["observations"] >= 4, initial
            assert command(scratch, "health").returncode == 0
            config = json.loads(docker("inspect", CONTAINER).stdout)[0]
            host = config["HostConfig"]
            assert host["Memory"] == 512 * 1024 * 1024 and host["NanoCpus"] == 10**9
            assert host["PidsLimit"] == 64 and host["ReadonlyRootfs"]
            assert host["NetworkMode"] == "none" and "ALL" in host["CapDrop"]
            assert "no-new-privileges:true" in host["SecurityOpt"]
            assert host["RestartPolicy"]["Name"] == "unless-stopped"
            assert config["Config"]["User"] == "10001:10001"
            assert not config["NetworkSettings"]["Ports"]
            duplicate = docker(
                "run", "--rm", *flags(scratch), IMAGE, "scheduler", check=False, timeout=20
            )
            assert duplicate.returncode != 0, "A second scheduler acquired the lifetime lock"

            # Acquire the actual manual-operation lock from a separate container.
            mark(scratch, "smoke-hold-lock", True)
            locker = CONTAINER + "-locker"
            lock_code = """import time
from pathlib import Path
from waagd_discovery.locking import database_lock
from waagd_discovery.models import DiscoveryError
while True:
 try:
  with database_lock(Path('/data/discovery.sqlite3')):
   Path('/data/smoke-lock-ready').touch()
   while Path('/data/smoke-hold-lock').exists():time.sleep(.1)
  break
 except DiscoveryError:time.sleep(.1)
"""
            docker(
                "run",
                "-d",
                "--name",
                locker,
                *flags(scratch),
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                lock_code,
            )
            try:
                wait_for(lambda: marked(scratch, "smoke-lock-ready"), "manual lock acquired")
                before = state(scratch)["runs"]
                time.sleep(3)
                assert state(scratch)["runs"] == before, "Scheduler overlapped a manual lock"
                assert command(scratch, "health").returncode == 0
            finally:
                mark(scratch, "smoke-hold-lock", False)
                docker("rm", "-f", locker, check=False)
            wait_for(lambda: state(scratch)["runs"] > before, "collection after busy skip")

            # SIGTERM during source two must preserve source one's commit.
            mark(scratch, "smoke-block", True)
            wait_for(lambda: marked(scratch, "smoke-blocked"), "active collection for SIGTERM")
            before_stop = state(scratch)
            docker("stop", "--time", "10", CONTAINER)
            stopped = state(scratch)
            assert stopped["running"] == 0 and stopped["interrupted"] >= 1, stopped
            assert stopped["source_runs"] >= before_stop["source_runs"], stopped
            assert command(scratch, "health", check=False).returncode != 0
            mark(scratch, "smoke-block", False)
            mark(scratch, "smoke-blocked", False)
            docker("start", CONTAINER)
            wait_for(lambda: state(scratch)["success"] > stopped["success"], "graceful restart")
            session = state(scratch)["session"]["session_id"]
            docker("restart", "--time", "10", CONTAINER)
            wait_for(
                lambda: state(scratch)["session"].get("session_id") != session,
                "docker restart session",
            )

            # Explicit docker kill is an operator stop on some Engine versions;
            # start explicitly, then prove lock-gated orphan recovery.
            mark(scratch, "smoke-block", True)
            mark(scratch, "smoke-blocked", False)
            wait_for(lambda: marked(scratch, "smoke-blocked"), "active collection for SIGKILL")
            pre_crash = state(scratch)
            docker("kill", "--signal", "KILL", CONTAINER)
            killed = state(scratch)
            assert killed["running"] == 1, killed
            docker("start", CONTAINER)
            wait_for(
                lambda: state(scratch).get("run_interruptions", 0) > pre_crash["run_interruptions"],
                "crash audit after explicit restart",
            )
            mark(scratch, "smoke-block", False)
            wait_for(
                lambda: state(scratch)["success"] > pre_crash["success"],
                "successful cycle after crash",
            )
            # A process crash, distinct from an explicit Docker operator stop,
            # must activate Engine's configured unless-stopped restart policy.
            session = state(scratch)["session"]["session_id"]
            docker(
                "exec",
                CONTAINER,
                "python",
                "-c",
                "import os,signal;from pathlib import Path;"
                "child=int(Path('/proc/1/task/1/children').read_text().split()[0]);"
                "os.kill(child,signal.SIGKILL)",
                check=False,
            )
            wait_for(
                lambda: state(scratch)["session"].get("session_id") != session,
                "Engine restart after scheduler process SIGKILL",
            )
            assert json.loads(docker("inspect", CONTAINER).stdout)[0]["RestartCount"] >= 1

            # Recreate from the same volume, then run diagnostics and health.
            before_recreate = state(scratch)
            docker("stop", "--time", "10", CONTAINER)
            docker("rm", CONTAINER)
            start(scratch)
            wait_for(
                lambda: state(scratch)["success"] > before_recreate["success"],
                "recreate with persistent SQLite",
            )
            wait_for(
                lambda: (
                    json.loads(docker("inspect", CONTAINER).stdout)[0]["State"]
                    .get("Health", {})
                    .get("Status")
                    == "healthy"
                ),
                "Docker healthcheck",
            )
            command(scratch, "verify")
            diagnostic = command(scratch, "status")
            assert json.loads(diagnostic.stdout), diagnostic.stdout
            final = state(scratch)
            assert final["articles"] == final["eligible"] == 2, final
            assert final["run_interruptions"] >= 1, final
            memory_peak = int(
                docker(
                    "exec",
                    CONTAINER,
                    "python",
                    "-c",
                    "from pathlib import Path;"
                    "print(Path('/sys/fs/cgroup/memory.peak').read_text())",
                ).stdout
            )
            inspect = json.loads(docker("inspect", CONTAINER).stdout)[0]
            assert not inspect["State"]["OOMKilled"]
            assert memory_peak < 512 * 1024 * 1024, memory_peak
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "image": IMAGE,
                        "elapsed_seconds": round(time.monotonic() - STARTED, 3),
                        "memory_peak_bytes": memory_peak,
                        "memory_peak_scope": "final_recreated_scheduler_cgroup",
                        "memory_limit_bytes": host["Memory"],
                        "cpu_limit": host["NanoCpus"] / 10**9,
                        "sqlite": final,
                        "checks": [
                            "cycles",
                            "deduplication",
                            "singleton",
                            "manual_lock",
                            "sigterm",
                            "restart",
                            "docker_sigkill_recovery",
                            "process_sigkill_autorestart",
                            "recovery",
                            "recreate",
                            "health",
                            "hardening",
                            "integrity",
                        ],
                    },
                    indent=2,
                )
            )
        except BaseException:
            print(docker("logs", "--tail", "80", CONTAINER, check=False).stderr)
            raise
        finally:
            docker("rm", "-f", CONTAINER, check=False)
            docker("rm", "-f", CONTAINER + "-locker", check=False)
            docker("volume", "rm", VOLUME, check=False)


if __name__ == "__main__":
    main()
