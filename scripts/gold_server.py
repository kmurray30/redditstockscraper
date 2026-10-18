"""Password-gated web page for rating the gold set from a phone.

Runs as its own small Railway service (`gold-ui`), separate from the worker, so the worker
keeps no public domain. It serves only the 30 gold threads, never the dataset. State lives in
the bucket: ``gold/threads.md`` is read at start, ``gold/ratings.csv`` is read at start and
rewritten after every rating, so the worker's `gold-eval` job reads the same file after a
`pull-data --only gold`.

Environment: ``GOLD_PASSWORD`` (HTTP Basic auth; any user name), ``S3_*`` as for
``bucket_sync.py``, ``PORT`` (Railway sets it). Every route but ``/healthz`` needs the password.
"""

from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gold_page as gp  # noqa: E402

PREFIX = "data/gold/"
RATING_COLS = ["n", "post_id", "cik", "ticker", "title", "rating", "reason"]
MAX_BODY = 64 * 1024


class Store:
    """The threads and the ratings, mirrored to the bucket."""

    def __init__(self, bucket, prefix: str = PREFIX):
        self.bucket, self.prefix = bucket, prefix
        self.lock = threading.Lock()
        self.threads: list[dict] = []
        self.rows: list[dict] = []
        self.page = b""

    def load(self) -> None:
        md = self.bucket.get_bytes(self.prefix + "threads.md").decode()
        csv_text = self.bucket.get_bytes(self.prefix + "ratings.csv").decode()
        self.threads = gp.parse_threads_md(md)
        gp.attach_ciks(self.threads, csv_text)
        self.rows = list(csv.DictReader(io.StringIO(csv_text)))
        self.page = gp.build(self.threads, mode="server").encode()

    def ratings(self) -> dict[str, dict]:
        out = {}
        for r in self.rows:
            if (r.get("rating") or "").strip():
                out[r["post_id"]] = {"post_id": r["post_id"], "cik": r.get("cik"), "ticker": r.get("ticker"),
                                     "rating": int(float(r["rating"])), "reason": r.get("reason") or "",
                                     "ts": r.get("ts") or ""}
        return out

    def rate(self, post_id: str, rating: int, reason: str) -> dict:
        with self.lock:
            row = next((r for r in self.rows if r["post_id"] == post_id), None)
            if row is None:
                raise KeyError(post_id)
            row["rating"], row["reason"] = str(rating), reason
            row["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=RATING_COLS + ["ts"], extrasaction="ignore")
            w.writeheader()
            for r in self.rows:
                w.writerow({k: r.get(k, "") for k in RATING_COLS + ["ts"]})
            self.bucket.put_bytes(self.prefix + "ratings.csv", buf.getvalue().encode())
            return {"post_id": post_id, "rating": rating, "reason": reason, "ts": row["ts"]}


def make_handler(store: Store, password: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gold-ui"

        def _authed(self) -> bool:
            h = self.headers.get("Authorization", "")
            if not h.startswith("Basic "):
                return False
            try:
                _, _, given = base64.b64decode(h[6:]).decode().partition(":")
            except Exception:
                return False
            return hmac.compare_digest(given.encode(), password.encode())

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
                  extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _deny(self) -> None:
            self._send(401, b'{"error":"password required"}',
                       extra={"WWW-Authenticate": 'Basic realm="gold set", charset="UTF-8"'})

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if not self._authed():
                return self._deny()
            if path == "/":
                return self._send(200, store.page, "text/html; charset=utf-8")
            if path == "/api/ratings":
                return self._send(200, json.dumps(store.ratings()).encode())
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return self._deny()
            if self.path.split("?", 1)[0] != "/api/rate":
                return self._send(404, b'{"error":"not found"}')
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(400, b'{"error":"bad length"}')
            try:
                body = json.loads(self.rfile.read(n))
                post_id = str(body["post_id"])
                rating = int(body["rating"])
                reason = str(body.get("reason") or "")[:2000]
                if not 1 <= rating <= 5:
                    raise ValueError("rating out of range")
                saved = store.rate(post_id, rating, reason)
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, json.dumps({"error": f"bad request: {type(e).__name__}"}).encode())
            except Exception as e:  # the bucket write failed: say so, keep nothing half-written
                return self._send(502, json.dumps({"error": f"could not save: {type(e).__name__}"}).encode())
            self._send(200, json.dumps({"ok": True, "saved": saved}).encode())

        def log_message(self, fmt, *args):  # no auth headers, no bodies
            sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))
            sys.stdout.flush()

    return Handler


def serve(store: Store, password: str, port: int):
    httpd = ThreadingHTTPServer(("", port), make_handler(store, password))
    return httpd


def main() -> int:
    password = os.environ.get("GOLD_PASSWORD")
    if not password:
        raise SystemExit("GOLD_PASSWORD is not set")
    import bucket_sync as bs
    bucket = bs.Bucket(bs.load_creds(), workers=2)
    store = Store(bucket)
    store.load()
    port = int(os.environ.get("PORT") or 8080)
    print(f"[gold-ui] {len(store.threads)} threads, {len(store.ratings())} rated, port {port}", flush=True)
    serve(store, password, port).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
