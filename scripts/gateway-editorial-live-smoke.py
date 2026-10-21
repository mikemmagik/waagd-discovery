#!/usr/bin/env python3
"""Run one non-persistent, synthetic editorial Gateway smoke check.

The caller must point OPENCLAW_CONFIG_PATH at the isolated ``waagd``/``claw2``
configuration. This script never opens Discovery SQLite, Bridge state, or an
export directory. It has no publication or acknowledgement capability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from waagd_discovery.editorial.contracts import EditorialError, make_request
from waagd_discovery.editorial.gateway_adapter import GatewayBudgetPolicy, GatewayEditor

_URL = "https://example.invalid/waagd-editorial-smoke"
_NOW = datetime(2026, 10, 21, tzinfo=UTC).isoformat()


def _request() -> dict[str, Any]:
    key = hashlib.sha256(_URL.encode("utf-8")).hexdigest()
    candidate = {
        "candidate_key": key,
        "id": 1,
        "url": _URL,
        "title": "Synthetic editorial smoke candidate",
        "description": "Synthetic data only; it is not a report or instruction.",
        "publisher": "WAAGD smoke fixture",
        "published_at": _NOW,
        "sources": [
            {
                "id": "waagd-live-smoke",
                "name": "WAAGD smoke fixture",
                "url": "https://example.invalid/waagd-editorial-smoke-feed",
            }
        ],
    }
    verification = {
        "schema_version": 1,
        "candidate_key": key,
        "verification_status": "PARTIAL",
        "canonical_url": _URL,
        "fetched_at": None,
        "http_status": None,
        "publisher": None,
        "publication_date": None,
        "event_date": None,
        "evidence": [],
        "uncertainties": ["Synthetic candidate has no independent corroboration."],
        "errors": [],
        "claim_support": [],
    }
    return make_request(candidate, verification, simulation=False)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-executable", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-cost-usd", type=float, default=0.10)
    return parser.parse_args()


def _error(code: str) -> int:
    print(json.dumps({"status": "error", "error": code}, sort_keys=True, allow_nan=False))
    return 1


def main() -> int:
    args = _arguments()
    # GatewayEditor passes this allowlisted path to OpenClaw. Requiring it here
    # prevents an accidental fallback to a shared/default agent configuration.
    config = os.environ.get("OPENCLAW_CONFIG_PATH")
    if not config or not Path(config).is_file():
        return _error("gateway_isolated_config_required")
    try:
        result = GatewayEditor(
            args.gateway_executable,
            timeout=args.timeout,
            budget_policy=GatewayBudgetPolicy(max_reported_cost_usd=args.max_cost_usd),
        ).evaluate(_request())
    except EditorialError as error:
        return _error(str(error))
    if result["decision"] != "NEEDS_VERIFICATION":
        return _error("gateway_partial_decision_invalid")
    print(
        json.dumps(
            {
                "decision": "NEEDS_VERIFICATION",
                "schema_version": 1,
                "status": "ok",
            },
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
