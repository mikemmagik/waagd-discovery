"""The operator smoke command uses only a synthetic in-memory Gateway request."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gateway-editorial-live-smoke.py"
URL = "https://example.invalid/waagd-editorial-smoke"
KEY = hashlib.sha256(URL.encode("utf-8")).hexdigest()
NOW = datetime(2026, 10, 21, tzinfo=UTC).isoformat()


def _decision() -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate_key": KEY,
        "decision": "NEEDS_VERIFICATION",
        "source": URL,
        "date": None,
        "factual_summary": "The synthetic candidate has not been independently corroborated.",
        "satirical_angle": "",
        "headline": "",
        "summary": "",
        "article": "",
        "established_facts": ["A synthetic candidate was supplied."],
        "unverified_claims": ["The synthetic report describes a real event."],
        "open_questions": ["Which independent source corroborates the report?"],
        "checked_sources": [],
        "verification_evidence": [],
        "risks": ["No independent corroboration exists."],
        "rejection_reason": None,
        "created_at": NOW,
        "model_identifier": "fake-openclaw",
        "prompt_version": "waagd-editorial-v1",
    }


def _gateway(tmp_path: Path) -> Path:
    response = {
        "status": "ok",
        "result": {
            "payloads": [{"text": json.dumps(_decision())}],
            "meta": {"toolSummary": {"calls": 0, "tools": []}, "costUsd": 0.01},
        },
    }
    path = tmp_path / "openclaw"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "assert sys.argv[1] == 'agent'\n"
        "assert '--agent' in sys.argv and sys.argv[sys.argv.index('--agent') + 1] == 'waagd'\n"
        f"sys.stdout.buffer.write({json.dumps(response).encode()!r})\n"
    )
    path.chmod(0o700)
    return path


def _environment(config: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["OPENCLAW_CONFIG_PATH"] = str(config)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return environment


def test_live_smoke_command_accepts_isolated_tool_free_gateway(tmp_path: Path) -> None:
    config = tmp_path / "claw2.json"
    config.write_text("{}")
    config.chmod(0o600)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--gateway-executable", str(_gateway(tmp_path))],
        cwd=ROOT,
        env=_environment(config),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "decision": "NEEDS_VERIFICATION",
        "schema_version": 1,
        "status": "ok",
    }


def test_live_smoke_command_requires_explicit_isolated_config(tmp_path: Path) -> None:
    environment = _environment(tmp_path / "missing-claw2.json")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--gateway-executable", str(_gateway(tmp_path))],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "error": "gateway_isolated_config_required",
        "status": "error",
    }
