from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.gateway_adapter import GatewayEditor
from waagd_discovery.editorial.selection import SelectionResult
from waagd_discovery.editorial.selection_gateway import GatewaySelector


def executable(
    tmp_path: Path, outputs: list[str], *, payload_extras: dict[str, object] | None = None
) -> Path:
    path = tmp_path / "fake-openclaw"
    rendered = repr(outputs)
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"outputs={rendered}\n"
        f"payload_extras={payload_extras!r}\n"
        f"state=pathlib.Path({str(tmp_path / 'calls')!r})\n"
        "count=int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(count + 1))\n"
        "assert b'Shortlist JSON' in pathlib.Path("
        "sys.argv[sys.argv.index('--message-file') + 1]).read_bytes()\n"
        "answer=outputs[min(count,len(outputs)-1)]\n"
        "response={'status':'ok','result':{'payloads':[{'text':answer,**(payload_extras or {})}],"
        "'meta':{'agentMeta':{'terminalReceipt':{'successfulToolNames':[],'runId':'run',"
        "'sessionId':'session','turnId':'turn','requested':{'mode':'tool-free'},"
        "'effective':{'mode':'tool-free'},'rerouted':False,'terminalDisposition':'not-visible'},"
        "'codeModeEngaged':False},'systemPromptReport':{'tools':{'entries':[]}}}}}\n"
        "print(json.dumps(response))\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def shortlist() -> SelectionResult:
    return SelectionResult(
        shortlist=[
            {
                "candidate_key": key * 64,
                "title": key,
                "description": "",
                "publisher": "The Guardian",
            }
            for key in "abcde"
        ],
        selected=[],
        rejected={},
    )


@pytest.mark.parametrize("count", [0, 1, 3, 5])
def test_gateway_selector_accepts_bounded_choices(tmp_path: Path, count: int) -> None:
    keys = [item["candidate_key"] for item in shortlist().shortlist[:count]]
    output = json.dumps({"schema_version": 1, "selected_candidate_keys": keys})
    selector = GatewaySelector(GatewayEditor(executable(tmp_path, [output])))
    selected = selector.select(shortlist())
    assert selected.selected == keys


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("not-json", "selection_model_json_invalid"),
        (
            json.dumps({"schema_version": 1, "selected_candidate_keys": ["x" * 64]}),
            "selection_schema_invalid",
        ),
        (
            json.dumps({"schema_version": 1, "selected_candidate_keys": ["a" * 64, "a" * 64]}),
            "selection_schema_invalid",
        ),
    ],
)
def test_gateway_selector_retries_once_then_fails_closed(
    tmp_path: Path, output: str, expected: str
) -> None:
    selector = GatewaySelector(GatewayEditor(executable(tmp_path, [output])))
    with pytest.raises(EditorialError, match=f"^{expected}$"):
        selector.select(shortlist())
    assert (tmp_path / "calls").read_text() == "2"


def test_gateway_selector_accepts_openclaw_empty_payload_metadata(tmp_path: Path) -> None:
    """Match the known 2026.10.3 envelope shape without a live Gateway call."""

    keys = [item["candidate_key"] for item in shortlist().shortlist[:1]]
    output = json.dumps({"schema_version": 1, "selected_candidate_keys": keys})
    selector = GatewaySelector(
        GatewayEditor(executable(tmp_path, [output], payload_extras={"media": [], "meta": None}))
    )

    assert selector.select(shortlist()).selected == keys


def test_gateway_selector_reports_attestation_failure_without_model_text(tmp_path: Path) -> None:
    path = tmp_path / "bad-openclaw"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "print(json.dumps({'status':'ok','result':{'payloads':[{'text':'{}'}]}}))\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    selector = GatewaySelector(GatewayEditor(path))

    with pytest.raises(EditorialError, match="^selection_attestation_missing$"):
        selector.select(shortlist())
    assert selector.last_metrics == {
        "path": "gateway_selection",
        "outcome": "failed",
        "reason": "selection_attestation_missing",
    }


def test_gateway_selector_keeps_nonzero_subprocess_outcome_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "failed-openclaw"
    path.write_text(f"#!{sys.executable}\nraise SystemExit(7)\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    selector = GatewaySelector(GatewayEditor(path))

    with pytest.raises(EditorialError, match="^selection_outcome_unknown$"):
        selector.select(shortlist())
