"""Manual, machine-readable operational interface."""

import argparse
import json
import os
import signal
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import jsonschema

from waagd_discovery.backups import (
    create_managed_backup,
    load_retention_policy,
    rotate_backups,
)
from waagd_discovery.collection import run_once
from waagd_discovery.config import load_sources
from waagd_discovery.diagnostics import service_status
from waagd_discovery.export import export_candidates
from waagd_discovery.filtering import assess, filter_report, load_policy
from waagd_discovery.locking import database_lock
from waagd_discovery.logging import configure
from waagd_discovery.models import DiscoveryError
from waagd_discovery.operations import restore, snapshot
from waagd_discovery.storage import Repository


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="WAAGD RSS/Atom discovery — manual and opt-in scheduled operation"
    )
    result.add_argument(
        "--db", type=Path, default=Path(os.getenv("WAAGD_DB", "/data/discovery.sqlite3"))
    )
    result.add_argument(
        "--sources", type=Path, default=Path(os.getenv("WAAGD_SOURCES", "config/sources.yaml"))
    )
    result.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    result.add_argument(
        "--policy", type=Path, default=Path(os.getenv("WAAGD_FILTER", "config/filter.yaml"))
    )
    result.add_argument(
        "--backup-policy",
        type=Path,
        default=Path(os.getenv("WAAGD_BACKUP_POLICY", "config/backup.yaml")),
    )
    result.add_argument(
        "--scheduler-config",
        type=Path,
        default=Path(os.getenv("WAAGD_SCHEDULER_CONFIG", "config/scheduler.yaml")),
    )
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("init-db", "stats", "verify", "scheduler", "status", "health"):
        commands.add_parser(name)
    collection = commands.add_parser("collect")
    collection.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Required for an independent one-shot collection",
    )
    collection.add_argument("--source")
    collection.add_argument("--dry-run", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--limit", type=int, default=50)
    export.add_argument("--at", type=reference_time)
    filtering = commands.add_parser("filter", help="Persist a complete explainable filter batch")
    filtering.add_argument("--at", type=reference_time)
    report = commands.add_parser("filter-report", help="Inspect the latest persisted decisions")
    report.add_argument("--limit", type=int, default=100)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    managed = commands.add_parser(
        "backup-managed", help="Create a verified backup and rotate safely"
    )
    managed.add_argument("--directory", type=Path, required=True)
    rotation = commands.add_parser("backup-rotate", help="Preview managed backup retention")
    rotation.add_argument("--directory", type=Path, required=True)
    rotation.add_argument("--apply", action="store_true")
    restoration = commands.add_parser("restore")
    restoration.add_argument("--input", type=Path, required=True)
    restoration.add_argument("--confirm", action="store_true", required=True)
    return result


def reference_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("Expected an ISO8601 timestamp with UTC offset") from exc


def interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if args.command == "export":
        protected = {
            args.db.resolve(),
            args.sources.resolve(),
            args.policy.resolve(),
            args.backup_policy.resolve(),
            args.scheduler_config.resolve(),
        }
        protected.update(
            Path(str(args.db) + suffix).resolve()
            for suffix in ("-wal", "-shm", ".lock", ".scheduler.lock")
        )
        if args.output.resolve() in protected or args.output.suffix.lower() in (
            ".sqlite",
            ".sqlite3",
            ".db",
        ):
            raise DiscoveryError("export_target_protected")
    if args.command in ("backup-managed", "backup-rotate"):
        if args.directory.resolve() == args.db.resolve().parent:
            raise DiscoveryError("backup_directory_contains_database")
        retention = load_retention_policy(args.backup_policy)
        if args.command == "backup-rotate":
            rotation = rotate_backups(args.directory, retention, apply=args.apply)
            return rotation, 1 if rotation["status"] == "blocked" else 0
        with snapshot(args.db) as repository:
            managed = create_managed_backup(repository, args.directory, retention)
            managed_rotation = managed["rotation"]
            assert isinstance(managed_rotation, dict)
            return managed, 1 if managed_rotation["status"] == "blocked" else 0
    if args.command == "restore":
        with database_lock(args.db):
            previous = restore(args.db, args.input)
        return {"status": "restored", "pre_restore_backup": previous.name if previous else None}, 0
    if args.command in ("status", "health"):
        status_result = service_status(args.db)
        return status_result, 0 if args.command == "status" or status_result["healthy"] else 1
    if args.command == "scheduler":
        from waagd_discovery.scheduler import load_scheduler_policy, run_scheduler

        state = run_scheduler(
            args.db, args.sources, args.policy, load_scheduler_policy(args.scheduler_config)
        )
        return state, 0
    if args.command == "collect":
        result = run_once(
            args.db, args.sources, args.policy, selected=args.source, dry_run=args.dry_run
        )
        status = result["status"]
        return result, 130 if status == "interrupted" else 0 if status == "success" else 1
    if args.command == "init-db":
        with database_lock(args.db), Repository(args.db) as repository:
            repository.sync_sources(load_sources(args.sources))
            repository.verify()
        return {"status": "initialized"}, 0
    if args.command == "filter":
        sources = load_sources(args.sources)
        policy = load_policy(args.policy)
        if not args.db.is_file():
            raise DiscoveryError("database_missing")
        with database_lock(args.db), Repository(args.db) as repository:
            repository.sync_sources(sources)
            return assess(repository, policy, args.at), 0
    with snapshot(args.db) as repository:
        if args.command == "stats":
            return repository.stats(), 0
        if args.command == "verify":
            repository.verify()
            return {"status": "ok"}, 0
        if args.command == "export":
            repository.sync_sources(load_sources(args.sources))
            count = export_candidates(
                repository, args.output, args.limit, policy=load_policy(args.policy), as_of=args.at
            )
            return {"status": "exported", "candidates": count}, 0
        if args.command == "filter-report":
            return filter_report(repository, args.limit), 0
        if args.command == "backup":
            repository.backup(args.output)
            return {"status": "backed_up"}, 0
    raise DiscoveryError("command_invalid")


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    configure(args.log_level)
    previous_handler = signal.signal(signal.SIGTERM, interrupt)
    previous_int = signal.signal(signal.SIGINT, interrupt)
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
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    sys.exit(main())
