"""Explicit one-shot editorial commands; never installs an operator service."""

import argparse
import json
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from waagd_discovery.bridge import Bridge, io
from waagd_discovery.editorial.adapter import MockEditor
from waagd_discovery.editorial.contracts import EditorAdapter, EditorialError, Verifier
from waagd_discovery.editorial.controller import Controller


@dataclass(frozen=True)
class EditorialConfig:
    enabled: bool = False
    network_enabled: bool = False
    llm_enabled: bool = False
    auto_ack: bool = False
    publishing_enabled: bool = False


class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader: _Loader, node: yaml.MappingNode) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)  # type: ignore[no-untyped-call]
        if not isinstance(key, str) or key in result:
            raise EditorialError("editorial_config_invalid")
        result[key] = loader.construct_object(value_node)  # type: ignore[no-untyped-call]
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_config(path: Path | None = None) -> EditorialConfig:
    if path is None:
        return EditorialConfig()
    try:
        directory = io.open_directory(path.parent)
        try:
            raw = io.read_file(directory, path.name, 65536)
        finally:
            import os

            os.close(directory)
        data = yaml.load(raw.decode("utf-8"), Loader=_Loader)
        keys = {
            "editorial": ("enabled", "enabled"),
            "network": ("enabled", "network_enabled"),
            "llm": ("enabled", "llm_enabled"),
            "bridge": ("auto_ack", "auto_ack"),
            "publishing": ("enabled", "publishing_enabled"),
        }
        if not isinstance(data, dict) or set(data) - keys.keys():
            raise EditorialError("editorial_config_invalid")
        values = {}
        for section, value in data.items():
            allowed, field = keys[section]
            if (
                not isinstance(value, dict)
                or set(value) != {allowed}
                or type(value[allowed]) is not bool
            ):
                raise EditorialError("editorial_config_invalid")
            values[field] = value[allowed]
        config = EditorialConfig(**values)
        if config.auto_ack or config.publishing_enabled:
            raise EditorialError("editorial_feature_not_authorized")
        return config
    except (OSError, io.BridgeError, UnicodeError, yaml.YAMLError, RecursionError):
        raise EditorialError("editorial_config_unreadable") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waagd-editorial")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--simulation", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--allow-ack", action="store_true")
    parser.add_argument("--allow-gateway", action="store_true")
    parser.add_argument("--gateway-executable", type=Path)
    parser.add_argument("--gateway-agent", default="waagd")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("dry-run")
    commands.add_parser("process")
    commands.add_parser("gateway-process")
    commands.add_parser("status")
    reconsider = commands.add_parser("reconsider")
    reconsider.add_argument("--candidate-key", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        gateway = args.command == "gateway-process"
        if (args.allow_ack and not gateway) or (
            not args.simulation and args.command != "status" and not gateway
        ):
            raise EditorialError("real_editor_unavailable")
        if gateway and (
            args.simulation
            or not args.allow_gateway
            or args.gateway_executable is None
            or not config.enabled
            or not config.network_enabled
            or not config.llm_enabled
        ):
            raise EditorialError("gateway_processing_not_authorized")
        if args.inbox != args.workspace_root / "waagd-discovery" / "inbox.json":
            raise EditorialError("inbox_path_mismatch")
        bridge = Bridge(args.export, args.workspace_root, args.state_dir)
        protected = (args.workspace_root, args.export.parent, args.state_dir)

        def controller(store: Path) -> Controller:
            from waagd_discovery.editorial.verifier import SyntheticVerifier

            verifier: Verifier
            editor: EditorAdapter
            if gateway:
                from waagd_discovery.editorial.gateway_adapter import GatewayEditor
                from waagd_discovery.editorial.verifier import SourceVerifier

                verifier = SourceVerifier()
                editor = GatewayEditor(args.gateway_executable, agent_id=args.gateway_agent)
            else:
                verifier = SyntheticVerifier()
                editor = MockEditor()
            return Controller(
                args.inbox,
                store,
                bridge,
                verifier,
                editor,
                simulation=args.simulation,
                allow_ack=args.allow_ack and gateway,
                protected_roots=protected,
            )

        if args.command == "dry-run":
            with tempfile.TemporaryDirectory(prefix="waagd-editorial-dry-") as temporary:
                # macOS tempfile paths commonly begin with the /var symlink;
                # canonicalize only this trusted freshly-created scratch root.
                result = controller(
                    Path(temporary).resolve() / "store" / "editorial.sqlite3"
                ).process()
            result["durable_results"] = False
        else:
            if args.command in {"process", "reconsider"} and not config.enabled:
                raise EditorialError("editorial_disabled")
            if args.command in {"process", "reconsider"} and config.network_enabled:
                raise EditorialError("real_processing_unavailable")
            instance = controller(args.store)
            if args.command == "status":
                result = instance.status()
            elif args.command == "reconsider":
                result = instance.reconsider(args.candidate_key)
            else:
                result = instance.process()
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except EditorialError as error:
        print(json.dumps({"status": "error", "error": str(error)}, sort_keys=True))
        return 1
    except (io.BridgeError, OSError, sqlite3.Error, ValueError, TypeError):
        print(
            json.dumps({"status": "error", "error": "editorial_operation_failed"}, sort_keys=True)
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
