"""Explicit one-shot, mock-only RC commands; never installs an operator service."""

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
from waagd_discovery.editorial.contracts import EditorialError
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
        if config.llm_enabled or config.auto_ack or config.publishing_enabled:
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
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("dry-run")
    commands.add_parser("process")
    commands.add_parser("status")
    reconsider = commands.add_parser("reconsider")
    reconsider.add_argument("--candidate-key", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.allow_ack or (not args.simulation and args.command != "status"):
            raise EditorialError("real_editor_unavailable")
        if args.inbox != args.workspace_root / "waagd-discovery" / "inbox.json":
            raise EditorialError("inbox_path_mismatch")
        bridge = Bridge(args.export, args.workspace_root, args.state_dir)
        protected = (args.workspace_root, args.export.parent, args.state_dir)

        def controller(store: Path) -> Controller:
            # Lazy import: verifier is owned independently and network is never
            # instantiated or used by this mock-only RC CLI.
            from waagd_discovery.editorial.verifier import SyntheticVerifier

            return Controller(
                args.inbox,
                store,
                bridge,
                SyntheticVerifier(),
                MockEditor(),
                simulation=args.simulation,
                allow_ack=False,
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
