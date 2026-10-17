"""Shared one-shot orchestration for manual and scheduled discovery."""

from contextlib import nullcontext
from pathlib import Path

from waagd_discovery.config import load_sources
from waagd_discovery.filtering import assess, load_policy
from waagd_discovery.http import HttpClient
from waagd_discovery.lifecycle import recover_runs
from waagd_discovery.locking import database_lock
from waagd_discovery.models import DiscoveryError
from waagd_discovery.operations import snapshot
from waagd_discovery.pipeline import collect
from waagd_discovery.storage import Repository


def run_once(
    db: Path,
    sources_path: Path,
    policy_path: Path,
    *,
    selected: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    sources = load_sources(sources_path)
    policy = load_policy(policy_path)
    if selected and not any(s.id == selected and s.enabled for s in sources):
        raise DiscoveryError("source_unknown_or_disabled")
    if not any(s.enabled for s in sources):
        raise DiscoveryError("no_enabled_sources")
    context = snapshot(db) if dry_run and db.exists() else Repository(":memory:" if dry_run else db)
    with database_lock(db) if not dry_run else nullcontext():
        with context as repository, HttpClient() as client:
            recovered = recover_runs(repository)
            result = collect(repository, sources, client, selected)
            if result["status"] in ("success", "partial"):
                result["filter"] = assess(repository, policy)
            result["dry_run"] = dry_run
            result["recovered_runs"] = recovered
    return result
