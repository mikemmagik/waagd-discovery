"""Configured one-shot process used by ``waagd-autonomous run-due``."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from waagd_discovery.autonomous_cycle import AutonomousCycle, TrustedStageCommand
from waagd_discovery.editorial.delivery import PublicationDelivery
from waagd_discovery.editorial.publication_policy import load_publication_policy
from waagd_discovery.editorial.site_import import (
    CommandSiteImporter,
    PublishImportOutcomeParser,
    SiteImportCommand,
)
from waagd_discovery.models import DiscoveryError


def _command(value: str) -> TrustedStageCommand:
    parts = tuple(shlex.split(value))
    if not parts:
        raise DiscoveryError("autonomous_stage_invalid")
    return TrustedStageCommand(Path(parts[0]), parts[1:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waagd-autonomous-cycle")
    parser.add_argument("--stage-command", action="append", required=True)
    parser.add_argument("--editorial-store", type=Path, required=True)
    parser.add_argument("--publication-output", type=Path, required=True)
    parser.add_argument("--publication-policy", type=Path, required=True)
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
            parser=PublishImportOutcomeParser(),
        )
        delivery = PublicationDelivery(
            args.editorial_store,
            args.publication_output,
            load_publication_policy(args.publication_policy),
            importer,
        )
        stages = tuple(_command(item) for item in args.stage_command)
        result = AutonomousCycle(stages, delivery).run()
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (DiscoveryError, OSError, ValueError) as error:
        code = error.code if isinstance(error, DiscoveryError) else "autonomous_cycle_failed"
        print(json.dumps({"status": "operator_required", "code": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
