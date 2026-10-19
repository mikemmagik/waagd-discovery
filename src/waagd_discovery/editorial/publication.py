"""Data-only producer for the site PublicationIntent v1 core envelope."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import (
    EditorialError,
    validate_decision,
    validate_verification,
)
from waagd_discovery.editorial.publication_policy import PublicationPolicy
from waagd_discovery.editorial.store import EditorialStore

_MAXIMUM = 256 * 1024
_LIMIT = 100
_NAME = "publication-intent.json"


def _json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise EditorialError("publication_payload_invalid") from None


def _timestamp(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (AttributeError, TypeError, ValueError):
        raise EditorialError("publication_payload_invalid") from None


def _intent_id(key: str, revision: int) -> str:
    return f"auto-{key}-r{revision}"


def _source_item_id(intent_id: str) -> str:
    return "waagd:" + hashlib.sha256(intent_id.encode()).hexdigest()


def _text(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _content_hash(intent: dict[str, Any]) -> str:
    # This field order exactly follows site core/intent.ts hashDocument().
    post = intent["post"]
    document = {
        "version": intent["version"],
        "intent_id": intent["intent_id"],
        "origin": intent["origin"],
        "authorization": {
            "kind": intent["authorization"]["kind"],
            "approved": intent["authorization"]["approved"],
        },
        "editorial": {
            "decision_id": intent["editorial"]["decision_id"],
            "source_event_id": intent["editorial"]["source_event_id"],
            "source_attribution": intent["editorial"]["source_attribution"],
            "approved_at": intent["editorial"]["approved_at"],
            "publication_policy": {
                "version": intent["editorial"]["publication_policy"]["version"],
                "outcome": intent["editorial"]["publication_policy"]["outcome"],
            },
        },
        "post": {
            key: post[key]
            for key in (
                "type",
                "url",
                "canonical_url",
                "source_item_id",
                "title",
                "source",
                "origin",
                "preview",
                "embed",
                "body",
                "references",
            )
        },
    }
    return hashlib.sha256(_json(document)).hexdigest()


def _payload(
    candidate: dict[str, Any],
    verification: dict[str, Any],
    decision: dict[str, Any],
    delivery_id: str,
    revision: int,
    policy: PublicationPolicy,
) -> dict[str, Any]:
    evidence = {item["id"]: item for item in verification["evidence"]}
    ids = decision["verification_evidence"]
    publisher = candidate.get("publisher") or verification.get("publisher")
    published = _timestamp(candidate["published_at"])
    if (
        decision["decision"] != "READY_FOR_REVIEW"
        or verification["verification_status"] != "VERIFIED"
        or decision["unverified_claims"]
        or decision["open_questions"]
        or decision["risks"]
        or not ids
        or any(item not in evidence for item in ids)
        or not _text(publisher, 300)
        or not policy.permits(published)
    ):
        raise EditorialError("publication_not_eligible")
    key = candidate["candidate_key"]
    intent_id = _intent_id(key, revision)
    post = {
        "type": "news",
        "origin": "auto",
        "url": candidate["url"],
        "canonical_url": candidate["url"],
        "source_item_id": _source_item_id(intent_id),
        "title": decision["headline"],
        "source": "WAAGD",
        "preview": None,
        "embed": None,
        "body": decision["article"],
        "references": [
            {"url": evidence[item]["url"], "title": "Verified evidence"} for item in ids
        ],
    }
    if (
        not _text(post["title"], 300)
        or not _text(post["body"], 100000)
        or not _text(post["source_item_id"], 256)
        or len(post["references"]) > 100
        or len(_json(post)) > _MAXIMUM
    ):
        raise EditorialError("publication_payload_invalid")
    intent = {
        "version": 1,
        "intent_id": intent_id,
        "origin": "auto",
        "authorization": {"kind": "discovery", "approved": True},
        "editorial": {
            "decision_id": f"decision-{key}-r{revision}",
            "source_event_id": f"candidate:{key};delivery:{delivery_id}",
            "source_attribution": publisher,
            "approved_at": _timestamp(decision["created_at"]),
            "publication_policy": {"version": policy.version, "outcome": "approved"},
        },
        "post": post,
    }
    intent["content_hash"] = _content_hash(intent)
    return intent


def exportable(
    store: EditorialStore, limit: int = _LIMIT, *, policy: PublicationPolicy | None = None
) -> list[dict[str, Any]]:
    if store.simulation:
        raise EditorialError("publication_simulation_forbidden")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _LIMIT:
        raise EditorialError("publication_limit_invalid")
    policy = policy or PublicationPolicy()
    query = (
        "SELECT c.candidate_key,c.current_revision,d.request,d.result decision_result,"
        "v.result verification_result,dc.delivery_id FROM candidates c "
        "JOIN decisions d ON d.candidate_key=c.candidate_key AND d.revision=c.current_revision "
        "JOIN verifications v ON v.candidate_key=c.candidate_key AND v.revision=c.current_revision "
        "JOIN (SELECT candidate_key,revision,MIN(delivery_id) delivery_id "
        "FROM delivery_candidates GROUP BY candidate_key,revision) dc "
        "ON dc.candidate_key=c.candidate_key AND dc.revision=c.current_revision "
        "WHERE c.current_revision>0 ORDER BY c.candidate_key LIMIT ?"
    )
    rows = store.connection.execute(query, (limit,)).fetchall()
    result = []
    for row in rows:
        try:
            request = io.decode(row["request"].encode())
            decision = io.decode(row["decision_result"].encode())
            verification = io.decode(row["verification_result"].encode())
            candidate = request["candidate"]
            validate_verification(verification, candidate["candidate_key"])
            validate_decision(decision, request)
            result.append(
                _payload(
                    candidate,
                    verification,
                    decision,
                    row["delivery_id"],
                    row["current_revision"],
                    policy,
                )
            )
        except EditorialError as error:
            if str(error) == "publication_not_eligible":
                continue
            raise EditorialError("publication_store_invalid") from None
        except (io.BridgeError, KeyError, TypeError, ValueError):
            raise EditorialError("publication_store_invalid") from None
    return result


def validate_publication_intent(value: Any) -> None:
    try:
        if not isinstance(value, dict) or set(value) != {
            "version",
            "intent_id",
            "content_hash",
            "origin",
            "authorization",
            "editorial",
            "post",
        }:
            raise ValueError
        if (
            value["version"] != 1
            or value["origin"] != "auto"
            or value["authorization"] != {"kind": "discovery", "approved": True}
        ):
            raise ValueError
        if (
            not isinstance(value["intent_id"], str)
            or not value["intent_id"].startswith("auto-")
            or value["content_hash"] != _content_hash(value)
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, EditorialError):
        raise EditorialError("publication_schema_invalid") from None


def export_file(
    store_path: Path, output: Path, limit: int = 1, *, policy: PublicationPolicy | None = None
) -> dict[str, Any]:
    io.path_parts(store_path)
    io.path_parts(output)
    if output.name != _NAME:
        raise EditorialError("publication_output_invalid")
    try:
        with EditorialStore(store_path, simulation=False, readonly=True) as store:
            intents = exportable(store, limit, policy=policy)
    except EditorialError as error:
        if str(error) != "editorial_simulation_mismatch":
            raise
        with EditorialStore(store_path, simulation=True, readonly=True) as store:
            exportable(store, limit, policy=policy)
        raise EditorialError("publication_simulation_forbidden") from None
    if len(intents) > 1:
        raise EditorialError("publication_limit_invalid")
    value = intents[0] if intents else None
    if value is None:
        return {"status": "held", "count": 0, "path": output.name}
    validate_publication_intent(value)
    encoded = _json(value)
    directory = io.open_directory(output.parent, create=True, private=True)
    try:
        io.atomic_write(directory, output.name, encoded)
    finally:
        os.close(directory)
    return {"status": "exported", "count": 1, "path": output.name}
