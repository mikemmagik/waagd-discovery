"""A fixed, tool-free subprocess mock. No real OpenClaw or LLM adapter exists."""

import os
import subprocess
import sys
import tempfile
from typing import Any

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import (
    EditorialError,
    validate_decision,
    validate_request,
)

_MAXIMUM = 128 * 1024
_WORKER = r"""
import datetime
import json
import resource
import sys
resource.setrlimit(resource.RLIMIT_FSIZE, (131072, 131072))
resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
raw = sys.stdin.buffer.read(131073)
if len(raw) > 131072:
    raise SystemExit(2)
request = json.loads(raw)
candidate = request["candidate"]
verification = request["verification"]
result = {
    "schema_version": 1,
    "candidate_key": candidate["candidate_key"],
    "decision": "NEEDS_VERIFICATION",
    "source": candidate["url"],
    "date": None,
    "factual_summary": "Source metadata was supplied; the reported event remains unverified.",
    "satirical_angle": "",
    "headline": "",
    "summary": "",
    "article": "",
    "established_facts": ["A candidate URL and publication metadata were supplied."],
    "unverified_claims": ["The event and substantive claims require independent confirmation."],
    "open_questions": ["Which independent evidence corroborates the material claims?"],
    "checked_sources": sorted({item["url"] for item in verification["evidence"]}),
    "verification_evidence": [item["id"] for item in verification["evidence"]],
    "risks": ["Simulation only; this mock does not make factual or editorial attestations."],
    "rejection_reason": None,
    "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "model_identifier": "mock-editor-v1",
    "prompt_version": "waagd-editorial-v1",
}
sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))
"""


class MockEditor:
    """Validate both boundaries around a separate fixed Python process.

    No source string is executable code, a shell argument, an environment variable,
    a path or an instruction. The mock has no agent tools and never returns READY.
    Its output file and CPU are bounded inside the fixed worker before parsing.
    """

    def __init__(self, *, timeout: float = 10):
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 0 < timeout <= 30
        ):
            raise EditorialError("editor_timeout_invalid")
        self.timeout = timeout

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        validate_request(request)
        payload = io.encode(request)
        if len(payload) > _MAXIMUM:
            raise EditorialError("editor_request_too_large")
        # CLI and Controller forbid mixing the subprocess mock into real results.
        if request["simulation"] is not True:
            raise EditorialError("real_editor_unavailable")
        environment = {"PATH": os.defpath, "PYTHONUTF8": "1", "PYTHONHASHSEED": "0"}
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    [sys.executable, "-I", "-c", _WORKER],
                    input=payload,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=self.timeout,
                    check=False,
                    env=environment,
                    cwd="/",
                    close_fds=True,
                )
                if result.returncode != 0:
                    raise EditorialError("editor_unavailable")
                output.seek(0)
                raw = output.read(_MAXIMUM + 1)
            if len(raw) > _MAXIMUM:
                raise EditorialError("editor_response_too_large")
            decision = io.decode(raw)
            validate_decision(decision, request)
            return decision  # type: ignore[no-any-return]
        except subprocess.TimeoutExpired:
            raise EditorialError("editor_timeout") from None
        except (OSError, io.BridgeError):
            raise EditorialError("editor_unavailable") from None
