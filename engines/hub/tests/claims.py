"""The v1 claim client handles proxy transport failures in both claim modes."""

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch

from brigade import fleet_client
from transport import HOST, free_port, front


@contextlib.contextmanager
def failure_upstream(case):
    if case == "connect":
        yield free_port()
        return

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if case == "headers":
                time.sleep(3)
                self.close_connection = True
                return
            if case == "reset-after-commit":
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/html" if case in ("stream-reset", "stream-idle") else "application/json"
            )
            length = 9 * 1024 * 1024 if case == "oversized-json" else 100
            self.send_header("Content-Length", str(length))
            self.end_headers()
            try:
                self.wfile.write(b"{" if length == 100 else b"x" * (8 * 1024 * 1024 + 1))
                self.wfile.flush()
                if case == "stream-idle":
                    time.sleep(3)
                else:
                    time.sleep(0.05)
            except OSError:
                pass
            self.close_connection = True

    server = ThreadingHTTPServer((HOST, 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Claims(unittest.TestCase):
    def test_default_and_required_claim_modes(self):
        cases = (
            "connect",
            "headers",
            "reset-after-commit",
            "json-reset",
            "stream-reset",
            "stream-idle",
            "oversized-json",
        )
        for case in cases:
            with self.subTest(case=case), failure_upstream(case) as upstream, front(upstream) as (_, port):
                config = {"hub_url": f"http://{HOST}:{port}", "token": "fake-admin-token"}
                with (
                    patch.object(fleet_client, "load_fleet_config", return_value=config),
                    patch.object(fleet_client, "resolve_node_id", return_value="11111111-1111-4111-8111-111111111111"),
                ):
                    with fleet_client.repo_claim("fake-repo") as decision:
                        self.assertFalse(decision.granted)
                        self.assertEqual(decision.reason, "hub-unavailable")
                    with self.assertRaises(fleet_client.FleetClaimRequiredError):
                        with fleet_client.repo_claim("fake-repo", require_claim=True):
                            self.fail("required claim entered work after a transport failure")


if __name__ == "__main__":
    unittest.main()
