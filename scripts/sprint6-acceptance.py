#!/usr/bin/env python3
"""Offline, cross-repository Sprint 6 release acceptance.

This program invokes only local tests and temporary Docker smoke data. It never
contacts a Gateway, Telegram, Cloudflare, or an operator database.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str, cwd: Path) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def uv_command(*args: str, cwd: Path) -> None:
    uv = shutil.which("uv")
    adjacent = Path(sys.executable).with_name("uv")
    if uv is not None or adjacent.is_file():
        uv = uv or str(adjacent)
        command(uv, "run", "--frozen", *args, cwd=cwd)
        return
    if args[0] == "python":
        command(sys.executable, *args[1:], cwd=cwd)
        return
    command(sys.executable, "-m", *args, cwd=cwd)


def docker_acceptance(platform: str) -> None:
    image = f"waagd-discovery:sprint6-acceptance-{platform}"
    environment = os.environ | {
        "DOCKER_DEFAULT_PLATFORM": f"linux/{platform}",
        "WAAGD_SMOKE_IMAGE": image,
        "WAAGD_SMOKE_SKIP_BUILD": "1",
    }
    with tempfile.TemporaryDirectory(prefix="waagd-sprint6-config-") as scratch:
        context = Path(scratch) / "context"
        subprocess.run(
            [sys.executable, "scripts/docker-config-smoke.py", "--prepare-context", str(context)],
            cwd=ROOT,
            check=True,
            env=environment,
        )
        subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                f"linux/{platform}",
                "--tag",
                image,
                str(context),
            ],
            cwd=ROOT,
            check=True,
            env=environment,
        )
        for argv in (
            [sys.executable, "scripts/docker-config-smoke.py", "--context", str(context)],
            ["sh", "scripts/docker-smoke.sh"],
            [sys.executable, "scripts/docker-bridge-smoke.py"],
            [sys.executable, "scripts/docker-editorial-smoke.py"],
        ):
            print("+", " ".join(argv), flush=True)
            subprocess.run(argv, cwd=ROOT, check=True, env=environment)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-root", type=Path, required=True)
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--docker-platform", action="append", choices=("amd64", "arm64"))
    args = parser.parse_args(argv)
    site = args.site_root.resolve()
    if site == ROOT or not (site / "package.json").is_file():
        parser.error("--site-root must be a separate checkout containing package.json")

    uv_command("python", "scripts/check-secrets.py", cwd=ROOT)
    uv_command("ruff", "check", ".", cwd=ROOT)
    uv_command("ruff", "format", "--check", ".", cwd=ROOT)
    uv_command("mypy", "src", cwd=ROOT)
    uv_command("pytest", "-m", "not live", "--cov-fail-under=85", cwd=ROOT)
    command("npm", "run", "validate", cwd=site)
    if not args.skip_docker:
        for platform in args.docker_platform or ("amd64", "arm64"):
            docker_acceptance(platform)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
