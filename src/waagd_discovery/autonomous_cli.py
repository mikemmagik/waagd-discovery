"""Host-facing controls for the autonomous daily coordinator."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from waagd_discovery.autonomous import DailyCoordinator, DailyPolicy, subprocess_runner
from waagd_discovery.models import DiscoveryError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="waagd-autonomous")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--timezone", default="Asia/Bangkok")
    parser.add_argument("--daily-time", default="08:00")
    parser.add_argument("--maximum-attempts", type=int, default=3)
    parser.add_argument("--cycle-command", help="Trusted absolute executable and fixed arguments")
    parser.add_argument("--cycle-timeout-seconds", type=int, default=28_800)
    parser.add_argument("--inner-cycle-timeout-seconds", type=int, default=25_200)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "install",
        "enable",
        "status",
        "pause",
        "resume",
        "disable",
        "uninstall",
        "run-due",
    ):
        commands.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = DailyPolicy(args.timezone, args.daily_time, args.maximum_attempts)
        command = tuple(shlex.split(args.cycle_command)) if args.cycle_command else ()
        runner = (
            subprocess_runner(
                command,
                args.cycle_timeout_seconds,
                args.inner_cycle_timeout_seconds,
            )
            if command
            else lambda _id, _date: {"status": "retryable", "code": "runner_missing"}
        )
        coordinator = DailyCoordinator(args.state_dir, policy, runner)
        method = getattr(coordinator, args.command.replace("-", "_"))
        result = method()
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except DiscoveryError as error:
        print(json.dumps({"status": "error", "code": error.code}), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print(json.dumps({"status": "error", "code": "autonomous_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
