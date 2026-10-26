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
from waagd_discovery.editorial.contracts import (
    EditorAdapter,
    EditorialError,
    Verifier,
    validate_input,
)
from waagd_discovery.editorial.controller import Controller
from waagd_discovery.editorial.corroboration import CorroborationClaim, CorroborationPolicy
from waagd_discovery.editorial.trusted_publishers import load_trusted_publishers


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


def load_corroboration_rules(path: Path) -> dict[str, CorroborationPolicy]:
    """Read a private, independently-maintained allowlist; never derive it from news."""
    try:
        directory = io.open_directory(path.parent, private=True)
        try:
            raw = io.read_file(directory, path.name, 65536, private=True)
        finally:
            import os

            os.close(directory)
        data = yaml.load(raw.decode("utf-8"), Loader=_Loader)
        if not isinstance(data, dict) or set(data) != {"corroboration"}:
            raise EditorialError("corroboration_policy_invalid")
        body = data["corroboration"]
        if not isinstance(body, dict) or set(body) != {"policies"}:
            raise EditorialError("corroboration_policy_invalid")
        entries = body.get("policies", [])
        if not isinstance(entries, list) or len(entries) > 50:
            raise EditorialError("corroboration_policy_invalid")
        policies: dict[str, CorroborationPolicy] = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "candidate_key",
                "trusted_independent_hosts",
                "claims",
            }:
                raise EditorialError("corroboration_policy_invalid")
            key, hosts, claims = (
                entry["candidate_key"],
                entry["trusted_independent_hosts"],
                entry["claims"],
            )
            if (
                not isinstance(key, str)
                or not isinstance(hosts, list)
                or not isinstance(claims, list)
                or any(not isinstance(host, str) for host in hosts)
            ):
                raise EditorialError("corroboration_policy_invalid")
            parsed_claims: list[CorroborationClaim] = []
            for claim in claims:
                if not isinstance(claim, dict) or set(claim) != {"claim", "independent_url"}:
                    raise EditorialError("corroboration_policy_invalid")
                parsed_claims.append(
                    CorroborationClaim(
                        claim=claim["claim"], independent_url=claim["independent_url"]
                    )
                )
            if key in policies:
                raise EditorialError("corroboration_policy_invalid")
            policies[key] = CorroborationPolicy(
                candidate_key=key,
                trusted_independent_hosts=frozenset(hosts),
                claims=tuple(parsed_claims),
            )
        if not policies:
            raise EditorialError("corroboration_policy_invalid")
        return policies
    except (OSError, io.BridgeError, UnicodeError, yaml.YAMLError, RecursionError):
        raise EditorialError("corroboration_policy_unreadable") from None


def load_corroboration_policies(path: Path) -> dict[str, CorroborationPolicy]:
    """Compatibility helper for focused policy validation callers."""
    return load_corroboration_rules(path)


def _check_gateway_batch(inbox: Path, maximum: int) -> None:
    """Bound calls before opening any network transport or Gateway process."""
    try:
        directory = io.open_directory(inbox.parent, private=True)
        try:
            value = io.decode(io.read_file(directory, inbox.name, 128 * 1024, private=True))
        finally:
            import os

            os.close(directory)
        if not isinstance(value, dict):
            raise EditorialError("inbox_invalid")
        validate_input(value)
        if len(value["candidates"]) > maximum:
            raise EditorialError("gateway_candidate_limit")
    except (OSError, io.BridgeError):
        raise EditorialError("gateway_inbox_unreadable") from None


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
    parser.add_argument("--gateway-timeout-seconds", type=float, default=120)
    parser.add_argument("--gateway-max-candidates", type=int, default=1)
    parser.add_argument("--gateway-max-cost-usd", type=float)
    parser.add_argument("--corroboration-policy", type=Path)
    parser.add_argument("--trusted-publishers", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("dry-run")
    commands.add_parser("process")
    commands.add_parser("gateway-process")
    retry_selection = commands.add_parser("retry-selection")
    retry_selection.add_argument("--delivery-id", required=True)
    retry_selection.add_argument("--operator-confirm", action="store_true")
    commands.add_parser("status")
    reconsider = commands.add_parser("reconsider")
    reconsider.add_argument("--candidate-key", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        gateway = args.command in {"gateway-process", "retry-selection"}
        if (args.allow_ack and not gateway) or (
            not args.simulation and args.command != "status" and not gateway
        ):
            raise EditorialError("real_editor_unavailable")
        if gateway and (
            args.simulation
            or not args.allow_gateway
            or args.gateway_executable is None
            or (args.corroboration_policy is None and args.trusted_publishers is None)
            or args.gateway_max_cost_usd is None
            or not config.enabled
            or not config.network_enabled
            or not config.llm_enabled
        ):
            raise EditorialError("gateway_processing_not_authorized")
        if args.command == "retry-selection" and not args.operator_confirm:
            raise EditorialError("selection_retry_confirmation_required")
        maximum_gateway_candidates = 50 if args.trusted_publishers else 10
        if gateway and (
            isinstance(args.gateway_max_candidates, bool)
            or not 1 <= args.gateway_max_candidates <= maximum_gateway_candidates
        ):
            raise EditorialError("gateway_candidate_limit")
        if gateway and not 0 < args.gateway_max_cost_usd <= 1:
            raise EditorialError("gateway_cost_limit")
        if args.inbox != args.workspace_root / "waagd-discovery" / "inbox.json":
            raise EditorialError("inbox_path_mismatch")
        bridge = Bridge(args.export, args.workspace_root, args.state_dir)
        protected = (args.workspace_root, args.export.parent, args.state_dir)

        def controller(store: Path) -> Controller:
            from waagd_discovery.editorial.verifier import SyntheticVerifier

            verifier: Verifier
            editor: EditorAdapter
            selector = None
            fidelity_reviewer = None
            if gateway:
                from waagd_discovery.editorial.fidelity import GatewayFidelityReviewer
                from waagd_discovery.editorial.gateway_adapter import (
                    GatewayBudgetPolicy,
                    GatewayEditor,
                )
                from waagd_discovery.editorial.verifier import SourceVerifier

                rules = (
                    load_corroboration_rules(args.corroboration_policy)
                    if args.corroboration_policy
                    else {}
                )
                trusted = (
                    load_trusted_publishers(args.trusted_publishers)
                    if args.trusted_publishers
                    else None
                )
                verifier = SourceVerifier(corroboration_policies=rules, trusted_publishers=trusted)
                editor = GatewayEditor(
                    args.gateway_executable,
                    agent_id=args.gateway_agent,
                    timeout=args.gateway_timeout_seconds,
                    budget_policy=GatewayBudgetPolicy(
                        max_reported_cost_usd=args.gateway_max_cost_usd
                    ),
                    required_model_route="openrouter/openrouter/free",
                )
                fidelity_reviewer = GatewayFidelityReviewer(editor)
                if args.trusted_publishers:
                    from waagd_discovery.editorial.selection_gateway import GatewaySelector

                    selector = GatewaySelector(editor)
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
                selector=selector,
                fidelity_reviewer=fidelity_reviewer,
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
            elif args.command == "retry-selection":
                _check_gateway_batch(args.inbox, args.gateway_max_candidates)
                result = instance.retry_selection(args.delivery_id)
            else:
                if gateway:
                    _check_gateway_batch(args.inbox, args.gateway_max_candidates)
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
