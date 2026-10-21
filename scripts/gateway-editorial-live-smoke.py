#!/usr/bin/env python3
"""Run one non-persistent, synthetic editorial policy or Gateway smoke check.

The caller must point OPENCLAW_CONFIG_PATH at the isolated ``waagd``/``claw2``
configuration. This script never opens Discovery SQLite, Bridge state, or an
export directory. It has no publication or acknowledgement capability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from waagd_discovery.editorial.contracts import EditorialError, make_request, trusted_decision
from waagd_discovery.editorial.gateway_adapter import GatewayBudgetPolicy, GatewayEditor

_URL = "https://example.invalid/waagd-editorial-smoke"
_NOW = datetime(2026, 10, 21, tzinfo=UTC).isoformat()


def _request(*, verified: bool = False) -> dict[str, Any]:
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
    evidence = []
    claims = []
    if verified:
        evidence = [
            {
                "id": "a" * 64,
                "url": _URL,
                "retrieved_at": _NOW,
                "sha256": "b" * 64,
                "excerpt": "Synthetic fixture assertion.",
                "source_kind": "article",
            },
            {
                "id": "c" * 64,
                "url": "https://independent.example.invalid/waagd-editorial-smoke",
                "retrieved_at": _NOW,
                "sha256": "d" * 64,
                "excerpt": "Synthetic fixture assertion.",
                "source_kind": "attestation",
            },
        ]
        claims = [
            {
                "claim": "Synthetic fixture assertion.",
                "evidence_ids": ["a" * 64, "c" * 64],
                "independently_confirmed": True,
            }
        ]
    verification = {
        "schema_version": 1,
        "candidate_key": key,
        "verification_status": "VERIFIED" if verified else "PARTIAL",
        "canonical_url": _URL,
        "fetched_at": None,
        "http_status": None,
        "publisher": None,
        "publication_date": None,
        "event_date": None,
        "evidence": evidence,
        "uncertainties": []
        if verified
        else ["Synthetic candidate has no independent corroboration."],
        "errors": [],
        "claim_support": claims,
    }
    return make_request(candidate, verification, simulation=False)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-executable", type=Path)
    parser.add_argument("--verified", action="store_true")
    parser.add_argument("--max-total-timeout", type=float, default=120)
    parser.add_argument("--max-total-cost-usd", type=float, default=0.10)
    parser.add_argument("--retry-format-once", action="store_true")
    return parser.parse_args()


def _diagnostic(code: str) -> str:
    if code == "gateway_model_json_invalid":
        return "model_json_invalid"
    if code == "gateway_empty_response":
        return "empty_response"
    if code == "gateway_envelope_invalid":
        return "envelope_invalid"
    if code.startswith("editorial_schema_invalid:"):
        return "schema_invalid:" + code.removeprefix("editorial_schema_invalid:")
    if code.startswith("gateway_creative_schema_invalid"):
        return "creative_schema_invalid"
    if code == "gateway_creative_safety_invalid":
        return "creative_safety_invalid"
    if code == "editorial_schema_invalid":
        return "schema_invalid"
    return code


def _error(code: str, completion: dict[str, str] | None = None) -> int:
    result: dict[str, Any] = {"status": "error", "error": _diagnostic(code)}
    if completion is not None:
        result["generation"] = completion
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 1


def _format_error(code: str) -> bool:
    return code in {"gateway_model_json_invalid", "gateway_empty_response"} or code.startswith(
        ("gateway_creative_schema_invalid", "editorial_schema_invalid")
    )


def main() -> int:
    args = _arguments()
    if not args.verified:
        # This is deliberately independent of an OpenClaw installation.  It
        # proves the PARTIAL path abstains without a model invocation.
        try:
            result = trusted_decision(_request())
        except EditorialError as error:
            return _error(str(error))
        if result["decision"] != "NEEDS_VERIFICATION":
            return _error("gateway_partial_decision_invalid")
        print(
            json.dumps(
                {"decision": "NEEDS_VERIFICATION", "schema_version": 1, "status": "ok"},
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 0

    # The VERIFIED fixture tests formatting only.  Its synthetic attestation is
    # not real independent verification and never touches Discovery state.
    config = os.environ.get("OPENCLAW_CONFIG_PATH")
    if not config or not Path(config).is_file() or args.gateway_executable is None:
        return _error("gateway_isolated_config_required")
    attempts = 2 if args.retry_format_once else 1
    if (
        not math.isfinite(args.max_total_timeout)
        or not 5 * attempts <= args.max_total_timeout <= 600
        or not math.isfinite(args.max_total_cost_usd)
        or not 0 < args.max_total_cost_usd <= 1
    ):
        return _error("gateway_smoke_budget_invalid")
    for attempt in range(attempts):
        editor = GatewayEditor(
            args.gateway_executable,
            timeout=args.max_total_timeout / attempts,
            budget_policy=GatewayBudgetPolicy(
                max_reported_cost_usd=args.max_total_cost_usd / attempts
            ),
        )
        try:
            result = editor.evaluate(_request(verified=True), fresh_session=True)
        except EditorialError as error:
            code = str(error)
            # A completed, attested formatting failure is definite. It may use
            # one new session; ambiguous Gateway outcomes are never retried.
            if attempt == 0 and args.retry_format_once and _format_error(code):
                continue
            return _error(code, editor.last_completion)
        if result["decision"] != "READY_FOR_REVIEW":
            return _error("gateway_verified_decision_invalid", editor.last_completion)
        print(
            json.dumps(
                {
                    "decision": "READY_FOR_REVIEW",
                    "fixture": "synthetic_verified",
                    "schema_version": 1,
                    "status": "ok",
                },
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 0
    raise AssertionError("format retry loop must return")


if __name__ == "__main__":
    raise SystemExit(main())
