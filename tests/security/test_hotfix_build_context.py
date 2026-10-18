"""The Docker permission regression must use private, isolated source fixtures."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "docker-config-smoke.py"
spec = importlib.util.spec_from_file_location("docker_config_smoke", SCRIPT)
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock"):
        (root / name).write_text(f"runtime fixture: {name}")
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("# runtime fixture")
    (root / "config").mkdir()
    for name in smoke.CONFIGS:
        path = root / "config" / name
        path.write_text(f"test policy: {name}")
        path.chmod(0o600)
    monkeypatch.setattr(smoke, "ROOT", root)
    return root


def test_private_build_context_preserves_source_and_excludes_local_runtime_data(source, tmp_path):
    for name in (".env", "discovery.sqlite3", "token.txt"):
        (source / name).write_text("must never reach fixture build context")
    (source / "exports").mkdir()
    (source / "exports" / "candidates.json").write_text("private runtime material")
    before = {
        path.name: (path.read_bytes(), path.stat().st_mode)
        for path in (source / "config").iterdir()
    }
    context = tmp_path / "context"
    smoke.prepare_context(context)
    assert smoke.verify_context(context) == {name: "0600" for name in smoke.CONFIGS}
    assert {path.name for path in context.iterdir()} == {
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        "src",
        "config",
    }
    for name, (content, mode) in before.items():
        assert (source / "config" / name).read_bytes() == content
        assert (source / "config" / name).stat().st_mode == mode
        assert (context / "config" / name).read_bytes() == content


def test_new_policy_cannot_silently_escape_all_configuration_regression(source, tmp_path):
    (source / "config" / "additional.yaml").write_text("future policy: must add explicit coverage")
    with pytest.raises(AssertionError):
        smoke.prepare_context(tmp_path / "context")


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o666])
def test_non_private_fixture_modes_are_rejected(source, tmp_path, mode):
    context = tmp_path / "context"
    smoke.prepare_context(context)
    (context / "config" / "filter.yaml").chmod(mode)
    with pytest.raises(AssertionError):
        smoke.verify_context(context)


def test_symlinked_fixture_policy_is_rejected(source, tmp_path):
    context = tmp_path / "context"
    smoke.prepare_context(context)
    path = context / "config" / "filter.yaml"
    path.unlink()
    path.symlink_to(source / "config" / "filter.yaml")
    with pytest.raises(AssertionError):
        smoke.verify_context(context)


def test_existing_build_context_is_never_overwritten(source, tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    sentinel = context / "keep.txt"
    sentinel.write_text("existing fixture")
    with pytest.raises(FileExistsError):
        smoke.prepare_context(context)
    assert sentinel.read_text() == "existing fixture"
