"""Test-only offline HTTP overlay, bind-mounted by docker-scheduler-smoke.py.

Not part of the runtime image. Production HTTP URL/DNS validation stays unchanged.
"""

import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from waagd_discovery import collection
from waagd_discovery.cli import main
from waagd_discovery.models import FetchResult, Source

DATA = Path("/data")


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        slug = self.path.strip("/")
        if slug == "second" and (DATA / "smoke-block").exists():
            (DATA / "smoke-blocked").write_text("ready")
            while (DATA / "smoke-block").exists():
                time.sleep(0.1)
        published = format_datetime(datetime.now(UTC))
        body = f"""<rss version="2.0"><channel><title>Offline fixture</title>
<link>https://example.org/</link><description>Local test</description>
<item><title>Offline discovery {slug}</title>
<link>https://example.org/story/{slug}</link><guid>fixture-{slug}</guid>
<description>Sufficient synthetic metadata to exercise real discovery and filtering.</description>
<pubDate>{published}</pubDate></item></channel></rss>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *args: object) -> None:
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()


class FixtureHttpClient:
    def __enter__(self) -> "FixtureHttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def fetch(self, source: Source) -> FetchResult:
        # URLs come only from the fixture registry. No feed URL is fetched.
        slug = "second" if source.id == "second" else "first"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/{slug}", timeout=120
        ) as response:
            return FetchResult(200, body=response.read())


collection.HttpClient = FixtureHttpClient  # type: ignore[assignment]
sys.exit(main())
