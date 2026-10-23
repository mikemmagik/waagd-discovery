"""Trusted, staging-only glue for the unattended Sprint 8 package.

Configuration is private JSON data, never shell source.  This module is the
only package component that reads it; the outer coordinator and individual
stages retain their deliberately minimal inherited environments.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from waagd_discovery.bridge import io
from waagd_discovery.bridge.validation import validate_export
from waagd_discovery.models import DiscoveryError

_ORIGIN = "https://waagd-staging.waagd.workers.dev"
_MAX_EXPORT = 8 * 1024 * 1024
_MAX_OUTPUT = 64 * 1024
_SAFE_STATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PATH_KEYS = frozenset(
    {
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
        "site_state_id",
        "runtime_state_dir",
    }
)


def _absolute(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise DiscoveryError("staging_config_invalid")
    return value


def load_config(path: Path) -> dict[str, Any]:
    """Load a small, private, exact-shape configuration without evaluating it."""

    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if (
            not path.is_absolute()
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ValueError
        directory = io.open_directory(resolved.parent, private=True)
        try:
            value = json.loads(io.read_file(directory, resolved.name, 16 * 1024, private=True))
        finally:
            os.close(directory)
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "staging_origin",
            "paths",
            "limits",
            "timeouts",
        }:
            raise ValueError
        if value["schema_version"] != 1 or value["staging_origin"] != _ORIGIN:
            raise ValueError
        paths = value["paths"]
        if not isinstance(paths, dict) or set(paths) != _PATH_KEYS:
            raise ValueError
        for key, item in paths.items():
            if key == "site_state_id":
                if not isinstance(item, str) or _SAFE_STATE_ID.fullmatch(item) is None:
                    raise ValueError
                continue
            _absolute(item)
        limits, timeouts = value["limits"], value["timeouts"]
        if (
            not isinstance(limits, dict)
            or set(limits) != {"gateway_max_candidates", "gateway_max_cost_usd"}
            or not isinstance(timeouts, dict)
            or set(timeouts)
            != {"bridge_seconds", "editorial_seconds", "cycle_seconds", "gateway_seconds"}
            or type(limits["gateway_max_candidates"]) is not int
            or not 1 <= limits["gateway_max_candidates"] <= 5
            or type(limits["gateway_max_cost_usd"]) not in {int, float}
            or not 0 < limits["gateway_max_cost_usd"] <= 1
            or any(type(item) is not int or item < 1 for item in timeouts.values())
            # GatewayEditor has the matching hard safety range.  Requiring the
            # stage deadline to be strictly larger prevents the wrapper from
            # pre-empting a still-bounded Gateway call.
            or not 5 <= timeouts["gateway_seconds"] <= 600
            or timeouts["editorial_seconds"] <= timeouts["gateway_seconds"]
            or timeouts["cycle_seconds"] <= timeouts["editorial_seconds"]
        ):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError, io.BridgeError):
        raise DiscoveryError("staging_config_invalid") from None


def _state_path(config: dict[str, Any]) -> Path:
    return Path(config["paths"]["runtime_state_dir"]) / "site-activation.json"


def _active(config: dict[str, Any]) -> bool:
    path = _state_path(config)
    try:
        directory = io.open_directory(path.parent, private=True)
        try:
            return bool(
                io.decode(io.read_file(directory, path.name, 256, private=True))
                == {
                    "version": 1,
                    "mode": "active",
                    "origin": _ORIGIN,
                }
            )
        finally:
            os.close(directory)
    except (OSError, io.BridgeError):
        return False


def set_activation(config: dict[str, Any], *, active: bool) -> dict[str, object]:
    path = _state_path(config)
    try:
        directory = io.open_directory(path.parent, create=True, private=True)
        try:
            io.atomic_write(
                directory,
                path.name,
                io.encode(
                    {"version": 1, "mode": "active" if active else "paused", "origin": _ORIGIN}
                ),
            )
        finally:
            os.close(directory)
    except (OSError, io.BridgeError):
        raise DiscoveryError("staging_state_invalid") from None
    return {"status": "completed", "site_active": active}


def snapshot(config: dict[str, Any], cycle_id: str, local_date: str) -> Path:
    """Validate then preserve one immutable source export for this cycle."""

    try:
        if (
            str(UUID(cycle_id)) != cycle_id
            or date.fromisoformat(local_date).isoformat() != local_date
        ):
            raise ValueError
        source = Path(config["paths"]["active_export"])
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_EXPORT:
                raise ValueError
            data = os.read(descriptor, _MAX_EXPORT + 1)
        finally:
            os.close(descriptor)
        if len(data) > _MAX_EXPORT:
            raise ValueError
        validate_export(io.decode(data), datetime.now(UTC))
        target = Path(config["paths"]["snapshot_dir"]) / f"{local_date}-{cycle_id}.json"
        directory = io.open_directory(target.parent, create=True, private=True)
        try:
            try:
                existing = io.read_file(directory, target.name, _MAX_EXPORT, private=True)
            except FileNotFoundError:
                io.atomic_write(directory, target.name, data)
            else:
                if existing != data:
                    raise ValueError
        finally:
            os.close(directory)
        return target
    except (OSError, ValueError, io.BridgeError):
        raise DiscoveryError("staging_snapshot_invalid") from None


def _stage_result(argv: list[str], environment: dict[str, str], timeout: int) -> dict[str, object]:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=timeout,
            check=False,
        )
        if result.returncode or len(result.stdout) > _MAX_OUTPUT:
            return {"status": "operator_required"}
        value = json.loads(result.stdout.decode("utf-8"))
        return value if isinstance(value, dict) else {"status": "operator_required"}
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, ValueError):
        return {"status": "operator_required"}


def bridge_stage(config: dict[str, Any], export: Path) -> dict[str, object]:
    paths, limits, timeouts = config["paths"], config["limits"], config["timeouts"]
    value = _stage_result(
        [
            paths["bridge"],
            "--export",
            str(export),
            "--workspace-root",
            paths["bridge_workspace"],
            "--state-dir",
            paths["bridge_state"],
            "--limit",
            str(limits["gateway_max_candidates"]),
            "--schema-version",
            "2",
            "sync",
        ],
        {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        timeouts["bridge_seconds"],
    )
    return (
        {"status": "completed"}
        if value.get("status") in {"idle", "delivered"}
        else {"status": "operator_required"}
    )


def editorial_stage(config: dict[str, Any], export: Path) -> dict[str, object]:
    paths, limits, timeouts = config["paths"], config["limits"], config["timeouts"]
    value = _stage_result(
        [
            paths["editorial"],
            "--config",
            paths["editorial_config"],
            "--inbox",
            str(Path(paths["bridge_workspace"]) / "waagd-discovery" / "inbox.json"),
            "--store",
            paths["editorial_store"],
            "--export",
            str(export),
            "--workspace-root",
            paths["bridge_workspace"],
            "--state-dir",
            paths["bridge_state"],
            "--no-simulation",
            "--allow-ack",
            "--allow-gateway",
            "--gateway-executable",
            paths["gateway"],
            "--gateway-agent",
            "waagd",
            "--gateway-timeout-seconds",
            str(timeouts["gateway_seconds"]),
            "--gateway-max-candidates",
            str(limits["gateway_max_candidates"]),
            "--gateway-max-cost-usd",
            str(limits["gateway_max_cost_usd"]),
            "--trusted-publishers",
            paths["trusted_publishers"],
            "gateway-process",
        ],
        {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": paths["gateway_home"],
            "OPENCLAW_CONFIG_PATH": paths["gateway_config"],
            "OPENCLAW_STATE_DIR": paths["gateway_state"],
        },
        timeouts["editorial_seconds"],
    )
    return (
        {"status": "completed"}
        if value.get("status") in {"acknowledged", "inbox_empty", "inbox_missing"}
        else {"status": "operator_required"}
    )


def cycle(
    config: dict[str, Any], config_path: Path, cycle_id: str | None, local_date: str | None
) -> dict[str, object]:
    identifier = cycle_id or os.environ.get("WAAGD_AUTONOMOUS_CYCLE_ID") or str(uuid4())
    date = (
        local_date
        or os.environ.get("WAAGD_AUTONOMOUS_LOCAL_DATE")
        or datetime.now(UTC).date().isoformat()
    )
    export = snapshot(config, identifier, date)
    paths, timeouts = config["paths"], config["timeouts"]
    # Stages receive no ambient secrets. The editor stage reloads the same
    # private JSON and constructs its own isolated Gateway environment.
    stage_bridge = shlex.join(
        (paths["staging"], "--config", str(config_path), "bridge", "--export", str(export))
    )
    stage_editorial = shlex.join(
        (paths["staging"], "--config", str(config_path), "editorial", "--export", str(export))
    )
    argv = [
        paths["autonomous_cycle"],
        "--stage-command",
        stage_bridge,
        "--stage-timeout-seconds",
        str(timeouts["bridge_seconds"]),
        "--stage-command",
        stage_editorial,
        "--stage-timeout-seconds",
        str(timeouts["editorial_seconds"]),
        "--cycle-timeout-seconds",
        str(timeouts["cycle_seconds"]),
        "--editorial-store",
        paths["editorial_store"],
        "--publication-output",
        paths["publication_output"],
        "--publication-policy",
        paths["publication_policy"],
        "--site-executable",
        paths["site_node"],
        "--site-argument=" + "--env-file=" + paths["site_environment_file"],
        "--site-argument=" + paths["site_command"],
        "--site-working-directory",
        paths["site_working_directory"],
        "--ingress-token-file",
        paths["ingress_token_file"],
    ]
    if not _active(config):
        argv.append("--site-paused")
    return _stage_result(argv, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, timeouts["cycle_seconds"])
