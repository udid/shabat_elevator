"""Local microphone service, dashboard, and read-only public timing API."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import math
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from .state import ObservationStore

ROOT = Path(__file__).resolve().parent.parent


class DashboardHTTPServer(ThreadingHTTPServer):
    # Many always-on screens can poll at the same instant after reconnecting.
    request_queue_size = 128


def load_env(path):
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class DashboardHandler(SimpleHTTPRequestHandler):
    cache_control = "no-cache"
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
        self.send_header("Cache-Control", self.cache_control)
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
            state = self.store.snapshot()
            state["monitorOnly"] = self.config.get("monitorOnly", False)
            self.send_json(state)
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

    def log_request(self, code="-", size="-"):
        # Avoid continuous SD-card log writes for normal five-second polling.
        if str(code) == "200" and urlsplit(self.path).path in ("/api/state", "/api/health"):
            return
        super().log_request(code, size)

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


class PublicDashboardHandler(DashboardHandler):
    """Read-only endpoint for a tunnel; never serve files or accept device events."""

    cache_control = "no-store"
    public_paths = frozenset(("/api/state", "/api/health"))

    def do_GET(self):
        if urlsplit(self.path).path in self.public_paths:
            super().do_GET()
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        if urlsplit(self.path).path not in self.public_paths:
            self.send_json({"error": "Not found"}, 404)
            return
        self.send_response(204)
        self.send_header("Allow", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def reject_method(self):
        self.send_response(405)
        self.send_header("Allow", "GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = reject_method
    do_POST = reject_method
    do_PUT = reject_method
    do_PATCH = reject_method
    do_DELETE = reject_method
    do_CONNECT = reject_method
    do_TRACE = reject_method


def create_server(host="127.0.0.1", port=8000, *, live=False, state_path=None, token=None,
                  store=None, public=False, runtime_config=None, monitor_only=False):
    config = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))
    if live:
        config["sourceMode"] = "live"
        config["liveApiUrl"] = ""
    config["monitorOnly"] = monitor_only
    if store is None:
        store = ObservationStore(state_path, device_id=os.getenv("DEVICE_ID", "begin17-floor7"),
                                 stale_after=config["staleAfterSeconds"], arrival_grace=config["arrivalGraceSeconds"],
                                 default_cycle_seconds=runtime_config["defaultCycleSeconds"] if runtime_config else None,
                                 cycle_tolerance_percent=runtime_config.get("cycleTolerancePercent", 15) if runtime_config else 15,
                                 event_kind="departure")
    handler_class = PublicDashboardHandler if public else DashboardHandler
    handler = partial(handler_class, store=store, config=config,
                      token=token if token is not None else os.getenv("INGEST_TOKEN", ""),
                      allowed_origin=os.getenv("ALLOWED_ORIGIN", ""))
    server = DashboardHTTPServer(("127.0.0.1" if public else host, port), handler)
    server.store = store
    return server


def main():
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Single-elevator Shabbat dashboard")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--public-port", type=int, help="Expose a separate read-only API on 127.0.0.1")
    parser.add_argument("--live", action="store_true", help="Wait for real floor-seven observations")
    parser.add_argument("--detector-config", type=Path, default=os.getenv("DETECTOR_CONFIG"),
                        help="Enable continuous microphone detection using compact calibration JSON")
    parser.add_argument("--audio-device", default=os.getenv("AUDIO_DEVICE"),
                        help="PortAudio input name (recommended) or numeric device index")
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--monitor-only", action="store_true",
                        help="Test microphone and log candidates without publishing elevator observations")
    parser.add_argument("--diagnostics-dir", type=Path, default=ROOT / "data" / "diagnostics",
                        help="Private rolling recordings and diagnostic event logs for live detection")
    parser.add_argument("--recording-max-gb", type=float, default=16,
                        help="Maximum rolling audio size in decimal GB (default 16); always reserve 20%% free disk")
    parser.add_argument("--no-recording", action="store_true",
                        help="Disable diagnostic audio recording for debugging; retain event logs")
    args = parser.parse_args()
    if not math.isfinite(args.recording_max_gb) or not 0.001 <= args.recording_max_gb <= 1_000_000:
        parser.error("--recording-max-gb must be between 0.001 and 1000000")
    if args.monitor_only and not args.detector_config:
        parser.error("--monitor-only requires --detector-config")
    runtime_config = None
    if args.detector_config:
        if not args.live:
            parser.error("--detector-config requires --live")
        from .runtime_config import validate_runtime_config
        runtime_config = validate_runtime_config(json.loads(args.detector_config.read_text(encoding="utf-8")))
        if args.sample_rate <= 2 * runtime_config["detector"]["frequencyHighHz"]:
            parser.error("--sample-rate must exceed twice the highest detector frequency")
    device = args.audio_device
    if device is not None and device.isdecimal():
        device = int(device)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = create_server(args.host, args.port, live=args.live, state_path=ROOT / "data" / "state.json",
                           runtime_config=runtime_config, monitor_only=args.monitor_only)
    public_server = None
    public_thread = None
    detector = None
    diagnostics = None
    try:
        if args.public_port is not None:
            if args.public_port == server.server_port:
                raise ValueError("The public API and local ingestion must use different ports")
            public_server = create_server(port=args.public_port, live=args.live, store=server.store, public=True,
                                           monitor_only=args.monitor_only)
            public_thread = Thread(target=public_server.serve_forever, name="elevator-public-api", daemon=True)
            public_thread.start()
            print(f"Public read-only API: http://127.0.0.1:{public_server.server_port}", flush=True)
        if runtime_config is not None:
            from .detector_service import DetectorSupervisor
            from .diagnostics import Diagnostics
            diagnostics = Diagnostics(args.diagnostics_dir,
                                      max_recording_bytes=max(1, int(args.recording_max_gb * 1_000_000_000)),
                                      recording_enabled=not args.no_recording)
            detector = DetectorSupervisor(server.store, runtime_config, device=device, sample_rate=args.sample_rate,
                                            monitor_only=args.monitor_only, diagnostics=diagnostics)
            detector.start()
        print(f"Elevator dashboard: http://{args.host}:{server.server_port} ({'live' if args.live else 'simulation'})", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if detector is not None:
            detector.stop()
        if diagnostics is not None:
            diagnostics.close()
        if public_thread is not None and public_thread.is_alive():
            public_server.shutdown()
            public_thread.join()
        if public_server is not None:
            public_server.server_close()
        server.server_close()
