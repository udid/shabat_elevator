import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from elevator.server import create_server
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
        event = {"deviceId": "begin17-floor7", "eventId": "http-test", "floor": 7,
                 "observedAt": iso(utc_now()), "cycleSeconds": 570}
        request = Request(self.base + "/api/observations", data=json.dumps(event).encode(),
                          headers={"Authorization": "Bearer test-only-do-not-use-in-production",
                                   "Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            self.assertTrue(json.load(response)["accepted"])

    def test_python_source_and_environment_are_not_public(self):
        for path in ("/main.py", "/.env", "/../.env", "/elevator/state.py"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                urlopen(self.base + path)
            self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
