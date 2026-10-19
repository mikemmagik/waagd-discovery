#!/usr/bin/env python3
"""Write one Python-produced verified-publication envelope for cross-repo E2E."""

import argparse
import hashlib
from pathlib import Path

from waagd_discovery.bridge import io
from waagd_discovery.editorial.publication import _payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = "https://example.test/cross-repository-fixture"
    key = hashlib.sha256(url.encode()).hexdigest()
    article, independent = "a" * 64, "b" * 64
    intent = _payload(
        {
            "candidate_key": key,
            "url": url,
            "title": "Cross repository fixture",
            "published_at": "2026-10-19T00:00:00+00:00",
            "sources": [{"id": "fixture", "name": "Fixture", "url": "https://example.test/rss"}],
        },
        {
            "verification_status": "VERIFIED",
            "evidence": [
                {"id": article, "url": url},
                {"id": independent, "url": "https://verify.example/claim"},
            ],
        },
        {
            "decision": "READY_FOR_REVIEW",
            "verification_evidence": [article, independent],
            "unverified_claims": [],
            "open_questions": [],
            "risks": [],
            "headline": "Cross repository fixture",
            "article": "A bounded verified editorial fixture.",
        },
        1,
    )
    args.output.write_bytes(
        io.encode(
            {
                "schema_version": 1,
                "producer": "waagd-discovery-publication-v1",
                "intents": [intent],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
