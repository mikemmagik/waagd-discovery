"""Manual, machine-readable operational interface."""

import argparse
import json
import os
import signal
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from types import FrameType

import jsonschema

from waagd_discovery.config import load_sources
from waagd_discovery.export import export_candidates
from waagd_discovery.http import HttpClient
from waagd_discovery.locking import database_lock
from waagd_discovery.logging import configure
from waagd_discovery.models import DiscoveryError
from waagd_discovery.operations import restore, snapshot
from waagd_discovery.pipeline import collect
from waagd_discovery.storage import Repository


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="WAAGD RSS/Atom discovery — manual operation")
    result.add_argument(
        "--db", type=Path, default=Path(os.getenv("WAAGD_DB", "/data/discovery.sqlite3"))
    )
    result.add_argument(
        "--sources", type=Path, default=Path(os.getenv("WAAGD_SOURCES", "config/sources.yaml"))
    )
    result.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("init-db", "stats", "verify"):
        commands.add_parser(name)
    collection = commands.add_parser("collect")
    collection.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Required: no automatic scheduling is available",
    )
    collection.add_argument("--source")
    collection.add_argument("--dry-run", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--limit", type=int, default=50)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    restoration = commands.add_parser("restore")
    restoration.add_argument("--input", type=Path, required=True)
    restoration.add_argument("--confirm", action="store_true", required=True)
    return result


def interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if args.command == "export":
        protected = {args.db.resolve(), args.sources.resolve()}
        protected.update(
            Path(str(args.db) + suffix).resolve() for suffix in ("-wal", "-shm", ".lock")
        )
        if args.output.resolve() in protected:
            raise DiscoveryError("export_target_protected")
    if args.command == "restore":
        with database_lock(args.db):
            previous = restore(args.db, args.input)
        return {"status": "restored", "pre_restore_backup": previous.name if previous else None}, 0
    if args.command == "collect":
        sources = load_sources(args.sources)
        if args.source and not any(s.id == args.source and s.enabled for s in sources):
            raise DiscoveryError("source_unknown_or_disabled")
        if not any(s.enabled for s in sources):
            raise DiscoveryError("no_enabled_sources")
        if args.dry_run:
            context = snapshot(args.db) if args.db.exists() else Repository(":memory:")
        else:
            context = Repository(args.db)
        with database_lock(args.db) if not args.dry_run else nullcontext():
            with context as repository, HttpClient() as client:
                result = collect(repository, sources, client, args.source)
                result["dry_run"] = args.dry_run
        status = result["status"]
        return result, 130 if status == "interrupted" else 0 if status == "success" else 1
    if args.command == "init-db":
        with database_lock(args.db), Repository(args.db) as repository:
            repository.sync_sources(load_sources(args.sources))
            repository.verify()
        return {"status": "initialized"}, 0
    with snapshot(args.db) as repository:
        if args.command == "stats":
            return repository.stats(), 0
        if args.command == "verify":
            repository.verify()
            return {"status": "ok"}, 0
        if args.command == "export":
            count = export_candidates(repository, args.output, args.limit)
            return {"status": "exported", "candidates": count}, 0
        if args.command == "backup":
            repository.backup(args.output)
            return {"status": "backed_up"}, 0
    raise DiscoveryError("command_invalid")


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    configure(args.log_level)
    previous_handler = signal.signal(signal.SIGTERM, interrupt)
    try:
        result, code = execute(args)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return code
    except DiscoveryError as exc:
        print(json.dumps({"status": "error", "code": exc.code}), file=sys.stderr)
        return 2 if exc.code.startswith(("registry_", "source_", "no_enabled")) else 1
    except (OSError, sqlite3.Error, ValueError, jsonschema.ValidationError):
        print(json.dumps({"status": "error", "code": "operation_failed"}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}), file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())
