"""The operator smoke command uses only synthetic in-memory editor fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gateway-editorial-live-smoke.py"


def _creative(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "satirical_angle": "Bureaucracy mistakes activity for progress.",
        "headline": "A Committee Finds Another Committee",
        "summary": "A tidy machine keeps moving while the answer waits outside the meeting room.",
        "article": " ".join(["bureaucracy"] * 90),
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
            "payloads": [{"text": json.dumps(_creative()) if text is None else text}],
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
        "assert session.startswith(('editorial-smoke-', 'editorial-format-retry-'))\n"
        f"sessions = pathlib.Path({str(sessions)!r})\n"
        "index = len(sessions.read_text().splitlines()) if sessions.exists() else 0\n"
        "sessions.open('a').write(session + '\\n')\n"
        f"outputs = {output_values!r}\n"
        "sys.stdout.buffer.write(outputs[min(index, len(outputs) - 1)])\n"
    )
    path.chmod(0o700)
    return path


def _environment(config: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    if config is not None:
        environment["OPENCLAW_CONFIG_PATH"] = str(config)
    else:
        environment.pop("OPENCLAW_CONFIG_PATH", None)
    return environment


def test_partial_live_smoke_is_deterministic_and_does_not_call_gateway(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    command = [sys.executable, str(SCRIPT), "--gateway-executable", str(gateway)]
    results = [
        subprocess.run(
            command, cwd=ROOT, env=_environment(), text=True, capture_output=True, check=False
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
    assert not (tmp_path / "sessions").exists()


@pytest.mark.parametrize(
    ("text", "raw", "error"),
    [
        ("", None, "empty_response"),
        ("model prose", None, "model_json_invalid"),
        (None, b"not an envelope", "envelope_invalid"),
        (json.dumps(_creative(summary="s" * 161)), None, "creative_schema_invalid"),
    ],
)
def test_verified_live_smoke_reports_safe_diagnostics(
    tmp_path: Path, text: str | None, raw: bytes | None, error: str
) -> None:
    config = tmp_path / "claw2.json"
    config.write_text("{}")
    config.chmod(0o600)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--verified",
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


def test_verified_live_smoke_retries_one_format_failure_in_new_session(tmp_path: Path) -> None:
    config = tmp_path / "claw2.json"
    config.write_text("{}")
    config.chmod(0o600)
    valid = json.dumps(
        {
            "status": "ok",
            "result": {
                "payloads": [{"text": json.dumps(_creative())}],
                "meta": {"toolSummary": {"calls": 0, "tools": []}, "costUsd": 0.01},
            },
        }
    ).encode()
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--verified",
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
    assert json.loads(result.stdout) == {
        "decision": "READY_FOR_REVIEW",
        "fixture": "synthetic_verified",
        "schema_version": 1,
        "status": "ok",
    }
    sessions = (tmp_path / "sessions").read_text().splitlines()
    assert len(sessions) == 2 and sessions[0] != sessions[1]


def test_verified_live_smoke_requires_explicit_isolated_config(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--verified",
            "--gateway-executable",
            str(_gateway(tmp_path)),
        ],
        cwd=ROOT,
        env=_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "error": "gateway_isolated_config_required",
        "status": "error",
    }
