"""Small local servers exercise accounting, deadlines and cgroup failures."""

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import load
import matrix


@contextlib.contextmanager
def hub(status=200, delay=0, truncated=False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.answer()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            time.sleep(delay)
            self.answer()

        def answer(self):
            metrics = self.path == "/metrics"
            body = json.dumps(
                {"proxy_buffer_fallback_total": 0}
                if metrics
                else {"error": "hub busy", "reason": "proxy-inflight-full"}
            ).encode()
            self.send_response(200 if metrics else status)
            self.send_header(
                "Content-Length",
                str(len(body) + (10 if truncated and not metrics else 0)),
            )
            self.send_header("Retry-After", "1")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            if truncated:
                self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((load.HOST, 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://{load.HOST}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Driver(unittest.TestCase):
    def run_load(self, url, *extra):
        with tempfile.TemporaryDirectory() as work:
            args = load.parser().parse_args(
                [
                    "--url",
                    url,
                    "--output",
                    str(Path(work) / "report.json"),
                    "--target",
                    "rust-proxy",
                    "--seconds",
                    ".16",
                    "--warmup",
                    "0",
                    "--rate",
                    "25",
                    "--timeout",
                    ".2",
                    *extra,
                ]
            )
            return load.run(args)

    def test_every_gateway_failure_retains_status_reason(self):
        for status in (502, 503, 504):
            with self.subTest(status=status), hub(status=status) as url:
                result = self.run_load(url, "--connection", "keep-alive")
                self.assertEqual(result["summary"]["attempts"], 4)
                self.assertEqual(result["summary"]["errors"], 4)
                self.assertTrue(
                    all(
                        r["status"] == status
                        and r["reason"] == "proxy-inflight-full"
                        and r["retry_after"] == "1"
                        for r in result["attempts"]
                    )
                )
                self.assertEqual(result["telemetry"]["queue_depth"], load.UNAVAILABLE)
                self.assertEqual(
                    result["telemetry"]["samples"][0]["value"],
                    {"proxy_buffer_fallback_total": 0},
                )

    def test_open_loop_does_not_hide_client_overload(self):
        with hub(delay=0.1) as url:
            result = self.run_load(url, "--rate", "100", "--backlog", "1")
        self.assertEqual(len(result["attempts"]), 16)
        self.assertTrue(
            any(r["reason"] == "client-backlog-full" for r in result["attempts"])
        )
        self.assertTrue(
            any(
                r["dispatch_lag_ms"] and r["dispatch_lag_ms"] > 20
                for r in result["attempts"]
            )
        )

    def test_truncated_success_is_error_and_never_retried(self):
        with hub(truncated=True) as url:
            result = self.run_load(url)
        self.assertEqual(result["summary"]["errors"], 4)
        self.assertTrue(all(r["error"] == "IncompleteRead" for r in result["attempts"]))

    def test_closed_loop_and_warmup(self):
        with hub() as url:
            result = self.run_load(url, "--mode", "closed", "--warmup", ".05")
        self.assertTrue(any(r["phase"] == "warmup" for r in result["attempts"]))
        self.assertGreater(result["summary"]["attempts"], 0)
        self.assertIsNone(result["conditions"]["offered_rate"])

    def test_scope_has_both_limits_and_checks_effective_values(self):
        with patch("shutil.which", return_value="/usr/bin/systemd-run"):
            command = matrix.scoped(["fake-hub"], True)
        self.assertIn("CPUQuota=100%", command)
        self.assertIn("MemoryMax=512M", command)
        with patch.object(Path, "read_text", return_value="0::/fake\n"):
            with self.assertRaisesRegex(RuntimeError, "cgroup delegation unavailable"):
                matrix.check_cgroup()

    def test_effective_cgroup_limits_and_missing_delegation(self):
        def contents(path, *, encoding):
            self.assertEqual(encoding, "utf-8")
            return {
                "cgroup": "0::/fake\n",
                "cpu.max": "100000 100000",
                "memory.max": "536870912",
            }[path.name]

        with patch.object(Path, "read_text", contents):
            self.assertEqual(matrix.check_cgroup()["memory_max"], "536870912")
        with patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "cgroup delegation unavailable"):
                matrix.scoped(["fake-hub"], True)

    def test_endpoint_and_percentiles(self):
        for url in (
            "http://example.invalid",
            "https://user:secret@example.invalid",
            "https://example.invalid/path",
        ):
            with self.assertRaises(ValueError):
                load.endpoint(url)
        self.assertEqual(load.percentiles([3, 1, 2])["p99"], 3)
        self.assertIsNone(load.percentiles([])["p50"])


if __name__ == "__main__":
    unittest.main()
