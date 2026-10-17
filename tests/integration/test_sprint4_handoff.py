"""Automatic data export stays separate from ingestion, health and raw provenance."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

from waagd_discovery import collection, export
from waagd_discovery.cli import main
from waagd_discovery.handoff import integration_status, load_handoff_policy
from waagd_discovery.logging import configure
from waagd_discovery.models import DiscoveryError, FetchResult
from waagd_discovery.storage import Repository


@pytest.fixture
def setup(tmp_path, monkeypatch):
    configure()
    config = tmp_path / "config"
    config.mkdir()
    sources = config / "sources.yaml"
    sources.write_text("""sources:
- {id: one, name: One, url: 'https://example.org/one.xml'}
- {id: two, name: Two, url: 'https://example.org/two.xml'}
""")
    policy = config / "filter.yaml"
    policy.write_text("filter: {}")
    output = tmp_path / "exports" / "candidates.json"
    handoff = config / "handoff.yaml"
    handoff.write_text(f'handoff: {{enabled: true, output: "{output}", limit: 1}}')
    db = tmp_path / "data" / "discovery.sqlite3"
    failures = set()

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch(self, source):
            if source.id in failures:
                raise DiscoveryError("http_status_429")
            date = format_datetime(datetime.now(UTC))
            return FetchResult(
                200,
                f"""<rss version="2.0"><channel><item>
            <title>Unusual news {source.id}</title><link>https://example.org/{source.id}</link>
            <description>Untrusted metadata retained as ordinary bounded JSON data.</description>
            <pubDate>{date}</pubDate></item></channel></rss>""".encode(),
            )

    monkeypatch.setattr(collection, "HttpClient", Client)
    return db, sources, policy, handoff, output, failures


def run(setup, **kwargs):
    db, sources, policy, handoff, _, _ = setup
    return collection.run_once(db, sources, policy, handoff_config=handoff, **kwargs)


def test_success_exports_current_bounded_candidates_and_retains_database(setup):
    db, _, _, handoff, output, _ = setup
    result = run(setup)
    assert result["status"] == "success"
    assert result["handoff"] == {"status": "exported", "candidates": 1, "diagnostics_saved": True}
    payload = json.loads(output.read_text())
    assert payload["schema_version"] == 2 and len(payload["candidates"]) == 1
    assert payload["selection"]["assessment_run_id"] == result["filter"]["run_id"]
    assert all(payload["candidates"][0]["selection"]["checks"].values())
    assert output.stat().st_mode & 0o777 == 0o600
    with Repository(db) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert repo.stats()["articles"] == 2 and repo.stats()["observations"] == 2
    status = integration_status(db, handoff)
    assert status["healthy"] and status["last_attempt"]["collection_run_id"] == result["run_id"]


def test_no_new_articles_refreshes_export_without_duplicate_articles(setup):
    db, _, _, _, output, _ = setup
    run(setup)
    first = json.loads(output.read_text())
    result = run(setup)
    second = json.loads(output.read_text())
    assert result["items_added"] == 0 and result["items_existing"] == 2
    assert second["selection"]["assessment_run_id"] != first["selection"]["assessment_run_id"]
    with Repository(db) as repo:
        assert repo.stats()["articles"] == 2


def test_partial_collection_can_publish_completed_assessment(setup):
    setup[-1].add("two")
    result = run(setup)
    assert result["status"] == "partial" and result["handoff"]["candidates"] == 1


def test_fully_failed_collection_keeps_previous_export_and_assessment(setup):
    run(setup)
    before = setup[-2].read_bytes()
    setup[-1].update(["one", "two"])
    result = run(setup)
    assert result["status"] == "failed" and "handoff" not in result and "filter" not in result
    assert setup[-2].read_bytes() == before


def test_disabled_handoff_has_no_extra_files(setup):
    setup[3].write_text("handoff: {enabled: false}")
    result = run(setup)
    assert result["handoff"] == {"status": "disabled"}
    assert not setup[-2].exists()
    assert not Path(str(setup[0]) + ".handoff.json").exists()


def test_dry_run_never_publishes_or_creates_real_database(setup):
    result = run(setup, dry_run=True)
    assert result["dry_run"] and "handoff" not in result
    assert not setup[0].exists() and not setup[-2].exists()


def test_bad_handoff_configuration_does_not_stop_ingestion(setup):
    setup[3].write_text("handoff: {enabled: true, limit: true}")
    result = run(setup)
    assert result["status"] == "success"
    assert result["handoff"]["status"] == "error"
    assert result["handoff"]["code"] == "handoff_config_invalid"
    with Repository(setup[0]) as repo:
        assert repo.stats()["articles"] == 2


def test_export_failure_preserves_previous_file_and_is_separately_unhealthy(setup, monkeypatch):
    run(setup)
    before = setup[-2].read_bytes()
    monkeypatch.setattr(export, "write_json_atomic", lambda *args: (_ for _ in ()).throw(OSError()))
    result = run(setup)
    assert result["status"] == "success" and result["handoff"]["status"] == "error"
    assert setup[-2].read_bytes() == before
    status = integration_status(setup[0], setup[3])
    assert status["status"] == "current" and not status["healthy"]
    assert status["last_attempt"]["code"] == "handoff_export_failed"


def test_enabled_auto_target_is_reserved_from_manual_export(setup, capsys):
    run(setup)
    output = setup[-2]
    before = output.read_bytes()
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                str(setup[0]),
                "--handoff-config",
                str(setup[3]),
                "export",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "export_target_reserved"
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        "handoff: {enabled: true, enabled: false}",
        "handoff: {limit: .nan}",
        "handoff: {limit: 501}",
        'handoff: {output: "x.sqlite3"}',
        "handoff: {enabled: 1}",
        "handoff: {output: 5}",
        "[]",
    ],
)
def test_bad_policy_is_a_safe_configuration_error(tmp_path, bad):
    path = tmp_path / "handoff.yaml"
    path.write_text(bad)
    with pytest.raises(DiscoveryError):
        load_handoff_policy(path)


@pytest.mark.parametrize("age,expected", [(3, "stale"), (-1, "future")])
def test_diagnostics_rejects_stale_or_future_exports(setup, age, expected):
    run(setup)
    payload = json.loads(setup[-2].read_text())
    payload["generated_at"] = (datetime.now(UTC) - timedelta(hours=age)).isoformat()
    setup[-2].write_text(json.dumps(payload))
    status = integration_status(setup[0], setup[3])
    assert not status["healthy"] and status["status"] == expected


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', '{"x":1e400}', '{"x":NaN}'])
def test_diagnostic_json_is_strict(setup, content):
    setup[-2].parent.mkdir()
    setup[-2].write_text(content)
    result = integration_status(setup[0], setup[3])
    assert not result["healthy"] and result["status"] == "error"


def test_auto_export_does_not_overwrite_database_directory_or_config(setup):
    db, sources, _, handoff, _, _ = setup
    handoff.write_text(f'handoff: {{enabled: true, output: "{db.parent / "other.json"}"}}')
    result = run(setup)
    assert result["handoff"]["code"] == "handoff_target_protected"
    original = sources.read_bytes()
    handoff.write_text(f'handoff: {{enabled: true, output: "{sources}"}}')
    result = run(setup)
    assert result["handoff"]["status"] == "error"
    assert sources.read_bytes() == original
