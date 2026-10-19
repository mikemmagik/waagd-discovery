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
FINAL_SITE_SHA = "ca112b2ab89a7ec0d82b85b06df07bb86a35b41c"
TARGET_SITE_E2E_FILE = "tests/backend.integration.test.ts"
TARGET_SITE_E2E_NAME = (
    "actual Discovery 17f59e3 producer enters the core ingress once and reaches "
    "Worker/D1 and frontend parser"
)


def command(*args: str, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True, env=env)


def expected_failure(
    *args: str,
    cwd: Path,
    expected_text: str,
    env: dict[str, str] | None = None,
) -> None:
    """Require a negative acceptance case to fail for its intended reason."""
    print("+", " ".join(args), "(expected failure)", flush=True)
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, env=env)
    output = result.stdout + result.stderr
    if result.returncode == 0 or expected_text not in output:
        raise RuntimeError(
            "negative acceptance case did not reject the invalid input as expected:\n" + output
        )


def checked_site_revision(site: Path) -> None:
    revision = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=site, text=True).strip()
    if revision != FINAL_SITE_SHA:
        raise RuntimeError(f"site checkout must be pinned to {FINAL_SITE_SHA}, found {revision}")
    dirty = subprocess.check_output(("git", "status", "--porcelain"), cwd=site, text=True).strip()
    if dirty:
        raise RuntimeError("site checkout must be clean for release acceptance")


def run_target_site_e2e(site: Path) -> None:
    """Run and prove execution of the frozen site's concrete E2E test file."""
    args = ("node", "--test", TARGET_SITE_E2E_FILE)
    print("+", " ".join(args), flush=True)
    result = subprocess.run(
        args,
        cwd=site,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ
        | {
            "WAAGD_DISCOVERY_ROOT": str(ROOT),
            "WAAGD_DISCOVERY_PYTHON": sys.executable,
        },
    )
    print(result.stdout, end="", flush=True)
    result.check_returncode()
    target_lines = [line for line in result.stdout.splitlines() if TARGET_SITE_E2E_NAME in line]
    if not any(
        line.lstrip().startswith(("ok ", "✔")) and "SKIP" not in line for line in target_lines
    ):
        raise RuntimeError("the required Site RC E2E test was not executed")


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
    checked_site_revision(site)

    uv_command("python", "scripts/check-secrets.py", cwd=ROOT)
    uv_command("ruff", "check", ".", cwd=ROOT)
    uv_command("ruff", "format", "--check", ".", cwd=ROOT)
    uv_command("mypy", "src", cwd=ROOT)
    uv_command("pytest", "-m", "not live", "--cov-fail-under=85", cwd=ROOT)
    command("npm", "run", "validate", cwd=site)
    with tempfile.TemporaryDirectory(prefix="waagd-sprint6-e2e-") as scratch:
        invalid_fixture = Path(scratch) / "publication-intents.json"
        expected_failure(
            sys.executable,
            "scripts/sprint6-publication-fixture.py",
            "--output",
            str(invalid_fixture),
            cwd=ROOT,
            expected_text="publication_output_invalid",
        )

        fixture = Path(scratch) / "publication-intent.json"
        uv_command(
            "python",
            "scripts/sprint6-publication-fixture.py",
            "--output",
            str(fixture),
            cwd=ROOT,
        )
        command(
            "npx",
            "--no-install",
            "tsx",
            str(ROOT / "scripts" / "validate_site_intent.mjs"),
            str(site / "automation" / "core" / "intent.ts"),
            str(fixture),
            cwd=site,
        )
        # The concrete file constructs an actual Discovery fixture and exercises
        # the real validator, ingress, publisher, Worker/D1 and frontend parser.
        run_target_site_e2e(site)
    if not args.skip_docker:
        for platform in args.docker_platform or ("amd64", "arm64"):
            docker_acceptance(platform)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
