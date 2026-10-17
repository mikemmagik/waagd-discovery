#!/usr/bin/env python3
"""Reject high-signal credentials and runtime data in Git-tracked source.

Only paths, line numbers and rule names are printed; never matched values.
This is a small preventive check, not a substitute for revoking leaked secrets
or reviewing Git history. No external scanner or network access is required.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "openai-openrouter-key": re.compile(r"\bsk-(?:or-v1-|proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b"),
    "google-api-key": re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    "telegram-bot-token": re.compile(r"\b[0-9]{7,12}:AA[A-Za-z0-9_-]{30,}\b"),
    "credential-url": re.compile(r"https?://[^\s/@:'\"]+:[^\s/@'\"]{16,}@", re.IGNORECASE),
    "provider-secret-assignment": re.compile(
        r"\b(?:OPENROUTER_API_KEY|OPENAI_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY|"
        r"CLOUDFLARE_API_TOKEN|TELEGRAM_BOT_TOKEN|GITHUB_TOKEN|GH_TOKEN)"
        r"\s*[:=]\s*[\"'](?!\$|<|example|placeholder|dummy|fake|test|replace|changeme)"
        r"[A-Za-z0-9_./+=:-]{20,}[\"']",
        re.IGNORECASE,
    ),
}
RUNTIME_DIRECTORIES = {"data", "exports", "backups"}


def path_violation(path: str) -> str | None:
    """Return a rule name for files that never belong in source control."""
    parts = Path(path).parts
    name = Path(path).name.lower()
    if parts and parts[0] in RUNTIME_DIRECTORIES:
        return "runtime-data-directory"
    if name != ".env.example" and (name == ".env" or name.startswith(".env.")):
        return "environment-file"
    if re.search(r"\.(?:sqlite(?:3)?|db)(?:-(?:wal|shm|journal))?$", name):
        return "database-file"
    if name in {"id_rsa", "id_ed25519", "credentials.json", "token.json"}:
        return "credential-file"
    return None


def content_violations(content: str) -> list[tuple[int, str]]:
    """Inspect source without retaining or returning secret values."""
    return [
        (number, rule)
        for number, line in enumerate(content.splitlines(), start=1)
        for rule, pattern in PATTERNS.items()
        if pattern.search(line)
    ]


def main() -> int:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        print("Cannot enumerate tracked source files.", file=sys.stderr)
        return 2

    failures: list[str] = []
    paths = [path for path in result.stdout.decode("utf-8").split("\0") if path]
    for path in paths:
        if violation := path_violation(path):
            failures.append(f"{path}: {violation}")
        candidate = ROOT / path
        if candidate.is_symlink():
            failures.append(f"{path}: tracked-symlink (not followed)")
            continue
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            failures.append(f"{path}: unreadable-tracked-file")
            continue
        failures.extend(f"{path}:{number}: {rule}" for number, rule in content_violations(content))

    if failures:
        print("Tracked source safety check failed:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Tracked source safety check passed ({len(paths)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
