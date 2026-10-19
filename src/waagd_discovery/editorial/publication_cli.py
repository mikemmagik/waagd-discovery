"""Offline exporter for the trusted editorial-to-publisher hand-off."""

import argparse
import json
import sqlite3
from pathlib import Path

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.publication import export_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waagd-publication-export")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(export_file(args.store, args.output, args.limit), sort_keys=True))
        return 0
    except EditorialError as error:
        print(json.dumps({"status": "error", "error": str(error)}, sort_keys=True))
        return 1
    except (io.BridgeError, OSError, sqlite3.Error, ValueError, TypeError):
        print(json.dumps({"status": "error", "error": "publication_export_failed"}, sort_keys=True))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
