import json
import os
import socket
import threading
import unittest
from datetime import timedelta
from http.client import HTTPConnection
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from elevator.server import create_server, main
from elevator.state import iso, utc_now


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(port=0, live=True, token="test-only-do-not-use-in-production")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_live_config_and_public_state(self):
        with urlopen(self.base + "/config.json") as response:
            config = json.load(response)
        self.assertEqual(config["sourceMode"], "live")
        self.assertNotIn("INGEST_TOKEN", config)
        with urlopen(self.base + "/api/state") as response:
            self.assertEqual(json.load(response)["mode"], "live")

    def test_ingestion_requires_authentication(self):
        request = Request(self.base + "/api/observations", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 401)

    def test_valid_authenticated_observation(self):
        event = {"deviceId": "begin17-floor7", "eventId": "http-test", "floor": 7, "eventKind": "departure",
                 "observedAt": iso(utc_now()), "cycleSeconds": 570}
        request = Request(self.base + "/api/observations", data=json.dumps(event).encode(),
                          headers={"Authorization": "Bearer test-only-do-not-use-in-production",
                                   "Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            self.assertTrue(json.load(response)["accepted"])

    def test_python_source_and_environment_are_not_public(self):
        for path in ("/run_metrics_server.py", "/.env", "/../.env", "/elevator/state.py"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                urlopen(self.base + path)
            self.assertEqual(caught.exception.code, 404)


class PublicServerTests(unittest.TestCase):
    origin = "https://udid.github.io"
    token = "test-only-public-is-read-only"

    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"ALLOWED_ORIGIN": cls.origin, "DEVICE_ID": "begin17-floor7"}):
            cls.local = create_server(port=0, live=True, token=cls.token,
                                      runtime_config={"defaultCycleSeconds": 570})
            cls.public = create_server("0.0.0.0", 0, live=True, token=cls.token,
                                       store=cls.local.store, public=True)
        cls.threads = []
        for server in (cls.local, cls.public):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.threads.append(thread)

    @classmethod
    def tearDownClass(cls):
        for server, thread in zip((cls.local, cls.public), cls.threads):
            server.shutdown()
            thread.join()
            server.server_close()

    def request(self, server, path, *, method="GET", payload=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def test_local_events_and_heartbeat_are_visible_publicly(self):
        observed = utc_now() - timedelta(seconds=20)
        event = {"deviceId": "begin17-floor7", "eventId": "shared-http-event", "floor": 7, "eventKind": "departure",
                 "observedAt": iso(observed), "cycleSeconds": 570}
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        status, _, body = self.request(self.local, "/api/observations", method="POST",
                                       payload=event, headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["accepted"])
        status, public_headers, body = self.request(self.public, "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(public_headers["Cache-Control"], "no-store")
        state = json.loads(body)
        self.assertEqual(state["lastDepartureAt"], iso(observed))
        self.assertIsNone(state["lastArrivalAt"])
        self.assertEqual(state["cycleSeconds"], 570)

        seen = utc_now() - timedelta(seconds=5)
        status, _, _ = self.request(self.local, "/api/heartbeat", method="POST", headers=headers,
                                    payload={"deviceId": "begin17-floor7", "observedAt": iso(seen)})
        self.assertEqual(status, 200)
        status, _, body = self.request(self.public, "/api/state")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(state["lastSeenAt"], iso(seen))
        self.assertEqual(state["lastDepartureAt"], iso(observed))
        self.assertTrue(state["sourceConnected"])

    def test_public_methods_cannot_write_even_with_valid_token(self):
        before = self.local.store.snapshot()
        event = {"deviceId": "begin17-floor7", "eventId": "must-not-be-accepted", "floor": 7,
                 "observedAt": iso(utc_now()), "cycleSeconds": 570}
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        for method in ("POST", "PUT", "PATCH", "DELETE", "CONNECT", "TRACE"):
            for path in ("/api/observations", "/api/heartbeat", "/api/state"):
                with self.subTest(method=method, path=path):
                    status, response_headers, _ = self.request(self.public, path, method=method,
                                                               payload=event, headers=headers)
                    self.assertEqual(status, 405)
                    self.assertEqual(response_headers["Allow"], "GET, OPTIONS")
        after = self.local.store.snapshot()
        for field in ("lastArrivalAt", "lastSeenAt", "cycleSeconds"):
            self.assertEqual(after[field], before[field])

    def test_public_does_not_serve_files_or_other_api_routes(self):
        for path in ("/", "/index.html", "/config.json", "/app.js", "/.env", "/../.env",
                     "/api/observations", "/api/heartbeat", "/api/unknown"):
            with self.subTest(path=path):
                status, _, _ = self.request(self.public, path)
                self.assertEqual(status, 404)
                status, _, body = self.request(self.public, path, method="HEAD")
                self.assertEqual(status, 405)
                self.assertEqual(body, b"")
        status, _, body = self.request(self.public, "/api/state", method="HEAD")
        self.assertEqual(status, 405)
        self.assertEqual(body, b"")

    def test_public_cors_allows_only_configured_origin(self):
        for origin in (self.origin, "https://other.example", None):
            with self.subTest(origin=origin):
                headers = {"Origin": origin} if origin else {}
                status, response_headers, _ = self.request(self.public, "/api/state", headers=headers)
                self.assertEqual(status, 200)
                if origin == self.origin:
                    self.assertEqual(response_headers["Access-Control-Allow-Origin"], self.origin)
                    self.assertEqual(response_headers["Vary"], "Origin")
                else:
                    self.assertIsNone(response_headers["Access-Control-Allow-Origin"])

    def test_public_preflight_advertises_only_reading_on_allowed_paths(self):
        for path in ("/api/state", "/api/health"):
            with self.subTest(path=path):
                status, headers, body = self.request(self.public, path, method="OPTIONS",
                                                     headers={"Origin": self.origin})
                self.assertEqual(status, 204)
                self.assertEqual(headers["Access-Control-Allow-Methods"], "GET, OPTIONS")
                self.assertEqual(headers["Access-Control-Allow-Origin"], self.origin)
                self.assertIsNone(headers["Access-Control-Allow-Headers"])
                self.assertEqual(body, b"")
        for path in ("/config.json", "/api/observations", "/api/heartbeat"):
            with self.subTest(path=path):
                status, headers, _ = self.request(self.public, path, method="OPTIONS")
                self.assertEqual(status, 404)
                self.assertIsNone(headers["Access-Control-Allow-Methods"])

    def test_public_is_loopback_only_and_local_site_still_works(self):
        self.assertEqual(self.public.server_address[0], "127.0.0.1")
        self.assertIs(self.public.store, self.local.store)
        self.assertNotEqual(self.public.server_port, self.local.server_port)
        status, _, body = self.request(self.public, "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "sourceMode": "live"})
        status, _, body = self.request(self.local, "/config.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sourceMode"], "live")


class ServerLifecycleTests(unittest.TestCase):
    def test_same_public_and_local_port_is_rejected(self):
        local = create_server(port=0)
        self.addCleanup(local.server_close)
        args = ["run_metrics_server.py", "--public-port", str(local.server_port)]
        with patch("sys.argv", args), patch("elevator.server.load_env"), \
                patch("elevator.server.create_server", return_value=local):
            with self.assertRaisesRegex(ValueError, "different ports"):
                main()
        self.assertEqual(local.socket.fileno(), -1)

    def test_public_bind_failure_closes_local_listener(self):
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        created = []

        def capture_server(*args, **kwargs):
            kwargs["state_path"] = None
            server = create_server(*args, **kwargs)
            created.append(server)
            return server

        args = ["run_metrics_server.py", "--port", "0", "--public-port", str(occupied.getsockname()[1])]
        with patch("sys.argv", args), patch("elevator.server.load_env"), \
                patch("elevator.server.create_server", side_effect=capture_server):
            with self.assertRaises(OSError):
                main()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].socket.fileno(), -1)

    def test_interrupt_stops_public_thread_and_closes_both_listeners(self):
        local = create_server(port=0)
        public = create_server(port=0, store=local.store, public=True)
        self.addCleanup(local.server_close)
        self.addCleanup(public.server_close)
        threads = []

        def capture_thread(*args, **kwargs):
            thread = threading.Thread(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch("sys.argv", ["run_metrics_server.py", "--public-port", "0"]), \
                patch("elevator.server.load_env"), patch("builtins.print"), \
                patch("elevator.server.create_server", side_effect=[local, public]), \
                patch("elevator.server.Thread", side_effect=capture_thread), \
                patch.object(local, "serve_forever", side_effect=KeyboardInterrupt):
            main()
        self.assertEqual(len(threads), 1)
        self.assertFalse(threads[0].is_alive())
        self.assertEqual(local.socket.fileno(), -1)
        self.assertEqual(public.socket.fileno(), -1)

    def test_public_thread_start_failure_closes_both_listeners(self):
        local = create_server(port=0)
        public = create_server(port=0, store=local.store, public=True)
        self.addCleanup(local.server_close)
        self.addCleanup(public.server_close)
        with patch("sys.argv", ["run_metrics_server.py", "--public-port", "0"]), \
                patch("elevator.server.load_env"), \
                patch("elevator.server.create_server", side_effect=[local, public]), \
                patch("elevator.server.Thread.start", side_effect=RuntimeError("Cannot start thread")):
            with self.assertRaisesRegex(RuntimeError, "Cannot start thread"):
                main()
        self.assertEqual(local.socket.fileno(), -1)
        self.assertEqual(public.socket.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
