import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from mock_streaming_service import Handler


class MockStreamingServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, token=None, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_creator_cannot_modify_settings(self):
        status, body = self.request("POST", "/api/v1/creators/creator_test_99/settings", "creator-test-token", {"visibility": "public"})
        self.assertEqual(status, 403)
        self.assertFalse(body["state_changed"])

    def test_admin_auth_check_is_local_fixture_only(self):
        status, body = self.request("GET", "/admin/auth-check", "admin-test-token")
        self.assertEqual(status, 200)
        self.assertEqual(body["role"], "admin")


if __name__ == "__main__":
    unittest.main()
