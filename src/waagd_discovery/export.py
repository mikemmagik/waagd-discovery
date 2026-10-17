"""Bounded atomic JSON discovery export. This is not editorial approval."""

import json
import os
import tempfile
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import jsonschema

from waagd_discovery.storage import Repository


def export_candidates(repository: Repository, destination: Path, limit: int = 50) -> int:
    if not 1 <= limit <= 500:
        raise ValueError("export_limit_invalid")
    candidates = repository.candidates(limit)
    data = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "candidates": candidates,
    }
    schema = json.loads(files("waagd_discovery").joinpath("candidate.schema.json").read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        data
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".candidates-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)
    return len(candidates)
