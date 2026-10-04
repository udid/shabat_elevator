"""Local static site and authenticated future Raspberry Pi ingestion API."""

from __future__ import annotations

import argparse
import hmac
import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .state import ObservationStore

ROOT = Path(__file__).resolve().parent.parent


def load_env(path):
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class DashboardHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript; charset=utf-8",
                      ".json": "application/json; charset=utf-8", ".html": "text/html; charset=utf-8",
                      ".css": "text/css; charset=utf-8"}

    def __init__(self, *args, store, config, token="", allowed_origin="", **kwargs):
        self.store, self.config, self.token = store, config, token
        self.allowed_origin = allowed_origin
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", "no-cache")
        if self.allowed_origin and self.headers.get("Origin") == self.allowed_origin:
            self.send_header("Access-Control-Allow-Origin", self.allowed_origin)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def send_json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/state":
            self.send_json(self.store.snapshot())
        elif path == "/api/health":
            self.send_json({"ok": True, "sourceMode": self.config["sourceMode"]})
        elif path == "/config.json":
            self.send_json(self.config)
        elif path.startswith("/api/"):
            self.send_json({"error": "Not found"}, 404)
        else:
            super().do_GET()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/observations", "/api/heartbeat"):
            self.send_json({"error": "Not found"}, 404)
            return
        if not self.token or self.token.startswith("replace-"):
            self.send_json({"error": "Ingestion is disabled until INGEST_TOKEN is configured"}, 503)
            return
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + self.token).encode("utf-8")):
            self.send_json({"error": "Unauthorized"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                self.send_json({"error": "Body must contain 1..8192 bytes"}, 413)
                return
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(length))
            result = self.store.observe(payload) if path.endswith("observations") else self.store.heartbeat(payload)
            self.send_json(result)
        except (ValueError, TypeError, UnicodeError) as error:
            self.send_json({"error": str(error)}, 400)
        except TimeoutError:
            self.send_json({"error": "Request body timed out"}, 408)


def create_server(host="127.0.0.1", port=8000, *, live=False, state_path=None, token=None):
    config = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))
    if live:
        config["sourceMode"] = "live"
        config["liveApiUrl"] = ""
    store = ObservationStore(state_path, device_id=os.getenv("DEVICE_ID", "begin17-floor7"),
                             stale_after=config["staleAfterSeconds"], arrival_grace=config["arrivalGraceSeconds"])
    handler = partial(DashboardHandler, store=store, config=config,
                      token=token if token is not None else os.getenv("INGEST_TOKEN", ""),
                      allowed_origin=os.getenv("ALLOWED_ORIGIN", ""))
    return ThreadingHTTPServer((host, port), handler)


def main():
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Single-elevator Shabbat dashboard")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--live", action="store_true", help="Wait for real floor-seven observations")
    args = parser.parse_args()
    server = create_server(args.host, args.port, live=args.live, state_path=ROOT / "data" / "state.json")
    print(f"Elevator dashboard: http://{args.host}:{server.server_port} ({'live' if args.live else 'simulation'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
