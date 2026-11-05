"""The Python hub memory client (S14) against a native-memory Rust hub.

Run from engines/hub with Brigade importable (CI installs it with `pip install -e ../..`):

    BRIGADE_HUB_BINARY=/path/to/brigade-hub python tests/memory_client.py
"""

import json
import os
import secrets
import signal
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc  # noqa: E402 - repository-local drive helper

from brigade import memory_hub_client as hubc  # noqa: E402
from brigade import memory_hub_search as hub_search  # noqa: E402
from brigade import memory_hub_store as store  # noqa: E402
from brigade import memory_hub_sync as sync  # noqa: E402

BINARY = Path(os.environ.get("BRIGADE_HUB_BINARY", ROOT / "engines/hub/target/debug/brigade-hub"))
TEMPLATE = (
    f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}}"
    " --route-mode registration=native --route-mode memory=native"
)
SCOPE = "repo:client/it"
SENTINEL = "sentinel" + secrets.token_hex(6)


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.work = Path(cls.directory.name)
        (cls.work / "hub").mkdir()
        cls.hub = hc.launch_hub(
            TEMPLATE, brigade=[sys.executable, "-m", "brigade"], state_dir=cls.work / "hub", startup_timeout=30
        )
        node = hc.request(
            cls.hub.url,
            "POST",
            "/nodes",
            body={"action": "add", "node_id": "memory-client-it"},
            token=cls.hub.admin_token,
        )
        assert node.status == 200, node
        cls.node_token = node.json["token"]
        cls.saved_env = {k: os.environ.get(k) for k in ("BRIGADE_HOME", "BRIGADE_FLEET_HUB_URL", "BRIGADE_FLEET_NODE_TOKEN")}
        os.environ.update(
            BRIGADE_HOME=str(cls.work / "home"),
            BRIGADE_FLEET_HUB_URL=cls.hub.url,
            BRIGADE_FLEET_NODE_TOKEN=cls.node_token,
        )
        os.environ.pop("BRIGADE_FLEET_TOKEN", None)

    @classmethod
    def tearDownClass(cls):
        hc.stop_hub(cls.hub)
        for key, value in cls.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.directory.cleanup()

    def workspace(self, name: str, mode: str, **cards: str) -> Path:
        root = self.work / name
        (root / "memory" / "cards").mkdir(parents=True)
        (root / ".brigade").mkdir()
        (root / ".brigade" / "memory-care.toml").write_text(
            f'[hub]\nmode = "{mode}"\nscope = "{SCOPE}"\nread_scopes = ["fleet", "{SCOPE}"]\n', encoding="utf-8"
        )
        for card, text in cards.items():
            (root / "memory" / "cards" / f"{card}.md").write_text(text, encoding="utf-8")
        return root

    def client(self) -> hubc.MemoryHubClient:
        return hubc.MemoryHubClient(self.hub.url, self.node_token)

    def log_puts(self, card_id: str) -> list:
        events, after = [], 0
        while True:
            page = self.client().call("log", after_seq=after, limit=500)
            events += [e for e in page["events"] if e.get("card_id") == card_id and e["kind"] == "card.put"]
            if page["next_seq"] <= after:
                return events
            after = page["next_seq"]

    def test_put_pull_and_search_through_the_hub(self):
        writer = self.workspace("writer", "sync", kiwi=f"# Kiwi\nkiwifruit {'grows on vines ' * 3000}\n")
        settings = hubc.load_settings(writer)
        first = sync.push(writer, hubc.client_for(settings), settings)
        self.assertEqual((first["needs_confirmation"], first["card_count"]), (True, 1))
        report = sync.push(writer, hubc.client_for(settings), settings, yes=True)
        self.assertEqual([p["card_id"] for p in report["published"]], ["kiwi"], report)
        reader = self.workspace("reader", "read")
        settings = hubc.load_settings(reader)
        pulled = sync.pull(reader, hubc.client_for(settings), settings)
        head = store.replica(reader).heads[store.Replica.key(SCOPE, "kiwi")]
        # The card is longer than one 30,000-character chunk.
        self.assertGreater(len(head["text"]), 30_000)
        self.assertEqual(head["text"], (writer / "memory/cards/kiwi.md").read_text(encoding="utf-8"))
        self.assertEqual(pulled["log_epoch"], self.client().call("status")["log_epoch"])
        found = hub_search.search_payload(reader, "kiwifruit", settings=settings)
        self.assertFalse(found["retrieval"]["degraded"], found)
        self.assertEqual([(m["card_id"], m["source"], m["scope"]) for m in found["matches"]], [("kiwi", "hub", SCOPE)])

    def test_spool_replays_after_an_outage_without_duplicates(self):
        target = self.workspace("spool", "sync", fig="# Fig\nfirst\n")
        settings = hubc.load_settings(target)
        sync.push(target, hubc.client_for(settings), settings, yes=True)
        (target / "memory/cards/fig.md").write_text("# Fig\nsecond\n", encoding="utf-8")
        # A hung hub: every call times out.
        os.kill(self.hub.process.pid, signal.SIGSTOP)
        saved = hubc.CALL_TIMEOUT_SECONDS
        hubc.CALL_TIMEOUT_SECONDS = 1.0
        try:
            report = sync.push(target, hubc.client_for(settings), settings)
        finally:
            hubc.CALL_TIMEOUT_SECONDS = saved
            os.kill(self.hub.process.pid, signal.SIGCONT)
        self.assertEqual([s["card_id"] for s in report["spooled"]], ["fig"], report)
        [entry] = store.spool_entries(target, hubc.hub_identity(self.hub.url))
        # Replay the spooled put once by hand, as a crash after commit would leave it.
        fields = ("scope", "card_id", "base_revision", "base_epoch", "text", "origin", "client_event_id")
        body = {k: entry[k] for k in fields if k in entry}
        committed = self.client().call("put", **body)
        report = sync.push(target, hubc.client_for(settings), settings)
        self.assertEqual(report["published"], [{"card_id": "fig", "revision": committed["event"]["revision"]}])
        self.assertEqual(store.spool_count(target), 0)
        self.assertEqual(len(self.log_puts("fig")), 2)

    def test_conflicting_offline_edit_becomes_a_proposal(self):
        target = self.workspace("conflict", "sync", pear="# Pear\nmine\n")
        settings = hubc.load_settings(target)
        published = sync.push(target, hubc.client_for(settings), settings, yes=True)["published"][0]
        epoch = self.client().call("status")["log_epoch"]
        self.client().call(
            "put",
            scope=SCOPE,
            card_id="pear",
            base_revision=published["revision"],
            base_epoch=epoch,
            text="theirs",
            client_event_id=hubc.event_id(),
        )
        (target / "memory/cards/pear.md").write_text("# Pear\nmine, edited\n", encoding="utf-8")
        report = sync.push(target, hubc.client_for(settings), settings)
        self.assertEqual([p["reason"] for p in report["proposals"]], ["memory_base_mismatch"], report)
        card = self.client().call("card", scope=SCOPE, card_id="pear")["card"]
        self.assertEqual(card["text"], "theirs\n")

    def test_refusals_carry_the_hub_code(self):
        target = self.workspace("refused", "sync", plum="# Plum\n")
        result = sync.import_cards(
            target, self.client(), scope=SCOPE, paths=[target / "memory/cards/plum.md"]
        )["results"][0]
        self.assertEqual((result["status"], result["code"], result["http"]), ("refused", "memory_grant_missing", 403))
        with self.assertRaises(hubc.HubRefusal) as caught:
            self.client().call("card", scope=SCOPE, card_id="absent")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "memory_card_not_found"))

    def test_a_prompt_query_reaches_no_file_log_or_metric(self):
        target = self.workspace("sentinel", "read", lime="# Lime\nnothing\n")
        settings = hubc.load_settings(target)
        query = f"remember {SENTINEL} please"
        got = hub_search.search_payload(target, query, query_source="prompt", settings=settings)
        self.assertFalse(got["retrieval"]["degraded"], got)
        metrics = hc.request(self.hub.url, "GET", "/metrics", token=self.hub.admin_token)
        self.assertNotIn(SENTINEL.encode(), metrics.body)
        for root in (self.work / "hub", target):
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(SENTINEL.encode(), path.read_bytes(), path)

    def test_status_names_the_principal_and_the_readable_scopes(self):
        body = self.client().call("status", scopes=["fleet", SCOPE, "principal:someone-else"])
        self.assertEqual((body["principal"], body["credential"]), ("memory-client-it", "node"))
        self.assertEqual(body["readable_scopes"], sorted(["fleet", SCOPE]))
        self.assertNotIn(self.node_token, json.dumps(body))
        target = self.workspace("principal", "read")
        settings = hubc.load_settings(target)
        sync.pull(target, hubc.client_for(settings), settings)
        self.assertEqual(store.replica(target).meta["principal"], "memory-client-it")

    def test_the_admin_token_is_never_used_for_memory(self):
        target = self.workspace("admin-only", "read", lime="# Lime\nlocal lime\n")
        saved = os.environ.pop("BRIGADE_FLEET_NODE_TOKEN")
        os.environ["BRIGADE_FLEET_TOKEN"] = self.hub.admin_token
        try:
            settings = hubc.load_settings(target)
            got = hub_search.search_payload(target, "lime", settings=settings)
        finally:
            os.environ["BRIGADE_FLEET_NODE_TOKEN"] = saved
            os.environ.pop("BRIGADE_FLEET_TOKEN", None)
        self.assertEqual(settings.token, "")
        self.assertEqual(got["retrieval"]["reason"], "no-node-credential", got)

    def test_zz_search_answers_locally_when_the_hub_stops(self):
        # Runs last: it stops the shared hub.
        target = self.workspace("fallback", "read", plum="# Plum\nquince local\n")
        settings = hubc.load_settings(target)
        self.client().call(
            "put", scope="fleet", card_id="quince", base_revision=None, text="quince hub", client_event_id=hubc.event_id()
        )
        sync.pull(target, hubc.client_for(settings), settings)
        hc.stop_hub(self.hub)
        got = hub_search.search_payload(target, "quince", settings=settings)
        self.assertEqual(got["retrieval"]["reason"], "unreachable", got)
        self.assertEqual({(m["card_id"], m["source"]) for m in got["matches"]}, {("plum", "local"), ("quince", "replica")})
        status = sync.status(target, settings, hubc.client_for(settings))
        self.assertEqual(status["hub"], {"available": False, "reason": "unreachable"})
        self.assertNotIn(self.node_token, json.dumps(status))


if __name__ == "__main__":
    unittest.main()
