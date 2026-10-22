"""Tool-free Gateway turn for bounded, untrusted editorial selection."""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import tempfile
from pathlib import Path

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.gateway_adapter import _ENVIRONMENT, GatewayEditor
from waagd_discovery.editorial.selection import SelectionResult, accept_selection

_GUIDE = (
    b"Return JSON only with exactly schema_version and selected_candidate_keys. "
    b"schema_version must be integer 1. selected_candidate_keys must be an array of zero to five "
    b"candidate keys from the supplied shortlist. Choose only for satire potential, recency, "
    b"international clarity and originality. You cannot verify facts, alter safety policy, "
    b"publish, use tools, or add any fields. Empty selection is allowed.\nShortlist JSON:\n"
)


class GatewaySelector:
    """One bounded selection call; no model output can grant publication authority."""

    def __init__(self, editor: GatewayEditor) -> None:
        self.editor = editor
        self.last_metrics: dict[str, str] | None = None

    def select(self, shortlist: SelectionResult) -> SelectionResult:
        if shortlist.selected or len(shortlist.shortlist) > 25:
            raise EditorialError("selection_input_invalid")
        payload = io.encode({"schema_version": 1, "candidates": shortlist.shortlist})
        message = _GUIDE + payload
        if len(message) > self.editor.budget_policy.max_prompt_bytes:
            raise EditorialError("selection_request_too_large")
        for attempt in range(2):
            try:
                raw = self._invoke(message, payload, attempt)
                try:
                    selection = io.decode(raw)
                except io.BridgeError:
                    raise EditorialError("selection_model_json_invalid") from None
                selected = accept_selection(shortlist, selection)
                self.last_metrics = {
                    "path": "gateway_selection",
                    "outcome": "selected",
                    "retry": str(attempt),
                    "selected": str(len(selected.selected)),
                }
                return selected
            except (io.BridgeError, UnicodeError, EditorialError) as error:
                # Only invalid/empty model JSON is safe to retry. A transport exit
                # remains ambiguous and fail-closed.
                if (
                    str(error)
                    not in {
                        "selection_model_json_invalid",
                        "selection_empty_response",
                        "selection_schema_invalid",
                    }
                    or attempt
                ):
                    code = self._failure_code(error)
                    self.last_metrics = {
                        "path": "gateway_selection",
                        "outcome": "failed",
                        "reason": code,
                    }
                    raise EditorialError(code) from None
        raise AssertionError("unreachable")

    @staticmethod
    def _failure_code(error: BaseException) -> str:
        """Expose only a bounded operational category, never Gateway text."""

        code = str(error)
        mapping = {
            "gateway_outcome_unknown": "selection_outcome_unknown",
            "gateway_response_too_large": "selection_response_too_large",
            "gateway_envelope_invalid": "selection_envelope_invalid",
            "gateway_empty_response": "selection_empty_response",
            "gateway_model_json_invalid": "selection_model_json_invalid",
            "gateway_cost_unverified": "selection_budget_unverified",
            "gateway_tool_attestation_missing": "selection_attestation_missing",
            "gateway_tool_attestation_conflict": "selection_attestation_conflict",
            "gateway_tools_used": "selection_tools_used",
            "gateway_tools_exposed": "selection_tools_exposed",
        }
        if code in mapping:
            return mapping[code]
        if code in {
            "selection_schema_invalid",
            "selection_input_invalid",
            "selection_request_too_large",
            "selection_failed",
            "selection_transport_unavailable",
            "selection_outcome_unknown",
            "selection_response_too_large",
            "selection_envelope_invalid",
            "selection_empty_response",
            "selection_model_json_invalid",
            "selection_budget_unverified",
            "selection_attestation_missing",
            "selection_attestation_conflict",
            "selection_tools_used",
            "selection_tools_exposed",
        }:
            return code
        return "selection_transport_unavailable"

    def _invoke(self, message: bytes, payload: bytes, attempt: int) -> bytes:
        with tempfile.TemporaryDirectory(prefix="waagd-selection-") as temporary:
            path = Path(temporary) / "message.json"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(message)
                stream.flush()
                os.fsync(stream.fileno())
            import subprocess

            session = (
                "selection-" + hashlib.sha256(payload).hexdigest()
                if attempt == 0
                else "selection-retry-" + secrets.token_hex(16)
            )
            try:
                process = subprocess.Popen(
                    [
                        str(self.editor.executable),
                        "agent",
                        "--agent",
                        self.editor.agent_id,
                        "--session-key",
                        session,
                        "--message-file",
                        str(path),
                        "--json",
                        "--timeout",
                        str(max(1, int(self.editor.timeout) - 2)),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd="/",
                    env={key: value for key, value in os.environ.items() if key in _ENVIRONMENT},
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError:
                raise EditorialError("selection_transport_unavailable") from None
            try:
                envelope = self.editor._read_bounded(process)
            finally:
                if process.poll() is None:
                    self.editor._terminate(process)
        if process.returncode != 0:
            raise EditorialError("selection_outcome_unknown")
        try:
            _, _, text, containers = self.editor._parse_envelope_text(envelope)
            if self.editor.budget_policy.max_reported_cost_usd is not None:
                cost = self.editor._reported_cost(containers)
                if (
                    not isinstance(cost, (int, float))
                    or isinstance(cost, bool)
                    or not math.isfinite(cost)
                    or cost < 0
                    or cost > self.editor.budget_policy.max_reported_cost_usd
                ):
                    raise EditorialError("gateway_cost_unverified")
            if not text.strip():
                raise EditorialError("gateway_empty_response")
            return text.encode("utf-8")
        except EditorialError:
            raise
        except (io.BridgeError, UnicodeError, ValueError, TypeError):
            raise EditorialError("gateway_envelope_invalid") from None
