"""Agent-independent editorial orchestration with a durable post-result ACK outbox."""

import os
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
    digest,
    make_request,
    utc,
    validate_decision,
    validate_input,
    validate_verification,
)
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
        attempt = store.start_attempt(delivery, key, "editorial", utc(now).isoformat())
        try:
            result = self.editor.evaluate(request)
            validate_decision(result, request)
        except (EditorialError, io.BridgeError, OSError, ValueError, TypeError):
            store.finish_attempt(attempt, utc(now).isoformat(), "editorial_failed")
            store.mark_selection_editorial(delivery, key, "failed")
            return "editorial_failed"
        self._fresh(envelope, now)
        store.save_decision(
            key, revision, request, result, None if reconsider else delivery, utc(now).isoformat()
        )
        if not reconsider:
            store.mark_selection_editorial(delivery, key, "completed")
        store.finish_attempt(attempt, utc(now).isoformat())
        return None

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
                if self._has_state():
                    with self._store() as store:
                        self._recover(store, now)
                        if store.connection.execute(
                            "SELECT 1 FROM deliveries WHERE delivery_id=?",
                            (envelope["delivery_id"],),
                        ).fetchone():
                            if (
                                store.connection.execute(
                                    "SELECT ack_status FROM deliveries WHERE delivery_id=?",
                                    (envelope["delivery_id"],),
                                ).fetchone()[0]
                                != "acknowledged"
                            ):
                                store.mark_delivery(
                                    envelope["delivery_id"], "operator_required", "delivery_expired"
                                )
                            return self._result(store, envelope["delivery_id"])
                return {
                    "status": "operator_required",
                    "error_code": "delivery_expired",
                    "simulation": self.simulation,
                }
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
                        try:
                            store.begin_selection_call(envelope["delivery_id"])
                            chosen: list[str] = []
                            for batch in batches:
                                if len(chosen) == available:
                                    break
                                choice = self.selector.select(batch)
                                chosen.extend(choice.selected[: available - len(chosen)])
                            selected = set(chosen)
                            store.finish_selection(
                                envelope["delivery_id"],
                                sorted(selected),
                                utc(now).isoformat(),
                            )
                        except EditorialError:
                            store.finish_selection(
                                envelope["delivery_id"],
                                [],
                                utc(now).isoformat(),
                                "selection_failed",
                            )
                            store.mark_delivery(
                                envelope["delivery_id"], "retryable", "selection_failed"
                            )
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
                store.mark_delivery(envelope["delivery_id"], "retryable", "editorial_failed")
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
