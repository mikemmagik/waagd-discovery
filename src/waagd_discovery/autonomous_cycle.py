"""One-shot trusted composition of Bridge/editorial stages and Site delivery.

Each upstream stage is an already-configured absolute command.  The coordinator
never invents an RSS, Gateway, or Bridge configuration and gives those processes
no Site credential.  The final delivery step alone receives the token *file*
through the isolated Site importer.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from waagd_discovery.editorial.delivery import PublicationDelivery
from waagd_discovery.models import DiscoveryError

_MAX_STAGE_OUTPUT = 64 * 1024
CycleStatus = Literal["completed", "retryable", "operator_required"]


@dataclass(frozen=True)
class TrustedStageCommand:
    """An owner-configured command with no shell or inherited environment."""

    executable: Path
    arguments: tuple[str, ...] = ()
    timeout_seconds: int = 900

    def __post_init__(self) -> None:
        try:
            resolved = self.executable.resolve(strict=True)
            info = resolved.stat()
            valid = (
                self.executable.is_absolute()
                and stat.S_ISREG(info.st_mode)
                and not info.st_mode & 0o022
                and os.access(resolved, os.X_OK)
            )
        except OSError:
            valid = False
        if (
            not valid
            or not isinstance(self.arguments, tuple)
            or any(
                not isinstance(item, str) or not item or "\x00" in item for item in self.arguments
            )
            or isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or not 1 <= self.timeout_seconds <= 3600
        ):
            raise DiscoveryError("autonomous_stage_invalid")

    def run(self) -> CycleStatus:
        try:
            result = subprocess.run(
                (str(self.executable.resolve()), *self.arguments),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "retryable"
        if result.returncode != 0:
            return "retryable"
        if len(result.stdout) > _MAX_STAGE_OUTPUT:
            return "operator_required"
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return "operator_required"
        if not isinstance(value, dict):
            return "operator_required"
        status = value.get("status")
        if status == "operator_required":
            return "operator_required"
        if status == "retryable":
            return "retryable"
        return "completed"


class AutonomousCycle:
    """Run the configured upstream chain then deliver approved intents."""

    def __init__(
        self, stages: tuple[TrustedStageCommand, ...], delivery: PublicationDelivery
    ) -> None:
        if not stages:
            raise DiscoveryError("autonomous_stages_missing")
        self.stages = stages
        self.delivery = delivery

    def run(self) -> dict[str, object]:
        for stage in self.stages:
            status = stage.run()
            if status != "completed":
                return {"status": status, "code": "upstream_stage_" + status}
        return self.delivery.run()
