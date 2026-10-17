"""Structured diagnostics: only caller-supplied safe scalar metadata."""

import json
import logging
import sys
from datetime import UTC, datetime

_LOGGER = logging.getLogger("waagd.discovery")


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.handlers = [handler]
    _LOGGER.setLevel(level)
    _LOGGER.propagate = False


def event(name: str, level: int = logging.INFO, **fields: object) -> None:
    _LOGGER.log(
        level,
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": logging.getLevelName(level),
                "event": name,
                **fields,
            },
            ensure_ascii=True,
        ),
    )
