"""Durable, one-at-a-time delivery of already-approved publication intents.

This module is deliberately downstream of collection, Bridge, and editorial
processing.  It never starts a Gateway or reads a Discovery database.  Its sole
job is to bind an immutable intent in the Editorial Store to a separate Site
importer and to retain ambiguity for reconciliation.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.publication import prepare_next_publication
from waagd_discovery.editorial.publication_policy import PublicationPolicy
from waagd_discovery.editorial.site_import import SiteImportPort
from waagd_discovery.editorial.store import EditorialStore

_NAME = "publication-intent.json"
_MAXIMUM_PER_CYCLE = 5

CycleStatus = Literal["completed", "retryable", "operator_required"]


def _clock(value: datetime | None = None) -> str:
    now = value or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise EditorialError("publication_clock_invalid")
    return now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_intent(path: Path, intent: dict[str, object]) -> Path:
    if not path.is_absolute() or path.name != _NAME:
        raise EditorialError("publication_output_invalid")
    try:
        encoded = json.dumps(
            intent, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise EditorialError("publication_payload_invalid") from None
    directory = io.open_directory(path.parent, create=True, private=True)
    try:
        io.atomic_write(directory, _NAME, encoded)
    except io.BridgeError:
        raise EditorialError("publication_output_invalid") from None
    finally:
        os.close(directory)
    return path


class PublicationDelivery:
    """Submit at most five immutable jobs with no retry after uncertainty."""

    def __init__(
        self,
        store_path: Path,
        output: Path,
        policy: PublicationPolicy,
        importer: SiteImportPort,
    ) -> None:
        self.store_path = store_path
        self.output = output
        self.policy = policy
        self.importer = importer

    def run(self, now: datetime | None = None) -> dict[str, object]:
        """Deliver available work, retaining the stable identity on every error.

        A known ``failed`` Site result may be retried once by a later bounded
        scheduler attempt with its same intent.  An ``uncertain`` result is an
        operator-required reconciliation boundary and is never resubmitted.
        """

        stamp = _clock(now)
        delivered = 0
        held = 0
        with EditorialStore(self.store_path, simulation=False) as store:
            store.recover_publication_submissions(stamp)
            for _ in range(_MAXIMUM_PER_CYCLE):
                prepared = prepare_next_publication(store, policy=self.policy, clock=stamp)
                if prepared is None:
                    return {
                        "status": "completed",
                        "code": "no_eligible_intents" if not delivered and not held else None,
                        "delivered": delivered,
                        "held": held,
                    }
                intent = prepared["intent"]
                if not isinstance(intent, dict) or not isinstance(intent.get("intent_id"), str):
                    raise EditorialError("publication_store_invalid")
                intent_id = intent["intent_id"]
                status = prepared["status"]
                if status == "uncertain":
                    return {"status": "operator_required", "code": "site_outcome_ambiguous"}
                if status == "in_flight":
                    return {"status": "operator_required", "code": "site_submission_interrupted"}
                if status == "failed":
                    store.retry_failed_publication(intent_id, stamp)
                elif status not in {"pending", "held"}:
                    raise EditorialError("publication_store_invalid")
                _write_intent(self.output, intent)
                store.begin_publication_submission(intent_id, stamp)
                outcome = self.importer.import_intent(self.output)
                store.record_publication_outcome(intent_id, outcome.status, stamp)
                if outcome.status in {"created", "duplicate"}:
                    delivered += 1
                    continue
                if outcome.status == "held":
                    return {
                        "status": "completed",
                        "code": "site_hold",
                        "delivered": delivered,
                        "held": held + 1,
                    }
                if outcome.status == "failed":
                    return {"status": "operator_required", "code": "site_import_failed"}
                return {"status": "operator_required", "code": "site_outcome_ambiguous"}
        return {"status": "completed", "delivered": delivered, "held": held}
