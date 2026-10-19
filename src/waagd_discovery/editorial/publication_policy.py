"""Trusted, default-deny policy for generating automatic publication intents.

The policy is evaluated outside the editor model.  It deliberately does not make a
verification result true: an operator must explicitly enable it only after the
configured verification source is independently governed and validated.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from waagd_discovery.editorial.contracts import EditorialError, utc

PUBLICATION_POLICY_VERSION = "publication-policy-v1"


@dataclass(frozen=True)
class PublicationPolicy:
    """Small, inspectable policy supplied by the trusted host configuration.

    ``automatic_enabled`` defaults to false.  Test and staging callers must opt in
    deliberately; production remains blocked until its real evidence source has
    passed the separate operator acceptance.
    """

    automatic_enabled: bool = False
    version: str = PUBLICATION_POLICY_VERSION
    maximum_age: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if (
            type(self.automatic_enabled) is not bool
            or not isinstance(self.version, str)
            or not 1 <= len(self.version) <= 100
            or not isinstance(self.maximum_age, timedelta)
            or not timedelta(seconds=1) <= self.maximum_age <= timedelta(days=31)
        ):
            raise EditorialError("publication_policy_invalid")

    def permits(self, published_at_source: str, *, now: datetime | None = None) -> bool:
        """Return true only for explicitly enabled, fresh source material."""
        if not self.automatic_enabled:
            return False
        try:
            published = datetime.fromisoformat(published_at_source.replace("Z", "+00:00"))
            if published.tzinfo is None or published.utcoffset() is None:
                return False
            clock = utc(now)
            age = clock - published.astimezone(UTC)
            return timedelta(0) <= age <= self.maximum_age
        except (TypeError, ValueError, EditorialError):
            return False


def load_publication_policy(path: Path | None) -> PublicationPolicy:
    """Read a private, host-owned policy; absence intentionally remains deny-all."""
    if path is None:
        return PublicationPolicy()
    try:
        import os

        import yaml

        from waagd_discovery.bridge import io

        directory = io.open_directory(path.parent, private=True)
        try:
            data = yaml.safe_load(io.read_file(directory, path.name, 4096, private=True).decode())
        finally:
            os.close(directory)
        if not isinstance(data, dict) or set(data) != {"publication"}:
            raise ValueError
        body = data["publication"]
        if not isinstance(body, dict) or set(body) != {
            "automatic_enabled",
            "version",
            "maximum_age_hours",
        }:
            raise ValueError
        enabled, version, hours = (
            body["automatic_enabled"],
            body["version"],
            body["maximum_age_hours"],
        )
        if type(enabled) is not bool or type(hours) is not int:
            raise ValueError
        return PublicationPolicy(
            automatic_enabled=enabled,
            version=version,
            maximum_age=timedelta(hours=hours),
        )
    except (ImportError, OSError, TypeError, ValueError, yaml.YAMLError, io.BridgeError):
        raise EditorialError("publication_policy_unreadable") from None
