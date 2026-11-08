"""The dogfood deployment caller drives load without a local database."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import dogfood_heartbeat
import heartbeat


class StubHub(BaseHTTPRequestHandler):
    """Answers every heartbeat POST with 200 and counts it."""

    posts = 0
    lock = threading.Lock()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/events":
            with StubHub.lock:
                StubHub.posts += 1
        self.reply(b'{"ok": true}')

    def do_GET(self):
        self.reply(b"")

    def reply(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DogfoodWithoutDatabase(unittest.TestCase):
    def test_deployment_caller_generates_requests_without_a_database(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), StubHub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        StubHub.posts = 0
        commands = []
        real_popen = subprocess.Popen

        def spy(command, *args, **kwargs):
            commands.append(list(command))
            return real_popen(command, *args, **kwargs)

        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / "dogfood"
            argv = [
                "dogfood_heartbeat.py",
                "--url",
                f"http://127.0.0.1:{server.server_port}",
                "--output",
                str(output),
                "--fanout-cpus",
                "0",
                "--workers",
                "0",
                "--seconds",
                "0.5",
                "--load-min",
                "0",
                "--load-max",
                "1000000",
                "--load-deadline",
                "5",
            ]
            env = {
                "BRIGADE_BENCH_TOKEN": "test-node",
                "BRIGADE_BENCH_RECEIVER_TOKEN": "test-receiver",
                "BRIGADE_BENCH_METRICS_TOKEN": "test-metrics",
            }
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(os.environ, env),
                mock.patch.object(dogfood_heartbeat, "metrics", return_value={}) as caller_metrics,
                mock.patch.object(heartbeat, "metrics", return_value={}) as driver_metrics,
                mock.patch.object(dogfood_heartbeat, "probes"),
                mock.patch.object(heartbeat.subprocess, "Popen", side_effect=spy),
            ):
                dogfood_heartbeat.main()
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))

        self.assertTrue(caller_metrics.called)
        self.assertTrue(driver_metrics.called)
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertNotIn("--database", command)
            self.assertNotIn("None", command)
            self.assertEqual(command[command.index("--batch-events") + 1], "1")
        events = summary["events"]
        self.assertEqual(events["initial_database"]["status"], "not supplied")
        self.assertIsNone(events["cgroup_limits"])
        self.assertEqual((events["batch_events"], events["nodes"]), (1, 1))
        self.assertGreater(StubHub.posts, 0)
        self.assertGreater(events["ok_per_second"], 0)


if __name__ == "__main__":
    unittest.main()
