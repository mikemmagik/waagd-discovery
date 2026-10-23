"""Small command surface for the trusted staging-only deployment wrapper."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from waagd_discovery.models import DiscoveryError
from waagd_discovery.staging import (
    _active,
    bridge_stage,
    cycle,
    editorial_stage,
    load_config,
    set_activation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waagd-staging")
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("activate", "deactivate", "status", "cycle"):
        commands.add_parser(name)
    for name in ("bridge", "editorial"):
        command = commands.add_parser(name)
        command.add_argument("--export", type=Path, required=True)
    cycle_parser = commands.choices["cycle"]
    cycle_parser.add_argument("--cycle-id")
    cycle_parser.add_argument("--local-date")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "activate":
            result = set_activation(config, active=True)
        elif args.command == "deactivate":
            result = set_activation(config, active=False)
        elif args.command == "status":
            result = {"status": "completed", "site_active": _active(config)}
        elif args.command == "bridge":
            result = bridge_stage(config, args.export)
        elif args.command == "editorial":
            result = editorial_stage(config, args.export)
        else:
            result = cycle(config, args.config, args.cycle_id, args.local_date)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result.get("status") != "operator_required" else 1
    except DiscoveryError as error:
        print(json.dumps({"status": "operator_required", "code": error.code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
