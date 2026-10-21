"""Data-only producer for the site PublicationIntent v1 core envelope."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

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


def _v2_trusted_provenance(intent: dict[str, Any]) -> dict[str, Any] | None:
    """Rebuild the frozen Site v2 provenance document in its wire order."""
    editorial = intent["editorial"]
    trusted = editorial["trusted_publisher"]
    if trusted is None:
        return None
    return {
        "publisher_id": trusted["publisher_id"],
        "publisher_name": trusted["publisher_name"],
        "trusted_rss_url": trusted["trusted_rss_url"],
        "article_url": trusted["article_url"],
        "article_host": trusted["article_host"],
        "verified_at": trusted["verified_at"],
        "safety_gates": {
            "publisher_identity": trusted["safety_gates"]["publisher_identity"],
            "safe_retrieval": trusted["safety_gates"]["safe_retrieval"],
            "sensitive_content": trusted["safety_gates"]["sensitive_content"],
        },
    }


def _content_hash(intent: dict[str, Any]) -> str:
    """Match Site ``publicationIntentHashDocument()`` for frozen v1 and v2."""
    try:
        post = intent["post"]
        editorial = intent["editorial"]
        document: dict[str, Any] = {
            "version": intent["version"],
            "intent_id": intent["intent_id"],
            "origin": intent["origin"],
            "authorization": {
                "kind": intent["authorization"]["kind"],
                "approved": intent["authorization"]["approved"],
            },
            "editorial": {
                "decision_id": editorial["decision_id"],
                "source_event_id": editorial["source_event_id"],
                "source_attribution": editorial["source_attribution"],
                "approved_at": editorial["approved_at"],
                "publication_policy": {
                    "version": editorial["publication_policy"]["version"],
                    "outcome": editorial["publication_policy"]["outcome"],
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
        if intent["version"] == 2:
            document["editorial"].update(
                {
                    "review_route": editorial["review_route"],
                    "trusted_publisher": _v2_trusted_provenance(intent),
                }
            )
        elif intent["version"] != 1:
            raise ValueError
        return hashlib.sha256(_json(document)).hexdigest()
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise EditorialError("publication_payload_invalid") from None


def _trusted_provenance(candidate: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    """Make the Site v2 identity proof solely from validated host data."""
    trust = verification.get("publisher_trust")
    article_url = verification.get("canonical_url")
    publisher = verification.get("publisher") or candidate.get("publisher")
    if (
        not isinstance(trust, dict)
        or set(trust) != {"publisher_id", "rss_url"}
        or not _text(trust.get("publisher_id"), 64)
        or not _text(trust.get("rss_url"), 2048)
        or not _text(article_url, 2048)
        or not _text(publisher, 100)
    ):
        raise EditorialError("publication_not_eligible")
    safe_article_url = cast(str, article_url)
    safe_rss_url = cast(str, trust["rss_url"])
    safe_publisher = cast(str, publisher)
    safe_publisher_id = cast(str, trust["publisher_id"])
    try:
        article = urlsplit(safe_article_url)
        rss = urlsplit(safe_rss_url)
        if (
            article.scheme != "https"
            or rss.scheme != "https"
            or not article.hostname
            or article.username is not None
            or article.password is not None
            or article.port not in (None, 443)
            or article.fragment
        ):
            raise ValueError
    except (AttributeError, ValueError):
        raise EditorialError("publication_not_eligible") from None
    return {
        "publisher_id": safe_publisher_id,
        "publisher_name": safe_publisher,
        "trusted_rss_url": safe_rss_url,
        "article_url": safe_article_url,
        "article_host": article.hostname.lower().rstrip("."),
        "verified_at": _timestamp(verification["fetched_at"]),
        "safety_gates": {
            "publisher_identity": True,
            "safe_retrieval": True,
            "sensitive_content": "clear",
        },
    }


def _payload(
    candidate: dict[str, Any],
    verification: dict[str, Any],
    decision: dict[str, Any],
    delivery_id: str,
    revision: int,
    policy: PublicationPolicy,
) -> dict[str, Any]:
    del delivery_id  # Transport history must never affect stable publication identity.
    evidence = {item["id"]: item for item in verification["evidence"]}
    ids = decision["verification_evidence"]
    status = verification["verification_status"]
    published = _timestamp(candidate["published_at"])
    trusted = status == "TRUSTED_PUBLISHER"
    verified = status == "VERIFIED"
    if (
        decision["decision"] != "READY_FOR_REVIEW"
        or not (verified or trusted)
        or decision["open_questions"]
        or not ids
        or any(item not in evidence for item in ids)
        or not policy.permits(published)
        or (verified and (decision["unverified_claims"] or decision["risks"]))
        or (trusted and (not decision["unverified_claims"] or not decision["risks"]))
    ):
        raise EditorialError("publication_not_eligible")
    key = candidate["candidate_key"]
    intent_id = _intent_id(key, revision)
    if trusted:
        provenance = _trusted_provenance(candidate, verification)
        source = provenance["publisher_name"]
        article_url = provenance["article_url"]
        references = [{"url": article_url, "title": source + " source"}]
        version = 2
        route = "trusted_publisher"
    else:
        publisher = candidate.get("publisher") or verification.get("publisher")
        if not _text(publisher, 300):
            raise EditorialError("publication_not_eligible")
        provenance = None
        source = "WAAGD"
        article_url = candidate["url"]
        references = [{"url": evidence[item]["url"], "title": "Verified evidence"} for item in ids]
        version = 1
        route = None
    post = {
        "type": "news",
        "origin": "auto",
        "url": article_url,
        "canonical_url": article_url,
        "source_item_id": _source_item_id(intent_id),
        "title": decision["headline"],
        "source": source,
        "preview": None,
        "embed": None,
        "body": decision["article"],
        "references": references,
    }
    if (
        not _text(post["title"], 300)
        or not _text(post["body"], 100000)
        or not _text(post["source_item_id"], 256)
        or len(post["references"]) > 100
        or len(_json(post)) > _MAXIMUM
    ):
        raise EditorialError("publication_payload_invalid")
    editorial: dict[str, Any] = {
        "decision_id": f"decision-{key}-r{revision}",
        "source_event_id": f"candidate:{key};revision:{revision}",
        "source_attribution": source if trusted else publisher,
        "approved_at": _timestamp(decision["created_at"]),
        "publication_policy": {"version": policy.version, "outcome": "approved"},
    }
    if trusted:
        editorial.update({"review_route": route, "trusted_publisher": provenance})
    intent = {
        "version": version,
        "intent_id": intent_id,
        "origin": "auto",
        "authorization": {"kind": "discovery", "approved": True},
        "editorial": editorial,
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
    """Strict local mirror of the frozen Site v1/v2 envelope boundary."""
    try:
        required = {
            "version",
            "intent_id",
            "content_hash",
            "origin",
            "authorization",
            "editorial",
            "post",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError
        if (
            value["version"] not in {1, 2}
            or value["origin"] != "auto"
            or value["authorization"] != {"kind": "discovery", "approved": True}
            or not isinstance(value["intent_id"], str)
            or not value["intent_id"].startswith("auto-")
            or value["content_hash"] != _content_hash(value)
        ):
            raise ValueError
        editorial = value["editorial"]
        if not isinstance(editorial, dict):
            raise ValueError
        v1_fields = {
            "decision_id",
            "source_event_id",
            "source_attribution",
            "approved_at",
            "publication_policy",
        }
        if value["version"] == 1:
            if set(editorial) != v1_fields:
                raise ValueError
        else:
            if set(editorial) != v1_fields | {"review_route", "trusted_publisher"}:
                raise ValueError
            route = editorial["review_route"]
            trusted = editorial["trusted_publisher"]
            if route == "independently_verified":
                if trusted is not None:
                    raise ValueError
            elif route == "trusted_publisher":
                if not isinstance(trusted, dict) or set(trusted) != {
                    "publisher_id",
                    "publisher_name",
                    "trusted_rss_url",
                    "article_url",
                    "article_host",
                    "verified_at",
                    "safety_gates",
                }:
                    raise ValueError
                gates = trusted["safety_gates"]
                article = urlsplit(trusted["article_url"])
                if (
                    not isinstance(gates, dict)
                    or gates
                    != {
                        "publisher_identity": True,
                        "safe_retrieval": True,
                        "sensitive_content": "clear",
                    }
                    or not all(
                        _text(trusted[key], 2048)
                        for key in (
                            "publisher_id",
                            "publisher_name",
                            "trusted_rss_url",
                            "article_url",
                            "article_host",
                            "verified_at",
                        )
                    )
                    or urlsplit(trusted["trusted_rss_url"]).scheme != "https"
                    or article.scheme != "https"
                    or article.hostname != trusted["article_host"]
                    or value["post"]["canonical_url"] != trusted["article_url"]
                ):
                    raise ValueError
            else:
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
