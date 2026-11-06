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
def hub(status=200, delay=0, truncated=False, metrics_text=None):
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
            if metrics and metrics_text is not None:
                body = metrics_text().encode()
            else:
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

    def test_metrics_text_fills_the_telemetry_columns(self):
        # Each scrape sees a larger queue, batch and stall count, as under writes.
        scrapes = iter(range(1, 1000))

        def exposition():
            n = next(scrapes)
            return (
                "# HELP brigade_hub_writer_queue_depth Commands waiting.\n"
                f"brigade_hub_writer_queue_depth {n}\n"
                f"brigade_hub_writer_batch_size {n * 10}\n"
                "brigade_hub_writer_apply_seconds 0.002\n"
                "brigade_hub_reader_queue_depth 0\n"
                "brigade_hub_cpu_queue_depth NaN\n"
                f"brigade_hub_checkpoint_busy_total {n // 2}\n"
                f"brigade_hub_checkpoint_stall_frames {n * 100}\n"
                'brigade_hub_requests_total{family="health",status_class="2xx"} 1\n'
            )

        with hub(metrics_text=exposition) as url:
            result = self.run_load(url, "--sample-interval", ".05")
        telemetry = result["telemetry"]
        sample = telemetry["samples"][0]
        self.assertEqual(sample["status"], 200)
        self.assertIsNone(sample["value"]["brigade_hub_cpu_queue_depth"])
        queue = telemetry["queue_depth"]
        self.assertEqual(set(queue), {"writer_queue_depth", "reader_queue_depth"})
        self.assertEqual(queue["writer_queue_depth"]["metric"], "brigade_hub_writer_queue_depth")
        self.assertGreater(queue["writer_queue_depth"]["samples"], 1)
        self.assertGreater(queue["writer_queue_depth"]["max"], queue["writer_queue_depth"]["min"])
        self.assertEqual(queue["reader_queue_depth"]["max"], 0)
        sizes = telemetry["batch_sizes"]["writer_batch_size"]
        self.assertEqual(sizes["values"][0], 10)
        self.assertEqual(sizes["last"], sizes["values"][-1])
        stalls = telemetry["checkpoint_stalls"]
        self.assertEqual(set(stalls), {"checkpoint_busy_total", "checkpoint_stall_frames"})
        self.assertGreater(stalls["checkpoint_stall_frames"]["last"], stalls["checkpoint_stall_frames"]["first"])
        json.dumps(result, allow_nan=False)

    def test_nan_metrics_stay_unavailable(self):
        # A proxy front exports NaN for the writer and no checkpoint stall value.
        text = (
            "brigade_hub_writer_queue_depth NaN\n"
            "brigade_hub_writer_batch_size NaN\n"
            "brigade_hub_checkpoint_stall_frames NaN\n"
        )
        with hub(metrics_text=lambda: text) as url:
            result = self.run_load(url)
        telemetry = result["telemetry"]
        for column in ("queue_depth", "batch_sizes", "checkpoint_stalls"):
            self.assertEqual(telemetry[column], load.UNAVAILABLE)
        self.assertEqual(telemetry["samples"][0]["status"], 200)

    def test_parse_metrics(self):
        raw = b'# HELP x y\nbrigade_hub_a 3\nbrigade_hub_b NaN\nbrigade_hub_c{l="v w"} 1.5\nbad line\n'
        self.assertEqual(
            load.parse_metrics(raw),
            {"brigade_hub_a": 3.0, "brigade_hub_b": None, 'brigade_hub_c{l="v w"}': 1.5},
        )
        self.assertEqual(load.parse_metrics(b'{"a": 1}'), {"a": 1})

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

    def test_open_loop_throughput_excludes_queue_drain(self):
        # 16 arrivals at 100/s against a 100 ms server drain for about 1.6 s.
        with hub(delay=0.1) as url:
            result = self.run_load(url, "--rate", "100", "--backlog", "64")
        summary = result["summary"]
        self.assertLess(summary["completed_per_second"], 25)
        self.assertGreater(summary["completed_after_window"], 0)
        self.assertGreater(summary["drain_seconds"], 0.5)
        self.assertTrue(all(r["completed_s"] is not None for r in result["attempts"]))

    def test_flags_value_that_starts_with_dashes_parses(self):
        # matrix.py passes the flags as one token because argparse rejects a
        # separate value that starts with "--".
        args = load.parser().parse_args(
            [
                "--url",
                "http://127.0.0.1:1",
                "--output",
                "report.json",
                "--target",
                "python",
                "--flags=--allow-admin-writes; managed upstream",
            ]
        )
        self.assertEqual(args.flags, "--allow-admin-writes; managed upstream")
        self.assertIn('"--flags=--allow-admin-writes"', Path(matrix.__file__).read_text(encoding="utf-8"))

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

    def test_batch_events_post_unique_lists(self):
        bodies = []

        def request(client, method, path, body):
            if path != "/metrics":
                bodies.append(json.loads(body))
            return 200, b"{}", None

        with patch.object(load.Client, "request", request):
            result = self.run_load("http://127.0.0.1:9", "--batch-events", "3")
        self.assertEqual(result["conditions"]["payload"]["events_per_post"], 3)
        self.assertTrue(bodies and all(isinstance(b, list) and len(b) == 3 for b in bodies))
        sequences = [event["sequence"] for body in bodies for event in body]
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertEqual(len({event["digest"] for body in bodies for event in body}), len(sequences))

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
