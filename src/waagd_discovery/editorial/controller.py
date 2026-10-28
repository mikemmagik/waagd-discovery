"""Agent-independent editorial orchestration with a durable post-result ACK outbox."""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from waagd_discovery.bridge import io
from waagd_discovery.bridge.validation import timestamp, validate_envelope
from waagd_discovery.editorial.contracts import (
    PROTECTED_DELIVERIES,
    VERIFICATION_POLICY,
    BridgePort,
    EditorAdapter,
    EditorialError,
    Verifier,
    completed_editorial_rejection,
    digest,
    make_request,
    utc,
    validate_decision,
    validate_input,
    validate_verification,
)
from waagd_discovery.editorial.fidelity import require_source_fidelity
from waagd_discovery.editorial.store import EditorialStore


def _failed(candidate: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "candidate_key": candidate["candidate_key"],
        "verification_status": "FAILED",
        "canonical_url": candidate["url"],
        "fetched_at": None,
        "http_status": None,
        "publisher": None,
        "publication_date": None,
        "event_date": None,
        "evidence": [],
        "uncertainties": [
            "The event and its substantive claims have not been independently verified."
        ],
        "errors": [code],
        "claim_support": [],
    }


def _stored_envelope(raw: str) -> dict[str, Any]:
    """Private SQLite JSON still needs semantic validation after corruption."""
    try:
        if not isinstance(raw, str):
            raise EditorialError("persisted_delivery_invalid")
        value = io.decode(raw.encode())
        validate_envelope(value)
        keys = [item["candidate_key"] for item in value["candidates"]]
        if (
            not value["delivery_id"]
            or value["delivery_id"] in PROTECTED_DELIVERIES
            or not keys
            or len(set(keys)) != len(keys)
        ):
            raise EditorialError("persisted_delivery_invalid")
        return value  # type: ignore[no-any-return]
    except (EditorialError, io.BridgeError, KeyError, TypeError, ValueError):
        raise EditorialError("persisted_delivery_invalid") from None


class Controller:
    def __init__(
        self,
        inbox: Path,
        store_path: Path,
        bridge: BridgePort,
        verifier: Verifier,
        editor: EditorAdapter,
        simulation: bool = True,
        allow_ack: bool = False,
        protected_roots: tuple[Path, ...] = (),
        selector: Any | None = None,
        fidelity_reviewer: Any | None = None,
    ):
        self.inbox = Path(inbox)
        self.store_path = Path(store_path)
        io.path_parts(self.inbox)
        io.path_parts(self.store_path)
        if self.inbox.name != "inbox.json" or self.inbox.parent.name != "waagd-discovery":
            raise EditorialError("inbox_path_invalid")
        if not isinstance(simulation, bool) or not isinstance(allow_ack, bool):
            raise EditorialError("editorial_mode_invalid")
        if simulation and allow_ack:
            raise EditorialError("simulation_ack_forbidden")
        self.bridge, self.verifier, self.editor, self.selector = bridge, verifier, editor, selector
        self.fidelity_reviewer = fidelity_reviewer
        self.simulation, self.allow_ack = simulation, allow_ack
        self.protected_roots = (self.inbox.parent.parent, *protected_roots)
        # Validate overlaps before even reading an inbox or constructing a store.
        EditorialStore(self.store_path, simulation=simulation, protected_roots=self.protected_roots)

    def _store(self, *, readonly: bool = False) -> EditorialStore:
        return EditorialStore(
            self.store_path,
            simulation=self.simulation,
            readonly=readonly,
            protected_roots=self.protected_roots,
        )

    def _has_state(self) -> bool:
        try:
            directory = io.open_directory(self.store_path.parent, private=True)
        except FileNotFoundError:
            return False
        try:
            for suffix in ("", ".lock", ".initialized.json"):
                try:
                    os.stat(self.store_path.name + suffix, dir_fd=directory, follow_symlinks=False)
                    return True
                except FileNotFoundError:
                    pass
            return False
        finally:
            os.close(directory)

    def _read(self) -> dict[str, Any]:
        directory = io.open_directory(self.inbox.parent, private=True)
        try:
            value = io.decode(io.read_file(directory, self.inbox.name, 128 * 1024, private=True))
            check = io.open_directory(self.inbox.parent, private=True)
            try:
                left, right = os.fstat(directory), os.fstat(check)
                if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
                    raise EditorialError("inbox_directory_changed")
            finally:
                os.close(check)
            if not isinstance(value, dict):
                raise EditorialError("inbox_invalid")
            return value
        finally:
            os.close(directory)

    def _fresh(self, envelope: dict[str, Any], now: datetime | None) -> None:
        if envelope["delivery_id"] in PROTECTED_DELIVERIES:
            raise EditorialError("protected_delivery")
        if timestamp(envelope["expires_at"]) <= utc(now):
            raise EditorialError("delivery_expired")

    def _authority(
        self, envelope: dict[str, Any], now: datetime | None, *, allow_acknowledged: bool = True
    ) -> dict[str, Any]:
        delivery = envelope["delivery_id"]
        if delivery in PROTECTED_DELIVERIES:
            raise EditorialError("protected_delivery")
        receipt = self.bridge.inspect_delivery(delivery)
        allowed = {"pending", "acknowledged"} if allow_acknowledged else {"pending"}
        if (
            receipt.get("delivery_id") != delivery
            or receipt.get("status") not in allowed
            or receipt.get("payload_digest") != digest(envelope)
            or receipt.get("schema_version") != envelope["bridge_schema_version"]
            or receipt.get("expires_at") != envelope["expires_at"]
        ):
            raise EditorialError("bridge_authority_mismatch")
        self._fresh(envelope, now)
        return receipt

    def _candidate(
        self,
        store: EditorialStore,
        envelope: dict[str, Any],
        candidate: dict[str, Any],
        revision: int,
        now: datetime | None,
        *,
        reconsider: bool = False,
        verification_only: bool = False,
    ) -> str | None:
        key = candidate["candidate_key"]
        delivery = envelope["delivery_id"]
        if store.has_decision(key, revision):
            # Revalidate persisted results before treating them as completed.
            try:
                row = store.connection.execute(
                    "SELECT request,result FROM decisions WHERE candidate_key=? AND revision=?",
                    (key, revision),
                ).fetchone()
                request = io.decode(row["request"].encode())
                if (
                    request["candidate"]["candidate_key"] != key
                    or request["candidate"]["url"] != candidate["url"]
                    or request["simulation"] is not self.simulation
                ):
                    raise EditorialError("persisted_result_invalid")
                validate_decision(io.decode(row["result"].encode()), request)
            except (KeyError, TypeError, io.BridgeError, EditorialError):
                raise EditorialError("persisted_result_invalid") from None
            return None
        self._fresh(envelope, now)
        verification = store.verification(key, revision)
        if verification is None:
            attempt = store.start_attempt(delivery, key, "verification", utc(now).isoformat())
            error_code = None
            if envelope["bridge_schema_version"] == 1:
                verification = _failed(candidate, "v1_fetch_forbidden")
            else:
                try:
                    verification = self.verifier.verify(candidate, policy_id=VERIFICATION_POLICY)
                    validate_verification(verification, key)
                except (EditorialError, io.BridgeError, OSError, ValueError, TypeError):
                    error_code = "source_verification_failed"
                    verification = _failed(candidate, error_code)
            self._fresh(envelope, now)
            validate_verification(verification, key)
            store.save_verification(key, revision, verification, utc(now).isoformat())
            store.finish_attempt(attempt, utc(now).isoformat(), error_code)
        validate_verification(verification, key)
        if verification_only:
            return None
        self._fresh(envelope, now)
        request = make_request(candidate, verification, simulation=self.simulation)
        operation = (
            "editorial_gateway" if getattr(self.editor, "gateway_backed", False) else "editorial"
        )
        attempt = store.start_attempt(delivery, key, operation, utc(now).isoformat())
        try:
            result = self.editor.evaluate(request)
            validate_decision(result, request)
            # Deterministic checks remain a cheap first boundary. They never
            # replace the bounded model-assisted review below.
            require_source_fidelity(request, result)
        except (EditorialError, io.BridgeError, OSError, ValueError, TypeError) as error:
            code = self._editorial_failure_code(error)
            if not reconsider and self._terminal_rejection(code, self.editor):
                rejection = completed_editorial_rejection(request, now=now)
                store.save_terminal_rejection(
                    key,
                    revision,
                    request,
                    rejection,
                    delivery,
                    ((attempt, code),),
                    utc(now).isoformat(),
                )
                return None
            if getattr(self.editor, "gateway_backed", False):
                store.finish_gateway_attempt(
                    attempt,
                    utc(now).isoformat(),
                    code,
                    self._gateway_diagnostic(self.editor, error),
                )
            else:
                store.finish_attempt(attempt, utc(now).isoformat(), code)
            store.mark_selection_editorial(delivery, key, "failed")
            return code
        fidelity_attempt: str | None = None
        if result["decision"] == "READY_FOR_REVIEW" and self.fidelity_reviewer is not None:
            fidelity_attempt = store.start_attempt(delivery, key, "fidelity", utc(now).isoformat())
            try:
                review = self.fidelity_reviewer.review(request, result)
                if getattr(review, "code", None) is not None:
                    raise EditorialError(review.code)
            except (EditorialError, io.BridgeError, OSError, ValueError, TypeError) as error:
                code = self._editorial_failure_code(error)
                if not reconsider and self._terminal_rejection(
                    code, getattr(self.fidelity_reviewer, "editor", None)
                ):
                    assert fidelity_attempt is not None
                    rejection = completed_editorial_rejection(request, now=now)
                    store.save_terminal_rejection(
                        key,
                        revision,
                        request,
                        rejection,
                        delivery,
                        ((attempt, None), (fidelity_attempt, code)),
                        utc(now).isoformat(),
                    )
                    return None
                # Creative generation completed; fidelity is a separate policy
                # gate. Finish both records together so the parent never
                # remains falsely started after a terminal fidelity outcome.
                fidelity_editor = getattr(self.fidelity_reviewer, "editor", None)
                if getattr(fidelity_editor, "gateway_backed", False):
                    assert fidelity_attempt is not None
                    store.finish_fidelity_gateway_failure(
                        attempt,
                        fidelity_attempt,
                        utc(now).isoformat(),
                        code,
                        self._gateway_diagnostic(fidelity_editor, error),
                    )
                else:
                    store.finish_attempts(
                        ((attempt, None), (fidelity_attempt, code)), utc(now).isoformat()
                    )
                store.mark_selection_editorial(delivery, key, "failed")
                return code
        self._fresh(envelope, now)
        store.save_decision(
            key, revision, request, result, None if reconsider else delivery, utc(now).isoformat()
        )
        if not reconsider:
            store.mark_selection_editorial(delivery, key, "completed")
        if fidelity_attempt is not None:
            store.finish_attempts(((attempt, None), (fidelity_attempt, None)), utc(now).isoformat())
        else:
            store.finish_attempt(attempt, utc(now).isoformat())
        return None

    @staticmethod
    def _editorial_failure_code(error: Exception) -> str:
        """Map untrusted adapter failures to a compact, durable safe taxonomy."""

        code = str(error)
        if code.startswith("editorial_format_retry_exhausted:"):
            kind = code.removeprefix("editorial_format_retry_exhausted:")
            if kind in {"json", "schema", "length"}:
                return code
        if code in {
            "editorial_format_json_invalid",
            "editorial_format_schema_invalid",
            "editorial_format_length_invalid",
            "editorial_safety_rejected",
            "editorial_transport_unavailable",
            "editorial_outcome_unknown",
            "editorial_gateway_child_exit_nonzero",
            "editorial_gateway_timeout",
            "editorial_gateway_response_too_large",
            "editorial_gateway_stream_failure",
            "editorial_gateway_attestation_invalid",
        }:
            return code
        if code.startswith("gateway_format_retry_exhausted:"):
            kind = code.removeprefix("gateway_format_retry_exhausted:")
            if kind in {"json", "schema", "length"}:
                return "editorial_format_retry_exhausted:" + kind
            return "editorial_gateway_response_invalid"
        if code in {"gateway_model_json_invalid", "gateway_empty_response"}:
            return "editorial_format_json_invalid"
        if code.startswith("gateway_creative_schema_invalid:"):
            field = code.rsplit(":", 1)[1]
            return (
                "editorial_format_length_invalid"
                if field in {"headline", "summary", "article", "article_words"}
                else "editorial_format_schema_invalid"
            )
        if code in {
            "gateway_creative_schema_invalid",
            "editorial_schema_invalid",
        } or code.startswith("editorial_schema_invalid:"):
            return "editorial_format_schema_invalid"
        if code == "gateway_creative_safety_invalid":
            return "editorial_safety_rejected"
        safety_reason = code.removeprefix("gateway_creative_safety_")
        if safety_reason in {
            "url",
            "unsupported_date",
            "unattributed_quote",
            "sensitive_sexual_violence",
            "sensitive_minors",
            "sensitive_personal_accusation",
            "sensitive_acute_violence",
        }:
            return "editorial_safety_rejected:" + safety_reason
        if code in {"gateway_unavailable", "gateway_child_launch_failed"}:
            return "editorial_transport_unavailable"
        if code == "gateway_child_exit_nonzero":
            return "editorial_gateway_child_exit_nonzero"
        if code == "gateway_timeout":
            return "editorial_gateway_timeout"
        if code == "gateway_response_too_large":
            return "editorial_gateway_response_too_large"
        if code == "gateway_stream_failure":
            return "editorial_gateway_stream_failure"
        if code == "gateway_outcome_unknown":
            return "editorial_outcome_unknown"
        if code in {
            "editorial_fidelity_unsupported_attribution",
            "editorial_fidelity_contradiction",
            "editorial_fidelity_misleading_timing",
            "editorial_fidelity_proposal_as_outcome",
            "editorial_fidelity_scope_misrepresentation",
            "editorial_fidelity_invented_event",
            "editorial_fidelity_response_invalid",
            "editorial_fidelity_transport_unavailable",
            "editorial_fidelity_gateway_child_exit_nonzero",
            "editorial_fidelity_gateway_timeout",
            "editorial_fidelity_gateway_response_too_large",
            "editorial_fidelity_gateway_stream_failure",
            "editorial_fidelity_gateway_attestation_failure",
            "editorial_fidelity_gateway_outcome_unknown",
            "editorial_fidelity_no_visible_assistant_output",
            "editorial_fidelity_invalid_json",
            "editorial_fidelity_invalid_schema",
        }:
            return code
        if code.startswith("editorial_fidelity_"):
            return "editorial_gateway_response_invalid"
        if code.startswith("gateway_tool_") or code in {
            "gateway_envelope_invalid",
            "gateway_cost_unverified",
            "gateway_model_route_unverified",
        }:
            return "editorial_gateway_attestation_invalid"
        return "editorial_execution_failed"

    @staticmethod
    def _gateway_diagnostic(adapter: Any, error: Exception) -> dict[str, Any]:
        """Return scalar Gateway evidence only; never retain a response or prompt."""

        raw = str(error)
        observed = getattr(adapter, "last_diagnostics", None)
        observed = observed if isinstance(observed, dict) else {}
        category = observed.get("failure_category")
        if not isinstance(category, str) or not category.startswith("gateway_"):
            category = raw if raw.startswith("gateway_") else "gateway_diagnostic_unavailable"
        completion = getattr(adapter, "last_completion", None)
        completion = completion if isinstance(completion, dict) else {}
        terminal = observed.get("terminal_completion_attested") is True
        route = "failed" if raw == "gateway_model_route_unverified" else "unknown"
        cost = "failed" if raw == "gateway_cost_unverified" else "not_checked"
        tools = (
            "failed"
            if raw.startswith("gateway_tool_")
            or raw in {"gateway_tools_exposed", "gateway_tools_used"}
            else "unknown"
        )
        if terminal:
            route = "passed" if route != "failed" else route
            tools = "passed" if tools != "failed" else tools
        return {
            "failure_category": category,
            "child_exit_status": observed.get("child_exit_status"),
            "elapsed_ms": observed.get("elapsed_ms"),
            "terminal_receipt": terminal,
            "route_attestation": route,
            "cost_attestation": cost,
            "tool_attestation": tools,
            "terminal_disposition": completion.get("terminal_disposition"),
        }

    @staticmethod
    def _terminal_rejection(code: str, adapter: Any) -> bool:
        """True only for a known policy result after an attested Gateway reply.

        Transport, timeout, attestation, parse and persistence uncertainty never
        reaches this path.  ``last_completion`` is metadata recorded only by
        the Gateway adapter after it has received and attested one response.
        """

        terminal_invalid_fidelity = code in {
            "editorial_fidelity_invalid_json",
            "editorial_fidelity_invalid_schema",
        }
        known_policy_result = (
            code.startswith("editorial_format_retry_exhausted:")
            or code == "editorial_safety_rejected"
            or code.startswith("editorial_safety_rejected:")
            or code
            in {
                "editorial_fidelity_unsupported_attribution",
                "editorial_fidelity_contradiction",
                "editorial_fidelity_misleading_timing",
                "editorial_fidelity_proposal_as_outcome",
                "editorial_fidelity_scope_misrepresentation",
                "editorial_fidelity_invented_event",
            }
            or terminal_invalid_fidelity
        )
        if not known_policy_result or not getattr(adapter, "gateway_backed", False):
            return False
        completion = getattr(adapter, "last_completion", None)
        if not isinstance(completion, dict) or not completion:
            return False
        if terminal_invalid_fidelity:
            return (
                completion.get("terminal_disposition") == "visible"
                and getattr(adapter, "last_fidelity_output_visible", False) is True
            )
        return True

    @staticmethod
    def _requires_editorial_operator(code: str) -> bool:
        return code != "editorial_execution_failed"

    @classmethod
    def _recovered_editorial_failure(cls, operation: str, status: str, error: Any) -> str:
        """Return a bounded durable error without trusting historic text.

        A crash can happen after an attempt transaction commits but before the
        delivery transaction records ``operator_required``.  Never turn such a
        record into another Gateway call.  Keep only a code that the current
        mapper recognizes exactly; otherwise collapse it to an ambiguous,
        fail-closed outcome.
        """

        fallback = (
            "editorial_fidelity_gateway_outcome_unknown"
            if operation == "fidelity"
            else "editorial_outcome_unknown"
        )
        if status != "failed" or not isinstance(error, str):
            return fallback
        mapped = cls._editorial_failure_code(EditorialError(error))
        return error if mapped == error else fallback

    def _excluded(
        self,
        store: EditorialStore,
        candidate: dict[str, Any],
        revision: int,
        delivery: str,
        now: datetime | None,
    ) -> None:
        verification = store.verification(candidate["candidate_key"], revision)
        if verification is None:
            raise EditorialError("selection_state_invalid")
        request = make_request(candidate, verification, simulation=self.simulation)
        result = {
            "schema_version": 1,
            "candidate_key": candidate["candidate_key"],
            "decision": "REJECTED",
            "source": candidate["url"],
            "date": None,
            "factual_summary": "The candidate was not selected for editorial processing.",
            "satirical_angle": "",
            "headline": "",
            "summary": "",
            "article": "",
            "established_facts": [],
            "unverified_claims": [],
            "open_questions": [],
            "checked_sources": [],
            "verification_evidence": [],
            "risks": ["Editorial selection did not choose this candidate."],
            "rejection_reason": "NO_SATIRICAL_ANGLE",
            "created_at": utc(now).isoformat(),
            "model_identifier": "trusted-selection-v1",
            "prompt_version": "waagd-editorial-v1",
        }
        validate_decision(result, request)
        store.save_decision(
            candidate["candidate_key"], revision, request, result, delivery, utc(now).isoformat()
        )

    def _checked_completion(self, store: EditorialStore, envelope: dict[str, Any]) -> None:
        """An outbox intent never replaces proof that every result still exists."""
        validate_envelope(envelope)
        expected = {c["candidate_key"]: c for c in envelope["candidates"]}
        delivery = envelope["delivery_id"]
        required = store.required(delivery)
        if (
            not expected
            or len(expected) != len(envelope["candidates"])
            or {key for key, _ in required} != set(expected)
        ):
            raise EditorialError("ack_results_incomplete")
        record = store.connection.execute(
            "SELECT payload_digest,payload FROM deliveries WHERE delivery_id=?", (delivery,)
        ).fetchone()
        if (
            record is None
            or record["payload_digest"] != digest(envelope)
            or digest(_stored_envelope(record["payload"])) != digest(envelope)
        ):
            raise EditorialError("ack_results_incomplete")
        outbox = store.connection.execute(
            "SELECT payload_digest,status FROM ack_outbox WHERE delivery_id=?", (delivery,)
        ).fetchone()
        if (
            outbox is None
            or outbox["payload_digest"] != digest(envelope)
            or outbox["status"] not in {"pending", "acknowledged"}
        ):
            raise EditorialError("ack_results_incomplete")
        for key, revision in required:
            candidate_row = store.connection.execute(
                "SELECT source_url,current_revision FROM candidates WHERE candidate_key=?", (key,)
            ).fetchone()
            if (
                candidate_row is None
                or candidate_row["source_url"] != expected[key]["url"]
                or candidate_row["current_revision"] < revision
            ):
                raise EditorialError("ack_results_incomplete")
            row = store.connection.execute(
                "SELECT request,result FROM decisions WHERE candidate_key=? AND revision=?",
                (key, revision),
            ).fetchone()
            if row is None:
                raise EditorialError("ack_results_incomplete")
            try:
                request = io.decode(row["request"].encode())
                result = io.decode(row["result"].encode())
                if (
                    request["candidate"]["candidate_key"] != key
                    or request["candidate"]["url"] != expected[key]["url"]
                    or request["simulation"] is not self.simulation
                    or store.verification(key, revision) != request["verification"]
                ):
                    raise EditorialError("ack_results_incomplete")
                validate_decision(result, request)
            except (EditorialError, io.BridgeError, KeyError, TypeError):
                raise EditorialError("ack_results_incomplete") from None
        if store.connection.execute(
            "SELECT 1 FROM attempts WHERE delivery_id=? AND status='started'", (delivery,)
        ).fetchone():
            raise EditorialError("ack_operations_unfinished")

    def _ack(self, store: EditorialStore, envelope: dict[str, Any], now: datetime | None) -> None:
        if self.simulation or not self.allow_ack:
            return
        delivery = envelope["delivery_id"]
        if delivery in PROTECTED_DELIVERIES:
            raise EditorialError("protected_delivery")
        self._checked_completion(store, envelope)
        # Inspect before checking expiry: an ACK that already committed before
        # expiry must remain reconcilable after a lost response and later restart.
        receipt = self.bridge.inspect_delivery(delivery)
        matching = (
            receipt.get("delivery_id") == delivery
            and receipt.get("payload_digest") == digest(envelope)
            and receipt.get("schema_version") == envelope["bridge_schema_version"]
            and receipt.get("expires_at") == envelope["expires_at"]
        )
        if matching and receipt.get("status") == "acknowledged":
            store.acknowledged(delivery, utc(now).isoformat())
            return
        self._authority(envelope, now, allow_acknowledged=False)
        attempt = store.start_attempt(delivery, None, "ack", utc(now).isoformat())
        try:
            receipt = self.bridge.ack_if_current(delivery, digest(envelope))
        except (EditorialError, io.BridgeError) as error:
            store.finish_attempt(attempt, utc(now).isoformat(), "bridge_ack_failed")
            if str(error) != "bridge_busy":
                raise EditorialError("bridge_ack_requires_operator") from None
            return
        except OSError:
            store.finish_attempt(attempt, utc(now).isoformat(), "bridge_ack_failed")
            return
        if not (
            receipt.get("status") == "acknowledged"
            and receipt.get("delivery_id") == delivery
            and receipt.get("payload_digest") == digest(envelope)
            and receipt.get("schema_version") == envelope["bridge_schema_version"]
            and receipt.get("expires_at") == envelope["expires_at"]
        ):
            store.finish_attempt(attempt, utc(now).isoformat(), "bridge_ack_response_invalid")
            return
        store.acknowledged(delivery, utc(now).isoformat())
        store.finish_attempt(attempt, utc(now).isoformat())

    def _recover(self, store: EditorialStore, now: datetime | None) -> None:
        store.recover_attempts(utc(now).isoformat())
        if not self.allow_ack or self.simulation:
            return
        rows = store.connection.execute(
            "SELECT d.delivery_id,d.payload,d.payload_digest FROM ack_outbox a "
            "JOIN deliveries d ON d.delivery_id=a.delivery_id "
            "WHERE a.status='pending' ORDER BY a.created_at,d.delivery_id"
        ).fetchall()
        for row in rows:
            try:
                envelope = _stored_envelope(row["payload"])
                if (
                    envelope["delivery_id"] != row["delivery_id"]
                    or digest(envelope) != row["payload_digest"]
                ):
                    raise EditorialError("persisted_delivery_invalid")
            except EditorialError:
                store.mark_delivery(
                    row["delivery_id"], "operator_required", "persisted_delivery_invalid"
                )
                raise
            # Payloads were validated at admission and are private durable state.
            # Expired envelopes may ONLY reconcile a matching already-ACKed receipt.
            try:
                self._ack(store, envelope, now)
            except (EditorialError, io.BridgeError, OSError):
                store.mark_delivery(
                    envelope["delivery_id"], "operator_required", "ack_requires_operator"
                )

    def _result(self, store: EditorialStore, delivery: str) -> dict[str, Any]:
        row = store.connection.execute(
            "SELECT status,ack_status,error_code FROM deliveries WHERE delivery_id=?", (delivery,)
        ).fetchone()
        required = store.required(delivery)
        completed = sum(store.has_decision(key, revision) for key, revision in required)
        return {
            "delivery_id": delivery,
            "status": row["status"],
            "ack_status": row["ack_status"],
            "error_code": row["error_code"],
            "completed": completed,
            "total": len(required),
            "simulation": self.simulation,
            "operational": {
                "stage": "editorial",
                "failure_category": row["error_code"] or "none",
                "recovered": 0,
                "quarantined": int(row["status"] == "quarantined"),
                "operator_required": row["status"] == "operator_required",
            },
        }

    @staticmethod
    def _expired_operational(
        *, failure_category: str, recovered: int, quarantined: int, operator_required: bool
    ) -> dict[str, object]:
        return {
            "stage": "editorial",
            "failure_category": failure_category,
            "recovered": recovered,
            "quarantined": quarantined,
            "operator_required": operator_required,
        }

    def _selection_abandon_result(
        self,
        store: EditorialStore,
        delivery: str,
        *,
        recovered: int,
        terminal_status: str,
        terminal_error: str,
    ) -> dict[str, Any]:
        result = self._result(store, delivery)
        result["status"] = terminal_status
        result["error_code"] = terminal_error
        result["operational"] = {
            "stage": "editorial",
            "failure_category": terminal_error,
            "recovered": recovered,
            "quarantined": 0,
            "operator_required": False,
        }
        return result

    def _quarantine_expired(self, envelope: dict[str, Any], now: datetime | None) -> dict[str, Any]:
        """Quarantine only an expired delivery with no possible external work.

        The Bridge ledger owns release of the pending batch.  The Editorial
        Store first records a crash-recoverable reservation and rejects any
        hint of a Gateway, ACK, publication, or Site submission.  A failed
        Bridge commit is reconciled only by its durable receipt; otherwise the
        delivery remains operator-required rather than being replayed.
        """

        delivery = envelope["delivery_id"]
        expected = digest(envelope)
        try:
            receipt = self.bridge.inspect_delivery(delivery)
            if (
                receipt.get("delivery_id") != delivery
                or receipt.get("payload_digest") != expected
                or receipt.get("schema_version") != envelope["bridge_schema_version"]
                or receipt.get("expires_at") != envelope["expires_at"]
                or receipt.get("status") not in {"pending", "dismissed"}
            ):
                raise EditorialError("bridge_authority_mismatch")
        except (EditorialError, io.BridgeError, OSError, KeyError, TypeError):
            return {
                "status": "operator_required",
                "error_code": "delivery_expired_authority_unknown",
                "simulation": self.simulation,
                "operational": self._expired_operational(
                    failure_category="delivery_expired_authority_unknown",
                    recovered=0,
                    quarantined=0,
                    operator_required=True,
                ),
            }
        if receipt["status"] == "pending" and not callable(
            getattr(self.bridge, "quarantine_expired", None)
        ):
            return {
                "status": "operator_required",
                "error_code": "delivery_expired_authority_unknown",
                "simulation": self.simulation,
                "operational": self._expired_operational(
                    failure_category="delivery_expired_authority_unknown",
                    recovered=0,
                    quarantined=0,
                    operator_required=True,
                ),
            }
        try:
            with self._store() as store:
                if receipt["status"] == "dismissed":
                    store.finish_expired_quarantine(delivery)
                    result = self._result(store, delivery)
                    result["status"] = "quarantined"
                    result["operational"] = self._expired_operational(
                        failure_category="delivery_expired_quarantined",
                        recovered=1,
                        quarantined=1,
                        operator_required=False,
                    )
                    return result
                if not store.begin_expired_quarantine(envelope, utc(now).isoformat()):
                    row = self._result(store, delivery)
                    row["status"] = "operator_required"
                    row["error_code"] = "delivery_expired_requires_operator"
                    row["operational"] = self._expired_operational(
                        failure_category="delivery_expired_requires_operator",
                        recovered=0,
                        quarantined=0,
                        operator_required=True,
                    )
                    return row
                try:
                    quarantined = self.bridge.quarantine_expired(delivery, expected, now)
                    if (
                        quarantined.get("delivery_id") != delivery
                        or quarantined.get("payload_digest") != expected
                        or quarantined.get("status") != "dismissed"
                    ):
                        raise EditorialError("bridge_quarantine_invalid")
                except (
                    AttributeError,
                    EditorialError,
                    io.BridgeError,
                    OSError,
                    KeyError,
                    TypeError,
                ):
                    try:
                        reconciled = self.bridge.inspect_delivery(delivery)
                    except (io.BridgeError, OSError, KeyError, TypeError):
                        reconciled = {}
                    if (
                        reconciled.get("delivery_id") != delivery
                        or reconciled.get("payload_digest") != expected
                        or reconciled.get("status") != "dismissed"
                    ):
                        store.fail_expired_quarantine(delivery)
                        return self._result(store, delivery)
                store.finish_expired_quarantine(delivery)
                result = self._result(store, delivery)
                result["status"] = "quarantined"
                result["operational"] = self._expired_operational(
                    failure_category="delivery_expired_quarantined",
                    recovered=1,
                    quarantined=1,
                    operator_required=False,
                )
                return result
        except (EditorialError, io.BridgeError, OSError, sqlite3.Error, TypeError):
            return {
                "status": "operator_required",
                "error_code": "delivery_expired_quarantine_uncertain",
                "simulation": self.simulation,
                "operational": self._expired_operational(
                    failure_category="delivery_expired_quarantine_uncertain",
                    recovered=0,
                    quarantined=0,
                    operator_required=True,
                ),
            }

    def process(self, now: datetime | None = None) -> dict[str, Any]:
        utc(now)
        try:
            envelope = self._read()
        except FileNotFoundError:
            # A persisted outbox can reconcile independently of a removed inbox.
            if not self._has_state():
                return {"status": "inbox_missing", "simulation": self.simulation}
            with self._store() as store:
                self._recover(store, now)
                return {**store.status(), "status": "inbox_missing"}
        if envelope.get("delivery_id") is None and envelope.get("candidates") == []:
            validate_envelope(envelope)
            if not self._has_state():
                validate_input(envelope, utc(now))
            with self._store() as store:
                self._recover(store, now)
                return {**store.status(), "status": "inbox_empty"}
        try:
            validate_input(envelope, utc(now))
        except EditorialError as error:
            if str(error) == "delivery_expired":
                return self._quarantine_expired(envelope, now)
            raise
        authority = self._authority(envelope, now)
        if authority["status"] == "acknowledged":
            # An authoritative ACK is not proof that this independent store owns
            # the previous editorial results. Never regenerate a lost history.
            if not self._has_state():
                return {
                    "status": "operator_required",
                    "error_code": "editorial_history_unavailable",
                    "delivery_id": envelope["delivery_id"],
                    "simulation": self.simulation,
                }
            with self._store() as store:
                store.recover_attempts(utc(now).isoformat())
                try:
                    self._checked_completion(store, envelope)
                    if not self.simulation:
                        store.acknowledged(envelope["delivery_id"], utc(now).isoformat())
                except (EditorialError, io.BridgeError, KeyError, TypeError):
                    return {
                        "status": "operator_required",
                        "error_code": "editorial_history_unavailable",
                        "delivery_id": envelope["delivery_id"],
                        "simulation": self.simulation,
                    }
                return self._result(store, envelope["delivery_id"])
        with self._store() as store:
            self._recover(store, now)
            store.register(envelope, utc(now).isoformat())
            existing = store.connection.execute(
                "SELECT status,error_code FROM deliveries WHERE delivery_id=?",
                (envelope["delivery_id"],),
            ).fetchone()
            unresolved_editorial = store.connection.execute(
                "SELECT a.operation,a.status,a.error_code FROM attempts a "
                "JOIN delivery_candidates dc ON "
                "dc.delivery_id=a.delivery_id AND dc.candidate_key=a.candidate_key "
                "LEFT JOIN decisions d ON d.candidate_key=dc.candidate_key "
                "AND d.revision=dc.revision WHERE a.delivery_id=? "
                "AND a.operation IN ('editorial','editorial_gateway','fidelity') "
                "AND a.status IN ('interrupted','failed') AND d.candidate_key IS NULL "
                "ORDER BY CASE a.operation WHEN 'fidelity' THEN 0 ELSE 1 END, a.started_at DESC ",
                (envelope["delivery_id"],),
            ).fetchall()
            for unresolved in unresolved_editorial:
                code = self._recovered_editorial_failure(
                    unresolved["operation"], unresolved["status"], unresolved["error_code"]
                )
                gateway_turn = unresolved["operation"] in {"editorial_gateway", "fidelity"}
                known_terminal_failure = unresolved[
                    "status"
                ] == "failed" and self._requires_editorial_operator(code)
                if gateway_turn or known_terminal_failure:
                    store.mark_delivery(envelope["delivery_id"], "operator_required", code)
                    return self._result(store, envelope["delivery_id"])
            if (
                existing is not None
                and existing["status"] == "operator_required"
                and isinstance(existing["error_code"], str)
                and existing["error_code"].startswith("editorial_")
            ):
                # Selection has its own explicit recovery operation. Editorial
                # failures must remain stopped until an operator resolves them.
                return self._result(store, envelope["delivery_id"])
            if store.connection.execute(
                "SELECT 1 FROM ack_outbox WHERE delivery_id=?", (envelope["delivery_id"],)
            ).fetchone():
                # Never silently regenerate a result lost after an outbox commit.
                self._checked_completion(store, envelope)
            candidates = {c["candidate_key"]: c for c in envelope["candidates"]}
            required = store.required(envelope["delivery_id"])
            errors = []
            if self.selector is not None:
                # Verify every candidate first. Only trusted-publisher candidates
                # may enter selection.
                for key, revision in required:
                    self._candidate(
                        store, envelope, candidates[key], revision, now, verification_only=True
                    )
                from waagd_discovery.editorial.selection import shortlist_batches

                run = store.selection(envelope["delivery_id"])
                if run is None:
                    eligible = []
                    canonical_urls: set[str] = set()
                    for key, revision in required:
                        if store.has_decision(key, revision):
                            continue
                        verification = store.verification(key, revision)
                        if verification is None:
                            raise EditorialError("selection_state_invalid")
                        if verification["verification_status"] != "TRUSTED_PUBLISHER":
                            continue
                        value = dict(candidates[key])
                        value["verification_status"] = verification["verification_status"]
                        canonical = verification["canonical_url"]
                        if canonical in canonical_urls:
                            value["selection_exclusion"] = "duplicate_canonical_url"
                        else:
                            canonical_urls.add(canonical)
                        eligible.append(value)
                    batches = shortlist_batches(eligible)
                    run = store.start_selection(
                        envelope["delivery_id"],
                        utc(now).date().isoformat(),
                        [item for batch in batches for item in batch.shortlist],
                        batches[0].rejected,
                        utc(now).isoformat(),
                    )
                if run["status"] == "completed":
                    selected = set(io.decode(run["selected"].encode()))
                elif run["status"] == "in_flight":
                    store.mark_delivery(
                        envelope["delivery_id"], "operator_required", "selection_outcome_unknown"
                    )
                    return self._result(store, envelope["delivery_id"])
                elif run["status"] == "started":
                    from waagd_discovery.editorial.selection import SelectionResult

                    saved = io.decode(run["shortlist"].encode())
                    if not isinstance(saved, list) or not all(
                        isinstance(item, dict) for item in saved
                    ):
                        raise EditorialError("selection_state_invalid")
                    batches = [
                        SelectionResult(saved[index : index + 25], [], {})
                        for index in range(0, len(saved), 25)
                    ]
                    available = max(
                        0,
                        5
                        - store.selected_today(
                            utc(now).date().isoformat(),
                            excluding_delivery=envelope["delivery_id"],
                        ),
                    )
                    if not any(batch.shortlist for batch in batches) or not available:
                        selected = set()
                        store.finish_selection(
                            envelope["delivery_id"],
                            sorted(selected),
                            utc(now).isoformat(),
                            reason_code=(
                                "daily_quota_exhausted"
                                if not available
                                else "no_eligible_candidates"
                            ),
                        )
                    else:
                        selection_attempt = None
                        selection_received = False
                        try:
                            selection_attempt = store.start_attempt(
                                envelope["delivery_id"], None, "selection", utc(now).isoformat()
                            )
                            store.begin_selection_call(envelope["delivery_id"])
                            chosen: list[str] = []
                            for batch in batches:
                                if len(chosen) == available:
                                    break
                                choice = self.selector.select(batch)
                                chosen.extend(choice.selected[: available - len(chosen)])
                            selected = set(chosen)
                            selection_received = True
                            store.finish_selection(
                                envelope["delivery_id"],
                                sorted(selected),
                                utc(now).isoformat(),
                            )
                            store.finish_attempt(selection_attempt, utc(now).isoformat())
                        except EditorialError as error:
                            code = str(error)
                            if selection_received:
                                code = "selection_persistence_failed"
                            if selection_attempt is not None:
                                store.finish_attempt(selection_attempt, utc(now).isoformat(), code)
                            if code == "selection_outcome_unknown" or selection_received:
                                # A subprocess failure can occur after Gateway
                                # accepted a selection turn.  A SQLite failure
                                # after a model response is equally ambiguous:
                                # keep in_flight so restart never turns either
                                # case into a new model choice.
                                store.mark_delivery(
                                    envelope["delivery_id"], "operator_required", code
                                )
                                return self._result(store, envelope["delivery_id"])
                            store.finish_selection(
                                envelope["delivery_id"],
                                [],
                                utc(now).isoformat(),
                                code,
                            )
                            store.mark_delivery(envelope["delivery_id"], "operator_required", code)
                            return self._result(store, envelope["delivery_id"])
                else:
                    return self._result(store, envelope["delivery_id"])
                for key, revision in required:
                    verification = store.verification(key, revision)
                    if (
                        verification is not None
                        and verification["verification_status"] == "TRUSTED_PUBLISHER"
                        and key not in selected
                        and not store.has_decision(key, revision)
                    ):
                        self._excluded(
                            store, candidates[key], revision, envelope["delivery_id"], now
                        )
            for key, revision in required:
                if self.selector is not None and store.has_decision(key, revision):
                    continue
                try:
                    candidate_error = self._candidate(
                        store, envelope, candidates[key], revision, now
                    )
                    if candidate_error:
                        errors.append(candidate_error)
                except EditorialError as failure:
                    if str(failure) == "delivery_expired":
                        store.mark_delivery(
                            envelope["delivery_id"], "operator_required", "delivery_expired"
                        )
                        return self._result(store, envelope["delivery_id"])
                    raise
            if errors:
                # Gateway/editorial failures are never replayed through the
                # normal delivery loop: a completed turn may have been lost,
                # and an exhausted format retry must not become an unbounded
                # source of new model decisions. The per-candidate attempt
                # rows retain the exact safe code for operator review.
                operator_code = next(
                    (code for code in errors if self._requires_editorial_operator(code)), None
                )
                if operator_code is not None:
                    store.mark_delivery(envelope["delivery_id"], "operator_required", operator_code)
                else:
                    store.mark_delivery(
                        envelope["delivery_id"], "retryable", "editorial_execution_failed"
                    )
            else:
                store.complete_reused(envelope["delivery_id"], utc(now).isoformat())
                try:
                    self._ack(store, envelope, now)
                except (EditorialError, io.BridgeError, OSError):
                    store.mark_delivery(
                        envelope["delivery_id"], "operator_required", "ack_requires_operator"
                    )
            return self._result(store, envelope["delivery_id"])

    def status(self) -> dict[str, Any]:
        if not self._has_state():
            return {"status": "uninitialized", "schema_version": 1, "simulation": self.simulation}
        with self._store(readonly=True) as store:
            return store.status()

    def retry_selection(self, delivery: str, now: datetime | None = None) -> dict[str, Any]:
        """Explicitly retry one known Gateway selection failure.

        This is intentionally unavailable for an ``in_flight`` selection:
        a lost subprocess result may conceal a Gateway turn, which is never
        safe to replay automatically or through this convenience operation.
        """

        envelope = self._read()
        validate_input(envelope, utc(now))
        if envelope["delivery_id"] != delivery:
            raise EditorialError("selection_retry_delivery_mismatch")
        self._authority(envelope, now, allow_acknowledged=False)
        with self._store() as store:
            store.recover_attempts(utc(now).isoformat())
            run = store.selection(delivery)
            if run is None:
                raise EditorialError("selection_retry_not_allowed")
            if run["status"] == "in_flight":
                return {
                    **self._result(store, delivery),
                    "status": "operator_required",
                    "error_code": "selection_outcome_unknown",
                }
            store.reopen_failed_selection(delivery)
            # This is an explicit recovery only for a selection call proven
            # not to be in flight. Let process resume that durable run.
            store.mark_delivery(delivery, "processing", None)
        return self.process(now)

    def _abandon_terminal_selection(
        self,
        delivery: str,
        *,
        operator_confirm: bool,
        now: datetime | None,
        selection_error: str,
        terminal_status: str,
        terminal_error: str,
    ) -> dict[str, Any]:
        """Explicitly abandon one expired, terminal Gateway selection failure.

        This is deliberately distinct from selection retry and expiry quarantine.
        It neither invokes a Gateway adapter nor creates an ACK; it only releases
        the Bridge receipt after the Store proves the exact in-flight selection
        state and the absence of all downstream side effects.
        """

        if operator_confirm is not True:
            raise EditorialError("selection_abandon_confirmation_required")
        clock = utc(now)
        with self._store(readonly=True) as store:
            persisted = store.persisted_delivery(delivery)
        if persisted is None:
            raise EditorialError("selection_abandon_delivery_mismatch")
        persisted_digest, raw_persisted = persisted
        envelope = _stored_envelope(raw_persisted)
        validate_input(envelope, clock, allow_expired=True)
        if envelope["delivery_id"] != delivery or digest(envelope) != persisted_digest:
            raise EditorialError("persisted_delivery_invalid")
        live_inbox = False
        try:
            current = self._read()
        except FileNotFoundError:
            current = None
        if isinstance(current, dict) and current.get("delivery_id") is not None:
            validate_input(current, clock, allow_expired=True)
            if (
                current.get("delivery_id") != delivery
                or digest(current) != persisted_digest
                or current.get("bridge_schema_version") != envelope["bridge_schema_version"]
                or current.get("expires_at") != envelope["expires_at"]
            ):
                return {
                    "delivery_id": delivery,
                    "status": "operator_required",
                    "error_code": "selection_abandon_inbox_mismatch",
                    "simulation": self.simulation,
                }
            live_inbox = True
        if timestamp(envelope["expires_at"]) > clock:
            raise EditorialError("selection_abandon_requires_expiry")
        expected = digest(envelope)
        try:
            receipt = self.bridge.inspect_delivery(delivery)
            if (
                receipt.get("delivery_id") != delivery
                or receipt.get("payload_digest") != expected
                or receipt.get("schema_version") != envelope["bridge_schema_version"]
                or receipt.get("expires_at") != envelope["expires_at"]
                or receipt.get("status") not in {"pending", "dismissed"}
            ):
                raise EditorialError("bridge_authority_mismatch")
            if receipt["status"] == "pending" and not live_inbox:
                raise EditorialError("selection_abandon_inbox_unavailable")
            if receipt["status"] == "pending" and not callable(
                getattr(self.bridge, "abandon_ambiguous_selection", None)
            ):
                raise EditorialError("bridge_authority_mismatch")
        except (EditorialError, io.BridgeError, OSError, KeyError, TypeError):
            return {
                "delivery_id": delivery,
                "status": "operator_required",
                "error_code": "selection_abandon_authority_unknown",
                "simulation": self.simulation,
                "operational": {
                    "stage": "editorial",
                    "failure_category": "selection_abandon_authority_unknown",
                    "recovered": 0,
                    "quarantined": 0,
                    "operator_required": True,
                },
            }
        try:
            with self._store() as store:
                if selection_error == "selection_outcome_unknown":
                    reserved = store.begin_selection_abandon(envelope, clock.isoformat())
                    finish = store.finish_selection_abandon
                    fail = store.fail_selection_abandon
                else:
                    reserved = store.begin_selection_attestation_abandon(
                        envelope, clock.isoformat()
                    )
                    finish = store.finish_selection_attestation_abandon
                    fail = store.fail_selection_attestation_abandon
                if not reserved:
                    result = self._result(store, delivery)
                    if result["status"] == terminal_status and receipt["status"] == "dismissed":
                        return self._selection_abandon_result(
                            store,
                            delivery,
                            recovered=1,
                            terminal_status=terminal_status,
                            terminal_error=terminal_error,
                        )
                    result["status"] = "operator_required"
                    return result
                if receipt["status"] == "dismissed":
                    finish(delivery)
                    return self._selection_abandon_result(
                        store,
                        delivery,
                        recovered=1,
                        terminal_status=terminal_status,
                        terminal_error=terminal_error,
                    )
                try:
                    abandoned = self.bridge.abandon_ambiguous_selection(delivery, expected, clock)
                    if (
                        abandoned.get("delivery_id") != delivery
                        or abandoned.get("payload_digest") != expected
                        or abandoned.get("schema_version") != envelope["bridge_schema_version"]
                        or abandoned.get("expires_at") != envelope["expires_at"]
                        or abandoned.get("status") != "dismissed"
                    ):
                        raise EditorialError("bridge_selection_abandon_invalid")
                except (
                    AttributeError,
                    EditorialError,
                    io.BridgeError,
                    OSError,
                    KeyError,
                    TypeError,
                ):
                    try:
                        reconciled = self.bridge.inspect_delivery(delivery)
                    except (io.BridgeError, OSError, KeyError, TypeError):
                        reconciled = {}
                    if (
                        reconciled.get("delivery_id") != delivery
                        or reconciled.get("payload_digest") != expected
                        or reconciled.get("schema_version") != envelope["bridge_schema_version"]
                        or reconciled.get("expires_at") != envelope["expires_at"]
                        or reconciled.get("status") != "dismissed"
                    ):
                        fail(delivery)
                        return self._result(store, delivery)
                finish(delivery)
                return self._selection_abandon_result(
                    store,
                    delivery,
                    recovered=1,
                    terminal_status=terminal_status,
                    terminal_error=terminal_error,
                )
        except (EditorialError, io.BridgeError, OSError, sqlite3.Error, TypeError):
            return {
                "delivery_id": delivery,
                "status": "operator_required",
                "error_code": "selection_abandon_reconciliation_uncertain",
                "simulation": self.simulation,
                "operational": {
                    "stage": "editorial",
                    "failure_category": "selection_abandon_reconciliation_uncertain",
                    "recovered": 0,
                    "quarantined": 0,
                    "operator_required": True,
                },
            }

    def abandon_ambiguous_selection(
        self, delivery: str, *, operator_confirm: bool = False, now: datetime | None = None
    ) -> dict[str, Any]:
        return self._abandon_terminal_selection(
            delivery,
            operator_confirm=operator_confirm,
            now=now,
            selection_error="selection_outcome_unknown",
            terminal_status="selection_abandoned",
            terminal_error="selection_outcome_unknown_abandoned",
        )

    def abandon_selection_attestation(
        self, delivery: str, *, operator_confirm: bool = False, now: datetime | None = None
    ) -> dict[str, Any]:
        """Explicitly abandon one expired terminal selection-attestation failure."""

        return self._abandon_terminal_selection(
            delivery,
            operator_confirm=operator_confirm,
            now=now,
            selection_error="selection_attestation_missing",
            terminal_status="selection_attestation_abandoned",
            terminal_error="selection_attestation_missing_abandoned",
        )

    def abandon_partial_editorial_batch(
        self, delivery: str, *, operator_confirm: bool = False, now: datetime | None = None
    ) -> dict[str, Any]:
        """Terminally dismiss one expired mixed-result Gateway batch without publication."""

        if operator_confirm is not True:
            raise EditorialError("partial_editorial_abandon_confirmation_required")
        clock = utc(now)
        with self._store(readonly=True) as store:
            persisted = store.persisted_delivery(delivery)
        if persisted is None:
            raise EditorialError("partial_editorial_abandon_delivery_mismatch")
        persisted_digest, raw_persisted = persisted
        envelope = _stored_envelope(raw_persisted)
        validate_input(envelope, clock, allow_expired=True)
        if envelope["delivery_id"] != delivery or digest(envelope) != persisted_digest:
            raise EditorialError("persisted_delivery_invalid")
        try:
            current = self._read()
        except FileNotFoundError:
            current = None
        live_inbox = False
        if isinstance(current, dict) and current.get("delivery_id") is not None:
            validate_input(current, clock, allow_expired=True)
            if (
                current.get("delivery_id") != delivery
                or digest(current) != persisted_digest
                or current.get("bridge_schema_version") != envelope["bridge_schema_version"]
                or current.get("expires_at") != envelope["expires_at"]
            ):
                return {
                    "delivery_id": delivery,
                    "status": "operator_required",
                    "error_code": "partial_editorial_abandon_inbox_mismatch",
                    "simulation": self.simulation,
                }
            live_inbox = True
        if timestamp(envelope["expires_at"]) > clock:
            raise EditorialError("partial_editorial_abandon_requires_expiry")
        expected = digest(envelope)
        try:
            receipt = self.bridge.inspect_delivery(delivery)
            if (
                receipt.get("delivery_id") != delivery
                or receipt.get("payload_digest") != expected
                or receipt.get("schema_version") != envelope["bridge_schema_version"]
                or receipt.get("expires_at") != envelope["expires_at"]
                or receipt.get("status") not in {"pending", "dismissed"}
                or (receipt.get("status") == "pending" and not live_inbox)
                or (
                    receipt.get("status") == "pending"
                    and not callable(getattr(self.bridge, "abandon_ambiguous_selection", None))
                )
            ):
                raise EditorialError("bridge_authority_mismatch")
        except (EditorialError, io.BridgeError, OSError, KeyError, TypeError):
            return {
                "delivery_id": delivery,
                "status": "operator_required",
                "error_code": "partial_editorial_abandon_authority_unknown",
                "simulation": self.simulation,
            }
        try:
            with self._store() as store:
                reserved = store.begin_partial_editorial_abandon(envelope, clock.isoformat())
                if not reserved:
                    result = self._result(store, delivery)
                    if (
                        result["status"] == "partial_editorial_batch_abandoned"
                        and receipt["status"] == "dismissed"
                    ):
                        return self._selection_abandon_result(
                            store,
                            delivery,
                            recovered=1,
                            terminal_status="partial_editorial_batch_abandoned",
                            terminal_error="partial_editorial_batch_abandoned",
                        )
                    result["status"] = "operator_required"
                    return result
                if receipt["status"] == "pending":
                    try:
                        receipt = self.bridge.abandon_ambiguous_selection(delivery, expected, clock)
                    except (EditorialError, io.BridgeError, OSError, KeyError, TypeError):
                        receipt = self.bridge.inspect_delivery(delivery)
                if (
                    receipt.get("delivery_id") != delivery
                    or receipt.get("payload_digest") != expected
                    or receipt.get("schema_version") != envelope["bridge_schema_version"]
                    or receipt.get("expires_at") != envelope["expires_at"]
                    or receipt.get("status") != "dismissed"
                ):
                    store.fail_partial_editorial_abandon(delivery)
                    return self._result(store, delivery)
                store.finish_partial_editorial_abandon(delivery)
                return self._selection_abandon_result(
                    store,
                    delivery,
                    recovered=1,
                    terminal_status="partial_editorial_batch_abandoned",
                    terminal_error="partial_editorial_batch_abandoned",
                )
        except (EditorialError, io.BridgeError, OSError, sqlite3.Error, TypeError):
            return {
                "delivery_id": delivery,
                "status": "operator_required",
                "error_code": "partial_editorial_abandon_reconciliation_uncertain",
                "simulation": self.simulation,
            }

    def reconsider(self, candidate_key: str, now: datetime | None = None) -> dict[str, Any]:
        if len(candidate_key) != 64 or any(c not in "0123456789abcdef" for c in candidate_key):
            raise EditorialError("candidate_key_invalid")
        with self._store() as store:
            store.recover_attempts(utc(now).isoformat())
            context = store.connection.execute(
                "SELECT d.payload FROM deliveries d JOIN delivery_candidates dc ON "
                "dc.delivery_id=d.delivery_id WHERE dc.candidate_key=? "
                "ORDER BY d.received_at DESC LIMIT 1",
                (candidate_key,),
            ).fetchone()
            if context is None:
                raise EditorialError("candidate_not_processed")
            envelope = _stored_envelope(context[0])
            if envelope["delivery_id"] in PROTECTED_DELIVERIES:
                raise EditorialError("protected_delivery")
            result = store.reconsider(candidate_key, utc(now).isoformat())
            try:
                validate_input(envelope, utc(now))
                self._authority(envelope, now)
            except (EditorialError, io.BridgeError, OSError):
                return {
                    **result,
                    "status": "operator_required",
                    "error_code": "reconsideration_requires_operator",
                }
            candidate = next(
                c for c in envelope["candidates"] if c["candidate_key"] == candidate_key
            )
            error = self._candidate(
                store, envelope, candidate, result["revision"], now, reconsider=True
            )
            return {**result, "status": "retryable" if error else "completed", "error_code": error}
