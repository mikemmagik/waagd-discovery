"""Configured one-shot process used by ``waagd-autonomous run-due``."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path

from waagd_discovery.autonomous_cycle import AutonomousCycle, TrustedStageCommand
from waagd_discovery.editorial.delivery import PublicationDelivery
from waagd_discovery.editorial.publication_policy import load_publication_policy
from waagd_discovery.editorial.site_import import (
    CommandSiteImporter,
    PublishImportOutcomeParser,
    SiteImportCommand,
    StagingReceiptOutcomeParser,
)
from waagd_discovery.models import DiscoveryError


def _command(value: str, timeout_seconds: int, *, side_effecting: bool) -> TrustedStageCommand:
    parts = tuple(shlex.split(value))
    if not parts:
        raise DiscoveryError("autonomous_stage_invalid")
    return TrustedStageCommand(Path(parts[0]), parts[1:], timeout_seconds, side_effecting)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waagd-autonomous-cycle")
    parser.add_argument("--stage-command", action="append", required=True)
    parser.add_argument("--stage-timeout-seconds", action="append", type=int)
    parser.add_argument("--read-only-stage-index", action="append", type=int, default=[])
    parser.add_argument("--cycle-timeout-seconds", type=int)
    parser.add_argument("--editorial-store", type=Path, required=True)
    parser.add_argument("--publication-output", type=Path, required=True)
    parser.add_argument("--publication-policy", type=Path, required=True)
    parser.add_argument("--site-paused", action="store_true")
    parser.add_argument("--staging-origin")
    parser.add_argument("--staging-state-id")
    parser.add_argument("--site-executable", type=Path, required=True)
    parser.add_argument("--site-argument", action="append", default=[])
    parser.add_argument("--site-working-directory", type=Path, required=True)
    parser.add_argument("--ingress-token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        importer = CommandSiteImporter(
            SiteImportCommand(
                args.site_executable,
                tuple(args.site_argument),
                working_directory=args.site_working_directory,
            ),
            args.ingress_token_file,
            parser=(
                StagingReceiptOutcomeParser(args.staging_origin, args.staging_state_id)
                if args.staging_origin
                else PublishImportOutcomeParser()
            ),
        )
        policy = load_publication_policy(args.publication_policy)
        if args.site_paused:
            policy = replace(policy, automatic_enabled=False)
        delivery = PublicationDelivery(
            args.editorial_store,
            args.publication_output,
            policy,
            importer,
        )
        timeouts = args.stage_timeout_seconds or [10_800] * len(args.stage_command)
        if len(timeouts) != len(args.stage_command):
            raise DiscoveryError("autonomous_stage_timeout_count_invalid")
        read_only = set(args.read_only_stage_index)
        if any(index < 1 or index > len(args.stage_command) for index in read_only):
            raise DiscoveryError("autonomous_stage_invalid")
        stages = tuple(
            _command(
                item,
                timeouts[index],
                side_effecting=index + 1 not in read_only,
            )
            for index, item in enumerate(args.stage_command)
        )
        result = AutonomousCycle(stages, delivery, timeout_seconds=args.cycle_timeout_seconds).run()
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (DiscoveryError, OSError, ValueError) as error:
        code = error.code if isinstance(error, DiscoveryError) else "autonomous_cycle_failed"
        print(json.dumps({"status": "operator_required", "code": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
