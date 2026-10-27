"""Attested Bridge ``sync`` stage for the autonomous composition.

The public ``waagd-bridge sync`` command intentionally reports the Bridge
summary.  That summary is useful to operators, but it is not the narrow stage
protocol consumed by ``waagd-autonomous-cycle``.  This adapter calls the same
Bridge operation directly and emits ``completed`` only after validating the
complete, documented summary for a settled empty inbox or a durable delivered
inbox.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from waagd_discovery.bridge import Bridge, BridgeError

_SUMMARY_KEYS = frozenset(
    {
        "status",
        "pending_delivery_id",
        "prepared",
        "delivered",
        "processed",
        "dismissed",
        "known",
    }
)


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _delivery_id(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _attests_sync_completed(value: Any) -> bool:
    """Accept only summaries which prove that this ``sync`` completed.

    ``pending_expired`` deliberately remains a hold, and ``prepared`` is not
    accepted because its delivery publication is not yet proven complete.  The
    adapter never parses arbitrary subprocess output: it validates the direct
    return value of ``Bridge.sync`` before emitting its own fixed protocol.
    """

    if not isinstance(value, dict) or set(value) != _SUMMARY_KEYS:
        return False
    if not all(
        _count(value[key]) for key in ("prepared", "delivered", "processed", "dismissed", "known")
    ):
        return False
    prepared = value["prepared"]
    delivered = value["delivered"]
    processed = value["processed"]
    dismissed = value["dismissed"]
    if value["known"] != prepared + delivered + processed + dismissed:
        return False
    if value["status"] == "idle":
        return value["pending_delivery_id"] is None and prepared == 0 and delivered == 0
    if value["status"] == "delivered":
        return _delivery_id(value["pending_delivery_id"]) and prepared == 0 and delivered > 0
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waagd-bridge-stage")
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--schema-version", type=int, choices=(1, 2), default=1)
    args = parser.parse_args(argv)
    try:
        summary = Bridge(
            args.export,
            args.workspace_root,
            args.state_dir,
            args.limit,
            schema_version=args.schema_version,
        ).sync()
    except (BridgeError, OSError):
        print(json.dumps({"status": "operator_required", "code": "bridge_sync_failed"}))
        return 1
    if not _attests_sync_completed(summary):
        print(json.dumps({"status": "operator_required", "code": "bridge_sync_unconfirmed"}))
        return 1
    print('{"status":"completed"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
