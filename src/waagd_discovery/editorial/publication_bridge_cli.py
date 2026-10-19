"""No-network operator entrypoint for the publication hand-off."""

import argparse
import json
from pathlib import Path

from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.publication_bridge import transfer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waagd-publication-bridge")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(transfer(args.source, args.state_directory), sort_keys=True))
        return 0
    except EditorialError as error:
        print(json.dumps({"status": "error", "error": str(error)}, sort_keys=True))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"status": "error", "error": "publication_bridge_failed"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
