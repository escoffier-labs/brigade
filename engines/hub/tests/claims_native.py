"""Native claims preserve Python wire bytes, storage, fencing and lease expiry."""

import http.client
import json
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc  # noqa: E402 - repository-local drive helper

BINARY = ROOT / "engines/hub/target/debug/brigade-hub"
START = 1767225600
# Rows the parity run compares. Times come from the shared clock file, so they match.
STORAGE = {
    "claims": "SELECT * FROM claims ORDER BY target",
    "claim_fences": "SELECT * FROM claim_fences ORDER BY target",
    "claim_retired_holders": "SELECT * FROM claim_retired_holders ORDER BY target, owner_node, holder_token",
    # The Python dispatcher's sweep can record the same event id first, at its own time.
    "fleet_events": "SELECT event_id, name, subject, producer, data FROM fleet_events ORDER BY event_id",
}


def send(hub, method, path, token=None, body=None, raw=None):
    origin = urlsplit(hub.url)
    connection = http.client.HTTPConnection(origin.hostname, origin.port, timeout=30)
    try:
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection.request(method, path, body=payload, headers=headers)
        received = connection.getresponse()
        data = received.read()
        return SimpleNamespace(
            status=received.status,
            body=data,
            headers={k.lower(): v for k, v in received.getheaders()},
        )
    finally:
        connection.close()


def launch(state, clock, native, events=False):
    state.mkdir()
    template = f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}} --test-clock-file {clock}"
    if native:
        template += " --route-mode registration=native --route-mode claims=native"
    original = hc.hub_env

    def hub_env(*args):
        # The drive drops every BRIGADE_* variable; opt this hub into events.
        env = original(*args)
        if events:
            env["BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS"] = "events.example.test"
        return env

    hc.hub_env = hub_env
    try:
        hub = hc.launch_hub(
            template,
            brigade=[sys.executable, "-m", "brigade"],
            state_dir=state,
            startup_timeout=30,
        )
    finally:
        hc.hub_env = original
    hub.db = state / "hub.db"
    hub.tokens = {"admin": hub.admin_token, "bogus": "not-a-real-token"}
    return hub


def enroll(hub, node):
    added = send(hub, "POST", "/nodes", hub.admin_token, {"action": "add", "node_id": node})
    assert added.status == 200, added.body
    hub.tokens[node] = json.loads(added.body)["token"]


class Claims(unittest.TestCase):
    def test_python_byte_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clock = work / "clock"
            clock.write_text(str(START), encoding="utf-8")
            hubs = []
            try:
                for native in (False, True):
                    hubs.append(launch(work / ("native" if native else "proxy"), clock, native, events=True))
                for hub in hubs:
                    for node in ("node-a", "node-b", "node-c"):
                        enroll(hub, node)
                    db = sqlite3.connect(hub.db)
                    db.execute(
                        "INSERT INTO fleet_event_subscriptions(id,principal_id,name,arguments,url,secret,created_at) "
                        "VALUES('sub-claims','node-a','claim.expired','{}','https://events.example.test/hook','fake',?)",
                        (START,),
                    )
                    db.commit()
                    db.close()

                def at(offset):
                    clock.write_text(str(START + offset), encoding="utf-8")

                def compare(method, path, who=None, body=None, raw=None):
                    responses = [
                        send(hub, method, path, hub.tokens.get(who) if who else None, body, raw) for hub in hubs
                    ]
                    case = (method, path, who, body, raw)
                    self.assertEqual(responses[0].status, responses[1].status, (case, responses[0].body, responses[1].body))
                    self.assertEqual(responses[0].body, responses[1].body, case)
                    for header in ("content-type", "cache-control", "referrer-policy"):
                        self.assertEqual(responses[0].headers.get(header), responses[1].headers.get(header), case)
                    return json.loads(responses[0].body)

                def claim(who, **fields):
                    return compare("POST", "/claims", who, fields)

                lock = {"token": "lease-one", "acquired_at": "2026-01-01T00:00:00+00:00", "run_dir": "/runs/one"}
                compare("GET", "/claims", "node-a")
                compare("GET", "/claims")
                compare("GET", "/claims", "bogus")
                compare("POST", "/claims", None, {"action": "inspect"})
                compare("POST", "/claims", "bogus", {"action": "inspect"})
                compare("POST", "/claims", "admin", {"action": "inspect", "target": "repo", "node_id": "node-a"})
                compare("GET", "/claims", "admin")
                for body in (
                    ["acquire"],
                    {"action": "acquire", "node_id": "node-a"},
                    {"action": "seize", "target": "repo", "node_id": "node-a", "holder": "h"},
                    {"action": "acquire", "scope": "force", "target": "repo", "node_id": "node-a", "holder": "h"},
                    {"action": "acquire", "target": "re\tpo", "node_id": "node-a", "holder": "h"},
                    {"action": "acquire", "target": "repo", "node_id": "unknown", "holder": "h"},
                    {"action": "acquire", "target": "repo", "node_id": "node-a", "holder": "h", "conductor": "c\x7f"},
                    {"action": "acquire", "target": "repo", "node_id": "node-a", "holder": "h", "ttl_seconds": 90.0},
                    {"action": "acquire", "target": "repo", "node_id": "node-a", "holder": "h", "generation": None},
                    {"action": "acquire", "target": "repo", "node_id": "node-a", "holder": "h", "role": "x" * 129},
                    {"action": "renew", "target": "repo", "node_id": "node-a", "holder": "h", "lock": lock},
                    {"action": "acquire", "target": "repo", "node_id": "node-a", "holder": "h", "lock": {"run_dir": 3}},
                    {"action": "acquire", "scope": "node", "target": "repo", "node_id": "node-a", "holder": "h"},
                    {"action": "release", "scope": "node", "target": "repo", "node_id": "node-a"},
                    {"action": "inspect", "target": "repo", "node_id": "node-b"},
                    # Python's repr of a non-ASCII target: the native hub leaves these to Python.
                    {"action": "renew", "target": "dépôt", "node_id": "node-a", "holder": "h"},
                ):
                    compare("POST", "/claims", "node-a", body)
                # json.loads accepts NaN, which serde_json rejects.
                compare("POST", "/claims", "node-a", raw=b'{"action": "inspect", "target": "repo", "node_id": "node-a", "x": NaN}')
                compare("POST", "/claims", "node-a", raw=b"{")

                granted = claim(
                    "node-a",
                    action="acquire",
                    target="repo",
                    node_id="node-a",
                    holder="holder-one",
                    conductor=" conductor ",
                    harness="claude",
                    role="impl",
                    job="job-1",
                    session="s-1",
                    ttl_seconds=60,
                    lock=lock,
                )
                self.assertTrue(granted["granted"])
                self.assertEqual(granted["claim"]["generation"], 1)
                claim("node-b", action="acquire", target="repo", node_id="node-b", holder="holder-two")
                at(1)
                retry = claim("node-a", action="acquire", target="repo", node_id="node-a", holder="holder-one", generation=0, lock=lock)
                self.assertTrue(retry["granted"])
                claim("node-a", action="acquire", target="repo", node_id="node-a", holder="holder-one", generation=7)
                claim("node-a", action="inspect", target="repo", node_id="node-a")
                claim("node-b", action="inspect", target="repo", node_id="node-b")
                at(2)
                claim("node-a", action="renew", target="repo", node_id="node-a", holder="holder-one", ttl_seconds=120, generation=1)
                claim("node-b", action="renew", target="repo", node_id="node-b", holder="holder-two")
                claim("node-a", action="renew", target="it's", node_id="node-a", holder="holder-one")
                # A node supersedes its own dead lease and the old holder is retired.
                superseded = claim(
                    "node-a",
                    action="acquire",
                    scope="node",
                    target="repo",
                    node_id="node-a",
                    holder="holder-three",
                    supersede={"token": "lease-one", "acquired_at": "2026-01-02T00:00:00+00:00"},
                )
                self.assertEqual(superseded["claim"]["generation"], 2)
                claim("node-a", action="renew", target="repo", node_id="node-a", holder="holder-one")
                claim("node-a", action="acquire", scope="node", target="repo", node_id="node-a", holder="holder-four", supersede={"token": "other"})
                claim("node-a", action="release", target="repo", node_id="node-a", holder="holder-one")
                claim("node-b", action="release", scope="node", target="repo", node_id="node-b", acquired_at="x")
                claim("node-b", action="release", scope="force", target="repo", node_id="node-b", acquired_at="x")
                claim("node-a", action="release", target="repo", node_id="node-a", holder="holder-three", generation=1)
                released = claim("node-a", action="release", target="repo", node_id="node-a", holder="holder-three", generation=2)
                self.assertTrue(released["released"])
                claim("node-a", action="renew", target="repo", node_id="node-a", holder="holder-three")
                claim("node-a", action="release", target="repo", node_id="node-a", holder="holder-three")
                claim("node-a", action="inspect", target="repo", node_id="node-a")
                # Lease expiry: listings hide it, inspect sees a free target, and a
                # new acquire prunes it and records claim.expired for the subscriber.
                claim("node-a", action="acquire", target="short", node_id="node-a", holder="holder-five", ttl_seconds=1)
                claim("node-a", action="acquire", target="other", node_id="node-a", holder="holder-six", ttl_seconds=1)
                at(10)
                compare("GET", "/claims", "node-b")
                compare("GET", "/claims?all=1", "node-b")
                compare("GET", "/claims?all=YES&all=0", "node-b")
                claim("node-b", action="inspect", target="short", node_id="node-b")
                claim("node-a", action="renew", target="short", node_id="node-a", holder="holder-five")
                taken = claim("node-b", action="acquire", target="short", node_id="node-b", holder="holder-seven")
                # The prune ends the lapsed tenure (2), then the new holder starts at 3.
                self.assertEqual(taken["claim"]["generation"], 3)
                claim("node-a", action="acquire", target="short", node_id="node-a", holder="holder-five")
                # A lapsed lease released before any prune also records the expiry.
                claim("node-a", action="acquire", target="third", node_id="node-a", holder="holder-eight", ttl_seconds=1)
                at(20)
                claim("node-a", action="release", target="third", node_id="node-a", holder="holder-eight")
                # An operator release binds to the fence observed at ingress.
                claim("node-c", action="acquire", target="ops", node_id="node-c", holder="holder-nine", ttl_seconds=600)
                listed = compare("GET", "/claims", "node-c")
                acquired = [c for c in listed["claims"] if c["target"] == "ops"][0]["acquired_at"]
                claim("node-c", action="release", scope="node", target="ops", node_id="node-c", acquired_at=acquired)
                claim("node-c", action="release", scope="force", target="ops", node_id="node-c")
                claim("node-a", action="acquire", target="t" * 1025, node_id="node-a", holder="holder-ten")
                compare("POST", "/nodes", "admin", {"action": "revoke", "node_id": "node-b"})
                compare("POST", "/claims", "node-b", {"action": "inspect", "target": "short", "node_id": "node-b"})
                compare("GET", "/claims", "node-b")
                compare("GET", "/claims?all=1", "admin")

                dumps = []
                for hub in hubs:
                    db = sqlite3.connect(hub.db)
                    dumps.append({name: db.execute(sql).fetchall() for name, sql in STORAGE.items()})
                    db.close()
                self.assertEqual(dumps[0], dumps[1])
                self.assertTrue(dumps[1]["fleet_events"], "the subscribed expiry was recorded")
            finally:
                for hub in hubs:
                    hc.stop_hub(hub)

    def test_concurrent_clients_never_share_a_target(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clock = work / "clock"
            clock.write_text(str(START), encoding="utf-8")
            hub = launch(work / "native", clock, True)
            try:
                for node in ("node-a", "node-b"):
                    enroll(hub, node)
                granted, lock = [], threading.Lock()

                def client(index):
                    node = "node-a" if index % 2 else "node-b"
                    for round_ in range(4):
                        holder = f"holder-{index}-{round_}"
                        body = {"action": "acquire", "target": "contended", "node_id": node, "holder": holder}
                        response = send(hub, "POST", "/claims", hub.tokens[node], body)
                        decision = json.loads(response.body)
                        if response.status == 200 and decision.get("granted"):
                            with lock:
                                granted.append(decision["claim"]["generation"])
                            release = dict(body, action="release")
                            done = send(hub, "POST", "/claims", hub.tokens[node], release)
                            assert json.loads(done.body)["released"] is True, done.body

                threads = [threading.Thread(target=client, args=(i,)) for i in range(64)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertTrue(granted)
                # Each grant advanced the fence and its release advanced it again,
                # so no two holders ever shared or repeated a generation.
                self.assertEqual(len(granted), len(set(granted)))
                self.assertTrue(all(g % 2 == 1 for g in granted), granted)
                db = sqlite3.connect(hub.db)
                fence = db.execute("SELECT generation FROM claim_fences WHERE target='contended'").fetchone()[0]
                db.close()
                self.assertEqual(fence, 2 * len(granted))
                # With the Python upstream paused, claims still answer: they are native.
                pid = hub.process.pid
                with open(f"/proc/{pid}/task/{pid}/children", encoding="utf-8") as handle:
                    upstream = [int(child) for child in handle.read().split()]
                self.assertTrue(upstream)
                for child in upstream:
                    os.kill(child, signal.SIGSTOP)
                try:
                    body = {"action": "acquire", "target": "paused", "node_id": "node-a", "holder": "holder-paused"}
                    paused = send(hub, "POST", "/claims", hub.tokens["node-a"], body)
                    self.assertEqual(paused.status, 200, paused.body)
                    listed = send(hub, "GET", "/claims", hub.tokens["node-b"])
                    self.assertIn(b'"target": "paused"', listed.body)
                finally:
                    for child in upstream:
                        os.kill(child, signal.SIGCONT)
            finally:
                hc.stop_hub(hub)


if __name__ == "__main__":
    unittest.main()
