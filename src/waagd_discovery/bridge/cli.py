"""Explicit offline host entrypoint; never discovers or activates an agent."""

import argparse
import json
import time
from pathlib import Path

from waagd_discovery.bridge import Bridge, BridgeError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waagd-bridge")
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--schema-version", type=int, choices=(1, 2), default=1)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync")
    commands.add_parser("status")
    ack = commands.add_parser("ack")
    ack.add_argument("--delivery-id", required=True)
    dismiss = commands.add_parser("dismiss")
    dismiss.add_argument("--delivery-id", required=True)
    watch = commands.add_parser("watch")
    watch.add_argument("--interval", type=int, default=60)
    commands.add_parser("migrate-state")
    inspect = commands.add_parser("inspect-delivery")
    inspect.add_argument("--delivery-id", required=True)
    conditional = commands.add_parser("ack-if-current")
    conditional.add_argument("--delivery-id", required=True)
    conditional.add_argument("--digest", required=True)
    args = parser.parse_args(argv)
    try:
        bridge = Bridge(
            args.export,
            args.workspace_root,
            args.state_dir,
            args.limit,
            schema_version=args.schema_version,
        )
        if args.command == "inspect-delivery":
            result = bridge.inspect_delivery(args.delivery_id)
        elif args.command == "ack-if-current":
            result = bridge.ack_if_current(args.delivery_id, args.digest)
        elif args.command == "migrate-state":
            result = bridge.migrate_state()
        elif args.command == "watch":
            if not 10 <= args.interval <= 3600:
                raise BridgeError("interval_must_be_10_to_3600")
            while True:
                try:
                    print(json.dumps(bridge.sync(), sort_keys=True), flush=True)
                except (BridgeError, OSError):
                    print(
                        json.dumps({"status": "error", "error": "bridge_sync_failed"}), flush=True
                    )
                time.sleep(args.interval)
        elif args.command == "ack":
            result = bridge.ack(args.delivery_id)
        elif args.command == "dismiss":
            result = bridge.dismiss(args.delivery_id)
        elif args.command == "status":
            result = bridge.status()
        else:
            result = bridge.sync()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BridgeError, OSError) as error:
        # Never emit untrusted exception text, candidate content or filesystem paths.
        code = str(error) if isinstance(error, BridgeError) else "filesystem_operation_failed"
        print(json.dumps({"status": "error", "error": code}, sort_keys=True))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
