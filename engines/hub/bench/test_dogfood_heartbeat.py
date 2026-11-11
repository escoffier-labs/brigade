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
                "BRIGADE_BENCH_PROBE_TOKEN": "test-probe",
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


class FakeClient:
    """Records requests and replays canned acquire and release replies."""

    log = []

    def __init__(self, url, token, connection, timeout):
        self.token = token

    def request(self, method, path, body):
        payload = json.loads(body)
        FakeClient.log.append((self.token, payload))
        if payload["action"] == "acquire":
            return 200, json.dumps({"granted": True, "claim": {"generation": 7}}).encode(), {}
        return 200, b"{}", {}

    def close(self):
        pass


class ProbeBehavior(unittest.TestCase):
    def setUp(self):
        FakeClient.log = []

    def run_probe(self, records):
        with mock.patch.object(dogfood_heartbeat.load, "Client", FakeClient):
            dogfood_heartbeat.claim_probe("http://hub.invalid", "probe-secret", "bench-probe", records)

    def test_each_claim_probe_mints_a_fresh_holder(self):
        records = []
        self.run_probe(records)
        with mock.patch.object(dogfood_heartbeat.time, "time_ns", return_value=2):
            self.run_probe(records)
        holders = [p["holder"] for _, p in FakeClient.log if p["action"] == "acquire"]
        self.assertEqual(len(set(holders)), 2)
        for _, p in FakeClient.log:
            if p["action"] == "release":
                self.assertIn(p["holder"], holders)

    def test_release_reads_generation_from_nested_claim_object(self):
        self.run_probe([])
        acquire, release = (p for _, p in FakeClient.log)
        self.assertEqual((acquire["action"], release["action"]), ("acquire", "release"))
        self.assertEqual(release["generation"], 7)
        self.assertEqual(release["holder"], acquire["holder"])
        self.assertEqual(dogfood_heartbeat.claim_generation(b'{"generation": 3, "claim": {"generation": 9}}'), 9)

    def test_missing_generation_records_error_and_skips_release(self):
        class NoGeneration(FakeClient):
            def request(self, method, path, body):
                FakeClient.log.append((self.token, json.loads(body)))
                return 200, b'{"granted": true, "generation": 4}', {}

        records = []
        with mock.patch.object(dogfood_heartbeat.load, "Client", NoGeneration):
            dogfood_heartbeat.claim_probe("http://hub.invalid", "t", "n", records)
        self.assertEqual([p["action"] for _, p in FakeClient.log], ["acquire"])
        self.assertEqual(FakeClient.log[0][1]["ttl_seconds"], 60)
        self.assertEqual(dogfood_heartbeat.probe_errors(records), {"claim": 0, "claim-release": 1})

    def test_failed_acquire_skips_release(self):
        class Refusing(FakeClient):
            def request(self, method, path, body):
                FakeClient.log.append((self.token, json.loads(body)))
                return 503, b"credential-inflight-full", {}

        records = []
        with mock.patch.object(dogfood_heartbeat.load, "Client", Refusing):
            dogfood_heartbeat.claim_probe("http://hub.invalid", "t", "n", records)
        self.assertEqual(len(FakeClient.log), 1)
        self.assertEqual(dogfood_heartbeat.probe_errors(records), {"claim": 1})

    def test_probes_use_their_own_credential_and_skip_wake_by_default(self):
        stop = threading.Event()
        records = []
        real_wait = stop.wait

        def one_round(timeout):
            stop.set()
            return real_wait(0)

        with (
            mock.patch.object(dogfood_heartbeat.load, "Client", FakeClient),
            mock.patch.object(stop, "wait", side_effect=one_round),
        ):
            dogfood_heartbeat.probes("http://hub.invalid", "probe-secret", "bench-probe", stop, records)
        self.assertEqual({tok for tok, _ in FakeClient.log}, {"probe-secret"})
        self.assertEqual({r["kind"] for r in records}, {"claim"})

    def test_probe_credential_option_is_required_and_separate(self):
        env = {"BRIGADE_BENCH_TOKEN": "event", "BRIGADE_BENCH_METRICS_TOKEN": "m"}
        argv = ["dogfood_heartbeat.py", "--url", "http://hub.invalid", "--output", "x", "--fanout-cpus", "0"]
        with (
            mock.patch.object(sys, "argv", argv + ["--probe-token-env", "MY_PROBE"]),
            mock.patch.dict(os.environ, {**env, "MY_PROBE": "p"}, clear=True),
            mock.patch.object(dogfood_heartbeat, "probes") as probes,
            mock.patch.object(dogfood_heartbeat, "drive", return_value={}),
            mock.patch.object(dogfood_heartbeat, "metrics", return_value={}),
            mock.patch.object(dogfood_heartbeat.Path, "mkdir"),
            mock.patch.object(dogfood_heartbeat.Path, "write_text"),
            mock.patch.object(dogfood_heartbeat, "commit_p99", return_value=None),
            mock.patch.object(dogfood_heartbeat.os, "getloadavg", return_value=(170, 0, 0)),
        ):
            sys.argv.extend(["--workers", "0"])
            dogfood_heartbeat.main()
        self.assertTrue(probes.called)
        self.assertEqual(probes.call_args.args[1], "p")
        self.assertIsNone(probes.call_args.args[5])


if __name__ == "__main__":
    unittest.main()
