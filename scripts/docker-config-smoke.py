#!/usr/bin/env python3
"""Build with private source YAML and read every embedded policy as the app UID."""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = (
    "sources.yaml",
    "filter.yaml",
    "backup.yaml",
    "scheduler.yaml",
    "handoff.yaml",
    "trusted_publishers.yaml",
)
PROBE = r"""
import errno
import json
import os
import stat
from pathlib import Path

from waagd_discovery.backups import load_retention_policy
from waagd_discovery.config import load_sources
from waagd_discovery.filtering import load_policy
from waagd_discovery.handoff import load_handoff_policy
from waagd_discovery.scheduler import load_scheduler_policy
from waagd_discovery.editorial.trusted_publishers import load_trusted_publishers

assert (os.getuid(), os.getgid()) == (10001, 10001)
status = Path("/proc/self/status").read_text()
assert "NoNewPrivs:\t1" in status
assert "CapEff:\t0000000000000000" in status
directory = Path("/app/config")
info = directory.stat()
assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 0, 0o755)
loaders = {
    "sources.yaml": load_sources,
    "filter.yaml": load_policy,
    "backup.yaml": load_retention_policy,
    "scheduler.yaml": load_scheduler_policy,
    "handoff.yaml": load_handoff_policy,
    "trusted_publishers.yaml": load_trusted_publishers,
}
assert {path.name for path in directory.iterdir()} == set(loaders)
files = {}
for name, loader in loaders.items():
    path = directory / name
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 10001, 0o440)
    assert path.read_bytes(), name
    loader(path)
    for operation in (lambda: path.chmod(0o640), lambda: path.open("wb")):
        try:
            operation()
        except OSError as exc:
            assert exc.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
        else:
            raise AssertionError(f"Application modified trusted configuration: {name}")
    files[name] = {"uid": info.st_uid, "gid": info.st_gid, "mode": "0440", "loaded": True}
assert not Path("/var/run/docker.sock").exists()
assert not Path("/data/discovery.sqlite3").exists()
print(json.dumps({"uid": os.getuid(), "gid": os.getgid(), "files": files,
                  "policy_loading": "PASS", "modification_denied": True}))
"""


def prepare_context(destination: Path) -> None:
    # Copy only runtime inputs; never package local databases, exports or secrets.
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copytree(ROOT / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (destination / "config").mkdir(mode=0o755)
    assert {path.name for path in (ROOT / "config").glob("*.yaml")} == set(CONFIGS)
    for name in CONFIGS:
        path = destination / "config" / name
        shutil.copyfile(ROOT / "config" / name, path)
        path.chmod(0o600)


def verify_context(context: Path) -> dict[str, str]:
    modes = {}
    for name in CONFIGS:
        path = context / "config" / name
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode), name
        assert stat.S_IMODE(info.st_mode) == 0o600, name
        modes[name] = "0600"
    return modes


def smoke(context: Path) -> None:
    source_modes = verify_context(context)
    image = os.environ.get("WAAGD_SMOKE_IMAGE", "waagd-discovery:config-smoke")
    if os.environ.get("WAAGD_SMOKE_SKIP_BUILD", "0") != "1":
        subprocess.run(
            ["docker", "build", "--tag", image, str(context)],
            check=True,
            stdout=sys.stderr,
        )
    image_info = json.loads(
        subprocess.check_output(["docker", "image", "inspect", image], text=True)
    )[0]
    assert image_info["Config"]["User"] == "10001:10001"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
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
            "--entrypoint",
            "python",
            image,
            "-",
        ],
        input=PROBE,
        text=True,
        capture_output=True,
        check=True,
        timeout=180,
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "image": image,
                "image_id": image_info["Id"],
                "architecture": image_info["Architecture"],
                "source_config_modes": source_modes,
                "runtime": json.loads(result.stdout),
                "restrictions": {
                    "read_only": True,
                    "network": "none",
                    "cap_drop": ["ALL"],
                    "no_new_privileges": True,
                },
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prepare-context", type=Path, help="Create fixture build context and exit")
    group.add_argument("--context", type=Path, help="Use an already prepared 0600 fixture context")
    args = parser.parse_args()
    if args.prepare_context:
        prepare_context(args.prepare_context)
        print(json.dumps({"source_config_modes": verify_context(args.prepare_context)}))
    elif args.context:
        smoke(args.context)
    else:
        with tempfile.TemporaryDirectory(prefix="waagd-config-smoke-") as temporary:
            context = Path(temporary) / "context"
            prepare_context(context)
            smoke(context)


if __name__ == "__main__":
    main()
