"""Conservative boundary for the future Site ``publish-import`` command.

This module intentionally owns process isolation only.  The Site team's final
machine-readable receipt is not yet agreed, so a caller must supply the small
``SiteOutcomeParser`` policy which interprets a completed command.  The default
policy classifies every completed command as ``uncertain``: an exit status or
untrusted output alone cannot prove that a website write did not happen.
"""

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from waagd_discovery.bridge import io
from waagd_discovery.editorial.contracts import EditorialError
from waagd_discovery.editorial.publication import validate_publication_intent

SiteOutcomeStatus = Literal["created", "duplicate", "held", "failed", "uncertain"]
_SAFE_RECEIPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAXIMUM_OUTPUT = 64 * 1024
_MAXIMUM_INTENT = 256 * 1024
_ENVIRONMENT = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR"})


@dataclass(frozen=True)
class SiteImportOutcome:
    """A bounded Site outcome; receipt text is never treated as authority."""

    status: SiteOutcomeStatus
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"created", "duplicate", "held", "failed", "uncertain"}:
            raise EditorialError("site_outcome_invalid")
        if self.receipt_id is not None and (
            not isinstance(self.receipt_id, str)
            or _SAFE_RECEIPT.fullmatch(self.receipt_id) is None
        ):
            raise EditorialError("site_outcome_invalid")


class SiteImportPort(Protocol):
    """Replaceable publisher boundary used by the durable delivery coordinator."""

    def import_intent(self, intent_file: Path) -> SiteImportOutcome: ...


class SiteOutcomeParser(Protocol):
    """Site-owned receipt protocol, deliberately injected until it is finalized."""

    def parse(self, returncode: int, output: bytes) -> SiteImportOutcome: ...


class ConservativeSiteOutcomeParser:
    """No provisional output or exit-code convention authorizes a delivery."""

    def parse(self, returncode: int, output: bytes) -> SiteImportOutcome:
        del returncode, output
        return SiteImportOutcome("uncertain")


def _private_file(path: Path, *, code: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if (
            not path.is_absolute()
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise ValueError
        parent = io.open_directory(resolved.parent, private=True)
        os.close(parent)
        return resolved
    except (OSError, ValueError):
        raise EditorialError(code) from None


def _executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if (
            not path.is_absolute()
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o022
            or not os.access(resolved, os.X_OK)
        ):
            raise ValueError
        return resolved
    except (OSError, ValueError):
        raise EditorialError("site_command_invalid") from None


def _intent_bytes(path: Path) -> bytes:
    """Read a bounded immutable input through its private parent descriptor."""
    try:
        directory = io.open_directory(path.parent, private=True)
        try:
            return io.read_file(directory, path.name, _MAXIMUM_INTENT, private=True)
        finally:
            os.close(directory)
    except (OSError, io.BridgeError):
        raise EditorialError("site_intent_file_invalid") from None


@dataclass(frozen=True)
class SiteImportCommand:
    """Trusted executable plus fixed command arguments, excluding secret values."""

    executable: Path
    arguments: tuple[str, ...]
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        _executable(self.executable)
        if (
            not isinstance(self.arguments, tuple)
            or any(
                not isinstance(value, str) or not value or "\x00" in value
                for value in self.arguments
            )
            or not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 600
        ):
            raise EditorialError("site_command_invalid")


class CommandSiteImporter:
    """Invoke the provisional Site CLI without inheriting ambient secrets.

    The caller owns durable retries.  ``uncertain`` means it must first
    reconcile the same immutable intent with the eventual Site receipt API;
    this adapter never retries a process invocation by itself.
    """

    def __init__(
        self,
        command: SiteImportCommand,
        ingress_token_file: Path,
        *,
        parser: SiteOutcomeParser | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.ingress_token_file = _private_file(ingress_token_file, code="site_token_file_invalid")
        if environment is not None and (
            not isinstance(environment, dict)
            or any(
                not isinstance(key, str)
                or key not in _ENVIRONMENT
                or not isinstance(value, str)
                or "\x00" in value
                for key, value in environment.items()
            )
        ):
            raise EditorialError("site_environment_invalid")
        self.environment = dict(environment or {})
        self.parser = parser or ConservativeSiteOutcomeParser()

    def import_intent(self, intent_file: Path) -> SiteImportOutcome:
        path = _private_file(intent_file, code="site_intent_file_invalid")
        try:
            validate_publication_intent(io.decode(_intent_bytes(path)))
        except EditorialError:
            # The input must be checked before the Site process can observe it.
            raise EditorialError("site_intent_file_invalid") from None
        argv = [
            str(_executable(self.command.executable)),
            *self.command.arguments,
            "publish-import",
            "--intent-file",
            str(path),
            "--ingress-token-file",
            str(self.ingress_token_file),
        ]
        try:
            completed = subprocess.run(
                argv,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.environment,
                timeout=self.command.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return SiteImportOutcome("uncertain")
        except OSError:
            return SiteImportOutcome("failed")
        if len(completed.stdout) > _MAXIMUM_OUTPUT:
            return SiteImportOutcome("uncertain")
        try:
            outcome = self.parser.parse(completed.returncode, completed.stdout)
            if not isinstance(outcome, SiteImportOutcome):
                raise ValueError
            return outcome
        except (EditorialError, TypeError, ValueError, UnicodeError):
            # Unknown output might describe a completed write, so it is never a
            # retryable failure by default.
            return SiteImportOutcome("uncertain")
