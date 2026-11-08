"""S10: native writes continue while the managed upstream crashes, and it comes back.

With a native write family, killing the Python upstream must leave native
claims committing, proxied routes answering 502 and `/ready` answering 503.
The hub restarts the upstream with backoff, and `/ready` returns to 200.

The negative check runs the same probe against a hub with every family
proxied. That hub keeps the S3b rule and exits when its child dies, so the
probe must report failures. A probe that passed there would prove nothing.
"""

import json
import os
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_contract as hc  # noqa: E402 - repository-local drive helper
from claims_native import send  # noqa: E402 - shared HTTP helper

BINARY = Path(os.environ.get("BRIGADE_HUB_BINARY", ROOT / "engines/hub/target/debug/brigade-hub"))
NATIVE = ("--route-mode", "registration=native", "--route-mode", "claims=native")


def launch(state: Path, modes: tuple[str, ...]):
    state.mkdir()
    template = f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}} " + " ".join(modes)
    hub = hc.launch_hub(template, brigade=[sys.executable, "-m", "brigade"], state_dir=state, startup_timeout=30)
    hub.db = state / "hub.db"
    return hub


def upstream_pid(parent: int) -> int | None:
    """The managed Python upstream: the child of `parent` running `brigade fleet serve`."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        if ppid == parent and b"fleet" in cmdline and b"serve" in cmdline:
            return int(entry.name)
    return None


def wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def answer(hub, method, path, token=None, body=None):
    try:
        return send(hub, method, path, token, body)
    except OSError:
        return None


def status(hub, method, path, token=None, body=None) -> int:
    reply = answer(hub, method, path, token, body)
    return reply.status if reply is not None else 0


def crash_probe(hub) -> list[str]:
    """Kill the upstream and record every S10 expectation that does not hold."""
    failures: list[str] = []
    added = send(hub, "POST", "/nodes", hub.admin_token, {"action": "add", "node_id": "node-a"})
    if added.status != 200:
        return [f"enrollment answered {added.status}"]
    node = json.loads(added.body)["token"]
    if not wait_for(lambda: status(hub, "GET", "/ready") == 200, 10):
        return ["/ready never turned 200 before the crash"]
    first = upstream_pid(hub.process.pid)
    if first is None:
        return ["no managed upstream found"]
    os.kill(first, signal.SIGKILL)
    if not wait_for(lambda: status(hub, "GET", "/ready") == 503, 5):
        failures.append("/ready did not answer 503 after the upstream died")
    proxied = answer(hub, "GET", "/status", hub.admin_token)
    if proxied is None or proxied.status != 502 or json.loads(proxied.body).get("reason") != "upstream-down":
        failures.append(f"proxied /status answered {proxied and proxied.status} instead of 502 upstream-down")
    claim = {"action": "acquire", "target": "repo-during-crash", "node_id": "node-a", "holder": "h"}
    granted = status(hub, "POST", "/claims", node, claim)
    if granted != 200:
        failures.append(f"native claim during the crash answered {granted}")
    else:
        with sqlite3.connect(hub.db) as db:
            rows = db.execute("SELECT COUNT(*) FROM claims WHERE target='repo-during-crash'").fetchone()[0]
        if rows != 1:
            failures.append("the native claim during the crash did not commit")
    if not wait_for(lambda: status(hub, "GET", "/ready") == 200, 30):
        failures.append("/ready did not return to 200 after the restart")
        return failures
    second = upstream_pid(hub.process.pid)
    if second in (None, first):
        failures.append("no new managed upstream after the restart")
    if status(hub, "GET", "/status", hub.admin_token) != 200:
        failures.append("proxied /status did not recover after the restart")
    metrics = answer(hub, "GET", "/metrics", hub.admin_token)
    if metrics is None or "brigade_hub_upstream_restarts_total 1\n" not in metrics.body.decode():
        failures.append("metrics did not count one upstream restart")
    return failures


class UpstreamRestart(unittest.TestCase):
    def test_native_writes_continue_while_the_upstream_crashes(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = launch(Path(directory) / "native", NATIVE)
            try:
                self.assertEqual(crash_probe(hub), [])
                self.assertIsNone(hub.process.poll(), "the hub exited during the crash")
            finally:
                hc.stop_hub(hub)
            # The restarted upstream stops cleanly with the hub.
            self.assertEqual(hub.process.returncode, 0, hub.log.read_text(errors="replace")[-2000:])

    def test_negative_every_family_proxied_exits_and_the_probe_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = launch(Path(directory) / "proxy", ())
            try:
                failures = crash_probe(hub)
                self.assertTrue(failures, "the probe passed a hub that cannot restart its upstream")
                self.assertTrue(wait_for(lambda: hub.process.poll() is not None, 10), "the hub kept running")
                self.assertNotEqual(hub.process.returncode, 0)
            finally:
                hc.stop_hub(hub)


if __name__ == "__main__":
    unittest.main()
