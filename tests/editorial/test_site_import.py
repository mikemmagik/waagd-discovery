"""Offline tests for the provisional, independently replaceable Site boundary."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.site_import import (
    CommandSiteImporter,
    SiteImportCommand,
    SiteImportOutcome,
)


class FixtureParser:
    """Test-only parser for the local fake, not a Site protocol commitment."""

    def parse(self, returncode: int, output: bytes) -> SiteImportOutcome:
        value = json.loads(output)
        if returncode != 0 or not isinstance(value, dict):
            raise ValueError
        return SiteImportOutcome(value["fixture_status"], value.get("receipt"))


def _private(path: Path, content: bytes = b"token") -> Path:
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _intent(path: Path) -> Path:
    from waagd_discovery.editorial.publication import _content_hash

    value = {
        "version": 2,
        "intent_id": "auto-fixture-r1",
        "origin": "auto",
        "authorization": {"kind": "discovery", "approved": True},
        "editorial": {
            "decision_id": "decision-fixture-r1",
            "source_event_id": "fixture-event",
            "source_attribution": "Fixture News",
            "approved_at": "2026-10-23T00:00:00.000Z",
            "publication_policy": {"version": "publication-policy-v1", "outcome": "approved"},
            "review_route": "trusted_publisher_auto",
            "trusted_publisher": {
                "publisher_id": "fixture",
                "publisher_name": "Fixture News",
                "trusted_rss_url": "https://fixture.example.test/rss",
                "article_url": "https://fixture.example.test/story",
                "article_host": "fixture.example.test",
                "verified_at": "2026-10-23T00:00:00.000Z",
                "safety_gates": {
                    "publisher_identity": True,
                    "safe_retrieval": True,
                    "sensitive_content": "clear",
                },
            },
        },
        "post": {
            "type": "news",
            "origin": "auto",
            "url": "https://fixture.example.test/story",
            "canonical_url": "https://fixture.example.test/story",
            "source_item_id": "waagd:fixture",
            "title": "Fixture title",
            "source": "WAAGD",
            "preview": None,
            "embed": None,
            "body": "Fixture body",
            "references": [{"url": "https://fixture.example.test/story", "title": "Fixture"}],
        },
    }
    value["content_hash"] = _content_hash(value)
    return _private(path, json.dumps(value, separators=(",", ":")).encode())


def _fake(path: Path) -> Path:
    path.write_text(
        "import json, os, sys\n"
        "argv = sys.argv\n"
        "intent = argv[argv.index('--intent-file') + 1]\n"
        "token = argv[argv.index('--ingress-token-file') + 1]\n"
        "assert os.path.isabs(intent) and os.path.isabs(token)\n"
        "assert 'SITE_TEST_SECRET' not in os.environ\n"
        "statuses = {'created', 'duplicate', 'held', 'failed', 'uncertain'}\n"
        "status = argv[1] if len(argv) > 1 and argv[1] in statuses else 'created'\n"
        "print(json.dumps({'fixture_status': status, 'receipt': 'receipt-fixture'}))\n"
    )
    path.chmod(0o700)
    return path


@pytest.mark.parametrize("status", ["created", "duplicate", "held", "failed", "uncertain"])
def test_fake_site_outcomes_are_parsed_only_by_injected_fixture_protocol(
    tmp_path: Path, status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    fake = _fake(root / "fake_site.py")
    intent = _intent(root / "publication-intent.json")
    token = _private(root / "ingress-token")
    monkeypatch.setenv("SITE_TEST_SECRET", "must-not-reach-site")
    command = SiteImportCommand(Path(sys.executable).resolve(), (str(fake), status))
    importer = CommandSiteImporter(
        command,
        token,
        parser=FixtureParser(),
    )
    assert importer.import_intent(intent) == SiteImportOutcome(status, "receipt-fixture")


def test_default_protocol_never_interprets_provisional_site_output(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    importer = CommandSiteImporter(
        SiteImportCommand(Path(sys.executable).resolve(), (str(_fake(root / "fake.py")),)),
        _private(root / "token"),
    )
    assert importer.import_intent(_intent(root / "publication-intent.json")) == SiteImportOutcome(
        "uncertain"
    )


def test_timeout_and_unparseable_output_are_uncertain(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    sleeper = root / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(3)\n")
    sleeper.chmod(0o700)
    importer = CommandSiteImporter(
        SiteImportCommand(Path(sys.executable).resolve(), (str(sleeper),), timeout_seconds=1),
        _private(root / "token"),
        parser=FixtureParser(),
    )
    assert importer.import_intent(_intent(root / "publication-intent.json")) == SiteImportOutcome(
        "uncertain"
    )


def test_private_files_and_command_are_required(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    token = _private(root / "token")
    token.chmod(0o644)
    with pytest.raises(EditorialError, match="site_token_file_invalid"):
        CommandSiteImporter(
            SiteImportCommand(Path(sys.executable).resolve(), ("-c", "pass")), token
        )
    insecure = root / "insecure"
    insecure.write_text("x")
    insecure.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IWGRP)
    with pytest.raises(EditorialError, match="site_command_invalid"):
        SiteImportCommand(insecure, ())


def test_invalid_intent_never_starts_site_process(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    marker = root / "marker"
    fake = root / "fake.py"
    fake.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    fake.chmod(0o700)
    importer = CommandSiteImporter(
        SiteImportCommand(Path(sys.executable).resolve(), (str(fake),)), _private(root / "token")
    )
    with pytest.raises(EditorialError, match="site_intent_file_invalid"):
        importer.import_intent(_private(root / "publication-intent.json", b"{}"))
    assert not marker.exists()
