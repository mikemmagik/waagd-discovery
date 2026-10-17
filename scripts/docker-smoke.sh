#!/bin/sh
# Offline runtime proof; use only the isolated temporary volume created here.
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
image=${WAAGD_SMOKE_IMAGE:-waagd-discovery:smoke}
scratch=$(mktemp -d "${TMPDIR:-/tmp}/waagd-discovery-smoke.XXXXXX")
volume="waagd-smoke-$(basename "$scratch")"

cleanup() {
    docker volume rm "$volume" >/dev/null 2>&1 || true
    rm -rf "$scratch"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${WAAGD_SMOKE_SKIP_BUILD:-0}" != "1" ]; then
    docker build --tag "$image" "$project_dir"
fi
docker image inspect "$image" >/dev/null
docker volume create --label com.waagd-discovery.smoke=true "$volume" >/dev/null
mkdir "$scratch/exports" "$scratch/backups"
mkdir "$scratch/backups/managed"
cat > "$scratch/sources.yaml" <<'YAML'
sources:
  - id: smoke_fixture
    name: Offline fixture
    url: https://example.org/feed.xml
YAML
# These contain only disposable fixture output; support nonroot containers on
# both Linux runners and Docker Desktop without changing real operator folders.
# Precreate the managed fixture as the host owner too. Otherwise UID10001 creates
# it mode0700 and a Linux runner with another UID cannot clean up its contents.
# This permission applies only to disposable test directories, never production.
chmod 1777 "$scratch/exports" "$scratch/backups" "$scratch/backups/managed"

run_container() {
    docker run --rm -i \
        --network none \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 \
        --memory 512m --cpus 1 --pids-limit 64 \
        --mount "type=volume,src=$volume,dst=/data" \
        --mount "type=bind,src=$scratch/exports,dst=/exports" \
        --mount "type=bind,src=$scratch/backups,dst=/backups" \
        --mount "type=bind,src=$scratch/sources.yaml,dst=/app/config/sources.yaml,readonly" \
        "$@"
}

# Two separate containers parse and ingest the same feed into the same volume.
# No HTTP client, host networking, credentials or public feeds are involved.
for phase in first second; do
    run_container --entrypoint python "$image" - "$phase" <<'PY'
import errno
import os
import sys
from pathlib import Path

from waagd_discovery.collectors.rss import parse_feed
from waagd_discovery.models import FetchResult, Source
from waagd_discovery.storage import Repository

assert os.getuid() == 10001, "Runtime must be nonroot"
assert not Path("/var/run/docker.sock").exists(), "Docker socket must be absent"
status = Path("/proc/self/status").read_text()
assert "NoNewPrivs:\t1" in status
assert "CapEff:\t0000000000000000" in status
try:
    Path("/app/write-probe").write_text("must fail")
except OSError as exc:
    assert exc.errno in (errno.EROFS, errno.EACCES)
else:
    raise AssertionError("Root filesystem is writable")

body = b'''<?xml version="1.0"?>
<rss version="2.0"><channel><title>Offline fixture</title>
<link>https://example.org/</link><description>Smoke test</description>
<item><title>Deterministic offline discovery</title>
<link>https://example.org/story?utm_source=fixture#fragment</link>
<guid>smoke-story-1</guid>
<description>Sufficient synthetic feed metadata for a repeatable eligible fixture.</description>
<pubDate>Thu, 15 Oct 2026 12:00:00 GMT</pubDate></item>
</channel></rss>'''
source = Source("smoke_fixture", "Offline fixture", "https://example.org/feed.xml")
parsed = parse_feed(body, source)
assert len(parsed.items) == 1 and parsed.skipped == 0
with Repository(Path("/data/discovery.sqlite3")) as repo:
    repo.sync_sources([source])
    run = repo.start_run()
    counts = repo.save_source(run, source, parsed.items, FetchResult(200, body=body))
    expected = (1, 0, 0) if sys.argv[1] == "first" else (0, 1, 0)
    assert counts == expected, (counts, expected)
    repo.finish_run(run, "success")
    assert repo.stats()["articles"] == 1
    assert repo.stats()["runs"] == (1 if sys.argv[1] == "first" else 2)
    assert repo.candidates()[0]["url"] == "https://example.org/story"
    repo.verify()
print(f"Offline ingest {sys.argv[1]}: passed")
PY
done

run_container "$image" verify
run_container "$image" stats
run_container "$image" filter --at 2026-10-17T12:00:00Z
run_container "$image" filter-report --limit 10
run_container "$image" export --output /exports/smoke.json --at 2026-10-17T12:00:00Z
run_container "$image" backup --output /backups/smoke.sqlite3
run_container "$image" restore --input /backups/smoke.sqlite3 --confirm
run_container "$image" verify
run_container "$image" backup-managed --directory /backups/managed
run_container "$image" backup-rotate --directory /backups/managed
run_container "$image" backup-rotate --directory /backups/managed --apply
run_container --entrypoint python "$image" - <<'PY'
import json
from pathlib import Path

from waagd_discovery.storage import Repository

data = json.loads(Path("/exports/smoke.json").read_text())
assert data["schema_version"] == 2 and len(data["candidates"]) == 1
assert data["candidates"][0]["selection"]["status"] == "eligible"
assert all(data["candidates"][0]["selection"]["checks"].values())
assert len(list(Path("/backups/managed").glob("waagd-backup-*.sqlite3"))) == 1
with Repository(Path("/backups/smoke.sqlite3")) as backup:
    backup.verify()
    assert backup.stats()["articles"] == 1
    assert backup.stats()["runs"] == 2
print("Recreated container, persistent SQLite, JSON export and verified backup: passed")
PY
printf '%s\n' 'Docker offline smoke: PASS'
