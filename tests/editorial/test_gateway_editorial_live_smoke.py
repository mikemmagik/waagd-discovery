"""The operator smoke command uses only a synthetic in-memory Gateway request."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gateway-editorial-live-smoke.py"
URL = "https://example.invalid/waagd-editorial-smoke"
KEY = hashlib.sha256(URL.encode("utf-8")).hexdigest()
NOW = datetime(2026, 10, 21, tzinfo=UTC).isoformat()


def _decision(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
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
    return {**result, **changes}


def _gateway(
    tmp_path: Path,
    *,
    text: str | None = None,
    raw: bytes | None = None,
    outputs: list[bytes] | None = None,
) -> Path:
    response = {
        "status": "ok",
        "result": {
            "payloads": [{"text": json.dumps(_decision()) if text is None else text}],
            "meta": {"toolSummary": {"calls": 0, "tools": []}, "costUsd": 0.01},
        },
    }
    path = tmp_path / "openclaw"
    sessions = tmp_path / "sessions"
    encoded = json.dumps(response).encode() if raw is None else raw
    output_values = outputs or [encoded]
    path.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "assert sys.argv[1] == 'agent'\n"
        "assert '--agent' in sys.argv and sys.argv[sys.argv.index('--agent') + 1] == 'waagd'\n"
        "session = sys.argv[sys.argv.index('--session-key') + 1]\n"
        "assert session.startswith('editorial-smoke-')\n"
        f"sessions = pathlib.Path({str(sessions)!r})\n"
        "index = len(sessions.read_text().splitlines()) if sessions.exists() else 0\n"
        "sessions.open('a').write(session + '\\n')\n"
        f"outputs = {output_values!r}\n"
        "sys.stdout.buffer.write(outputs[min(index, len(outputs) - 1)])\n"
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
    gateway = _gateway(tmp_path)
    command = [sys.executable, str(SCRIPT), "--gateway-executable", str(gateway)]
    results = [
        subprocess.run(
            command,
            cwd=ROOT,
            env=_environment(config),
            text=True,
            capture_output=True,
            check=False,
        )
        for _ in range(2)
    ]
    for result in results:
        assert result.returncode == 0
        assert result.stderr == ""
        assert json.loads(result.stdout) == {
            "decision": "NEEDS_VERIFICATION",
            "schema_version": 1,
            "status": "ok",
        }
    sessions = (tmp_path / "sessions").read_text().splitlines()
    assert len(sessions) == 2
    assert sessions[0] != sessions[1]


@pytest.mark.parametrize(
    ("text", "raw", "error"),
    [
        ("", None, "empty_response"),
        ("model prose", None, "model_json_invalid"),
        (None, b"not an envelope", "envelope_invalid"),
        (json.dumps(_decision(summary="s" * 161)), None, "schema_invalid:$.summary:maxLength:160"),
    ],
)
def test_live_smoke_reports_safe_diagnostics(
    tmp_path: Path, text: str | None, raw: bytes | None, error: str
) -> None:
    config = tmp_path / "claw2.json"
    config.write_text("{}")
    config.chmod(0o600)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--gateway-executable",
            str(_gateway(tmp_path, text=text, raw=raw)),
        ],
        cwd=ROOT,
        env=_environment(config),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == error
    assert "model prose" not in result.stdout
    assert "s" * 161 not in result.stdout
    if error != "envelope_invalid":
        assert payload["generation"] == {"gateway_status": "ok"}


def test_live_smoke_retries_one_definite_format_failure_in_new_session(tmp_path: Path) -> None:
    config = tmp_path / "claw2.json"
    config.write_text("{}")
    config.chmod(0o600)
    valid = json.dumps(
        {
            "status": "ok",
            "result": {
                "payloads": [{"text": json.dumps(_decision())}],
                "meta": {"toolSummary": {"calls": 0, "tools": []}, "costUsd": 0.01},
            },
        }
    ).encode()
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--gateway-executable",
            str(
                _gateway(
                    tmp_path,
                    outputs=[
                        json.dumps(
                            {
                                "status": "ok",
                                "result": {
                                    "payloads": [{"text": "prose"}],
                                    "meta": {
                                        "toolSummary": {"calls": 0, "tools": []},
                                        "costUsd": 0.01,
                                    },
                                },
                            }
                        ).encode(),
                        valid,
                    ],
                )
            ),
            "--retry-format-once",
            "--max-total-cost-usd",
            "0.10",
        ],
        cwd=ROOT,
        env=_environment(config),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "NEEDS_VERIFICATION"
    sessions = (tmp_path / "sessions").read_text().splitlines()
    assert len(sessions) == 2 and sessions[0] != sessions[1]


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
