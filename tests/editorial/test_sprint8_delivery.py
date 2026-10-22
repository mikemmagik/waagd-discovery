"""Offline end-to-end checks for the durable Site delivery seam."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from test_sprint8_publication_progression import _policy, _store

from waagd_discovery.editorial.delivery import PublicationDelivery
from waagd_discovery.editorial.site_import import SiteImportOutcome


class FakeImporter:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.ids: list[str] = []

    def import_intent(self, intent_file: Path) -> SiteImportOutcome:
        import json

        self.ids.append(json.loads(intent_file.read_text())["intent_id"])
        return SiteImportOutcome(self.outcomes.pop(0))  # type: ignore[arg-type]


def _delivery(tmp_path: Path, importer: FakeImporter) -> PublicationDelivery:
    store = tmp_path.resolve() / "store" / "editorial.sqlite3"
    if not store.exists():
        _store(store)
    return PublicationDelivery(
        store,
        tmp_path.resolve() / "export" / "publication-intent.json",
        _policy(),
        importer,
    )


def test_delivery_advances_two_intents_only_after_site_receipts(tmp_path: Path) -> None:
    importer = FakeImporter(["created", "duplicate"])
    result = _delivery(tmp_path, importer).run(datetime.now(UTC))
    assert result == {"status": "completed", "code": None, "delivered": 2, "held": 0}
    assert len(importer.ids) == 2
    assert importer.ids[0] != importer.ids[1]


def test_uncertain_site_result_is_never_resubmitted(tmp_path: Path) -> None:
    first = FakeImporter(["uncertain"])
    assert _delivery(tmp_path, first).run(datetime.now(UTC)) == {
        "status": "operator_required",
        "code": "site_outcome_ambiguous",
    }
    second = FakeImporter(["created"])
    assert _delivery(tmp_path, second).run(datetime.now(UTC)) == {
        "status": "operator_required",
        "code": "site_outcome_ambiguous",
    }
    assert first.ids and not second.ids


def test_held_item_is_preserved_for_a_later_site_resume(tmp_path: Path) -> None:
    importer = FakeImporter(["held", "created", "created"])
    assert _delivery(tmp_path, importer).run(datetime.now(UTC)) == {
        "status": "completed",
        "code": "site_hold",
        "delivered": 0,
        "held": 1,
    }
    assert _delivery(tmp_path, importer).run(datetime.now(UTC)) == {
        "status": "completed",
        "code": None,
        "delivered": 2,
        "held": 0,
    }
