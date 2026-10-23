"""Offline checks for the staging-only deployment wrapper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bridge.helpers import NOW, export_document

from waagd_discovery import staging
from waagd_discovery.models import DiscoveryError
from waagd_discovery.staging import _active, load_config, set_activation, snapshot


def _config(root: Path) -> Path:
    paths = {
        key: str(root / key)
        for key in (
            "active_export",
            "snapshot_dir",
            "bridge",
            "bridge_workspace",
            "bridge_state",
            "editorial",
            "editorial_config",
            "editorial_store",
            "gateway",
            "gateway_config",
            "gateway_state",
            "gateway_home",
            "trusted_publishers",
            "publication_policy",
            "publication_output",
            "autonomous_cycle",
            "staging",
            "site_node",
            "site_command",
            "site_environment_file",
            "site_working_directory",
            "ingress_token_file",
            "site_state_dir",
            "runtime_state_dir",
        )
    }
    paths["site_state_id"] = "waagd-staging-test-v1"
    value = {
        "schema_version": 1,
        "staging_origin": "https://waagd-staging.waagd.workers.dev",
        "paths": paths,
        "limits": {"gateway_max_candidates": 5, "gateway_max_cost_usd": 0.1},
        "timeouts": {"bridge_seconds": 900, "editorial_seconds": 10800, "cycle_seconds": 15600},
    }
    root.mkdir(mode=0o700)
    path = root / "staging.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_private_config_defaults_site_to_paused_and_requires_explicit_activation(
    tmp_path: Path,
) -> None:
    config = load_config(_config(tmp_path.resolve() / "private"))
    assert not _active(config)
    assert set_activation(config, active=True) == {"status": "completed", "site_active": True}
    assert _active(config)
    assert set_activation(config, active=False) == {"status": "completed", "site_active": False}
    assert not _active(config)


def test_config_rejects_production_origin_without_reading_any_secret(tmp_path: Path) -> None:
    path = _config(tmp_path.resolve() / "private")
    value = json.loads(path.read_text())
    value["staging_origin"] = "https://waagd.workers.dev"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    with pytest.raises(DiscoveryError, match="staging_config_invalid"):
        load_config(path)


def test_snapshot_is_validated_stable_and_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(_config(tmp_path.resolve() / "private"))
    source = Path(config["paths"]["active_export"])
    source.write_text(json.dumps(export_document()))
    source.chmod(0o600)

    class Clock:
        @staticmethod
        def now(_zone):
            return NOW

    monkeypatch.setattr(staging, "datetime", Clock)
    identifier = "11111111-1111-4111-8111-111111111111"
    first = snapshot(config, identifier, "2026-10-18")
    original = first.read_bytes()
    changed = export_document()
    changed["candidates"][0]["title"] = "Changed after the first snapshot"
    source.write_text(json.dumps(changed))
    source.chmod(0o600)
    with pytest.raises(DiscoveryError, match="staging_snapshot_invalid"):
        snapshot(config, identifier, "2026-10-18")
    assert first.read_bytes() == original
