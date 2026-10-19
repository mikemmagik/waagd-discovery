"""Export only independently verified, publication-eligible editorial decisions.

This module never contacts a publisher and never handles publisher credentials.
It is the narrow hand-off from the private Editorial Store to a separately owned
host-side publication journal.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import (
    EditorialError,
    validate_decision,
    validate_verification,
)
from waagd_discovery.editorial.store import EditorialStore

_MAXIMUM = 256 * 1024


def _canonical(value: dict[str, Any]) -> bytes:
    """Match the website publisher's sorted-key UTF-8 payload digest exactly."""
    try:
        return json.dumps(
            value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise EditorialError("publication_payload_invalid") from None


def _source_item_id(identity: str) -> str:
    return "waagd:" + hashlib.sha256(identity.encode("ascii")).hexdigest()


def _payload(
    candidate: dict[str, Any], verification: dict[str, Any], decision: dict[str, Any], revision: int
) -> dict[str, Any]:
    key = candidate["candidate_key"]
    identity = f"auto:{key}:{revision}"
    evidence = {entry["id"]: entry for entry in verification["evidence"]}
    evidence_ids = decision["verification_evidence"]
    if (
        decision["decision"] != "READY_FOR_REVIEW"
        or verification["verification_status"] != "VERIFIED"
        or decision["unverified_claims"]
        or decision["open_questions"]
        or decision["risks"]
        or not evidence_ids
        or any(item not in evidence for item in evidence_ids)
    ):
        raise EditorialError("publication_not_eligible")
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in evidence_ids:
        url = evidence[item]["url"]
        if url not in seen:
            references.append({"url": url, "title": "Verified evidence"})
            seen.add(url)
    payload = {
        "type": "article",
        "origin": "auto",
        "url": candidate["url"],
        "canonical_url": candidate["url"],
        "source_item_id": _source_item_id(identity),
        "title": decision["headline"],
        "source": "WAAGD",
        "preview": None,
        "embed": None,
        "body": decision["article"],
        "references": references,
    }
    # These limits mirror the public Worker contract. The website independently
    # validates them again before an authenticated POST.
    if (
        len(payload["title"]) > 300
        or len(payload["source"]) > 100
        or len(payload["body"]) > 100000
        or len(payload["references"]) > 100
        or len(payload["url"]) > 2048
        or len(_canonical(payload)) > _MAXIMUM
    ):
        raise EditorialError("publication_payload_invalid")
    content_digest = hashlib.sha256(_canonical(payload)).hexdigest()
    return {
        "identity": identity,
        "mode": "verified_auto",
        "candidate_key": key,
        "decision_revision": revision,
        "evidence_ids": evidence_ids,
        "source_url": candidate["url"],
        "content_digest": content_digest,
        "payload": payload,
    }


def exportable(store: EditorialStore) -> list[dict[str, Any]]:
    """Return deterministic eligible intents from a real, validated store only."""
    if store.simulation:
        raise EditorialError("publication_simulation_forbidden")
    rows = store.connection.execute(
        "SELECT c.candidate_key,c.current_revision,d.request,d.result AS decision_result,"
        "v.result AS verification_result FROM candidates c "
        "JOIN decisions d ON d.candidate_key=c.candidate_key AND d.revision=c.current_revision "
        "JOIN verifications v ON v.candidate_key=c.candidate_key AND v.revision=c.current_revision "
        "WHERE c.current_revision > 0 ORDER BY c.candidate_key"
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            request = io.decode(row["request"].encode())
            decision = io.decode(row["decision_result"].encode())
            verification = io.decode(row["verification_result"].encode())
            candidate = request["candidate"]
            revision = row["current_revision"]
            if (
                not isinstance(row["candidate_key"], str)
                or candidate["candidate_key"] != row["candidate_key"]
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise EditorialError("publication_store_invalid")
            validate_verification(verification, candidate["candidate_key"])
            validate_decision(decision, request)
            if decision["decision"] == "READY_FOR_REVIEW":
                try:
                    results.append(_payload(candidate, verification, decision, revision))
                except EditorialError as error:
                    if str(error) != "publication_not_eligible":
                        raise
        except (EditorialError, io.BridgeError, KeyError, TypeError, ValueError):
            # Corrupt completed state fails closed; a valid non-ready decision
            # is deliberately absent from the export.
            raise EditorialError("publication_store_invalid") from None
    return results


def export_file(store_path: Path, output: Path) -> dict[str, Any]:
    """Atomically write an inspectable publication batch into a private directory."""
    io.path_parts(store_path)
    io.path_parts(output)
    if not output.name or output.suffix != ".json" or output.parent == Path("/"):
        raise EditorialError("publication_output_invalid")
    try:
        with EditorialStore(store_path, simulation=False, readonly=True) as store:
            intents = exportable(store)
    except EditorialError as error:
        # EditorialStore deliberately refuses to open an initialized simulation
        # database as real. Reopen only to identify that exact durable state;
        # exportable then emits the stable, fail-closed diagnostic.
        if str(error) != "editorial_simulation_mismatch":
            raise
        try:
            with EditorialStore(store_path, simulation=True, readonly=True) as store:
                exportable(store)
        except EditorialError as simulation:
            if str(simulation) == "publication_simulation_forbidden":
                raise
            raise EditorialError("publication_store_invalid") from None
        raise EditorialError("publication_store_invalid") from None
    result = {
        "schema_version": 1,
        "producer": "waagd-discovery-publication-v1",
        "intents": intents,
    }
    encoded = io.encode(result)
    if len(encoded) > _MAXIMUM:
        raise EditorialError("publication_export_too_large")
    directory = io.open_directory(output.parent, create=True, private=True)
    try:
        io.atomic_write(directory, output.name, encoded)
    finally:
        os.close(directory)
    return {"status": "exported", "count": len(intents), "path": output.name}
