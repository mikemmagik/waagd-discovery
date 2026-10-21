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


def executable(tmp_path: Path, outputs: list[str]) -> Path:
    path = tmp_path / "fake-openclaw"
    rendered = repr(outputs)
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"outputs={rendered}\n"
        f"state=pathlib.Path({str(tmp_path / 'calls')!r})\n"
        "count=int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(count + 1))\n"
        "assert b'Shortlist JSON' in pathlib.Path("
        "sys.argv[sys.argv.index('--message-file') + 1]).read_bytes()\n"
        "answer=outputs[min(count,len(outputs)-1)]\n"
        "print(json.dumps({'status':'ok','result':{'payloads':[{'text':answer}],'meta':{'agentMeta':{'terminalReceipt':{'successfulToolNames':[]},'codeModeEngaged':False},'systemPromptReport':{'tools':{'entries':[]}}}}}))\n"
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
    "output",
    [
        "not-json",
        json.dumps({"schema_version": 1, "selected_candidate_keys": ["x" * 64]}),
        json.dumps({"schema_version": 1, "selected_candidate_keys": ["a" * 64, "a" * 64]}),
    ],
)
def test_gateway_selector_retries_once_then_fails_closed(tmp_path: Path, output: str) -> None:
    selector = GatewaySelector(GatewayEditor(executable(tmp_path, [output])))
    with pytest.raises(EditorialError, match="selection_failed"):
        selector.select(shortlist())
    assert (tmp_path / "calls").read_text() == "2"
