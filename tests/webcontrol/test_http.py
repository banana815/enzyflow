"""HTTP boundary tests with a fake controller and an ephemeral loopback port."""
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from webcontrol.controller import Controller
from webcontrol.demo import DemoTransport
from webcontrol.server import LocalServer


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller(DemoTransport, demo=True, autostart=False)
        self.server = LocalServer(self.controller, 0, token="test-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.controller.close()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        data = None if body is None else json.dumps(body)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request(method, path, data, request_headers)
        response = connection.getresponse()
        result = (response.status, response.read(), dict(response.getheaders()))
        connection.close()
        return result

    def test_health_and_page_are_local_and_token_is_inserted(self):
        status, body, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["demo"])
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"test-secret", body)
        self.assertNotIn(b"__ENZYFLOW_TOKEN__", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_cross_origin_and_rebinding_host_are_rejected(self):
        for headers in ({"Host": "attacker.test"}, {"Origin": "https://attacker.test"}):
            status, _, _ = self.request("GET", "/api/state", headers=headers)
            self.assertEqual(status, 403)

    def test_mutations_require_nonce_and_valid_json(self):
        body = {"client_id": "test-page"}
        status, _, _ = self.request("POST", "/api/claim", body)
        self.assertEqual(status, 403)
        status, reply, _ = self.request("POST", "/api/claim", body,
                                         {"X-Enzyflow-Token": "test-secret"})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(reply)["state"]["owner"], "test-page")

    def test_motor_service_does_not_expose_spectrometer(self):
        status, _, _ = self.request("GET", "/api/spectrometer-state")
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", "/api/spectrometer", {"op": "connect"},
                                     {"X-Enzyflow-Token": "test-secret"})
        self.assertEqual(status, 404)
        self.assertFalse(hasattr(self.server, "spectrometer"))

    def test_path_traversal_is_rejected(self):
        status, _, _ = self.request("GET", "/static/%2e%2e/server.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
