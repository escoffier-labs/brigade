"""The Python hub memory client (S14) against a native-memory Rust hub.

Run from engines/hub with Brigade importable (CI installs it with `pip install -e ../..`):

    BRIGADE_HUB_BINARY=/path/to/brigade-hub python tests/memory_client.py
"""

import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc  # noqa: E402 - repository-local drive helper

from brigade import memory_hub_client as hubc  # noqa: E402
from brigade import memory_hub_import as importer  # noqa: E402
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

    def test_production_verifier_proposal_automatic_admission(self):
        """A _propose correction applies only when its class has a hub go record."""
        from dataclasses import replace
        import re
        import time
        from brigade import memory_fairness as fairness, memory_proposal_io as io
        from brigade import memory_verify_auto as automatic, memory_verify_cmd as cmd
        from brigade import memory_verify_contract as contract, memory_verify_run as runner
        from brigade import memory_verify_eval as evaluation, memory_verify_gate as gate
        from brigade import memory_verify_seat as seats, memory_verify_state as vstate

        value = evaluation.read_corpus(ROOT / "tests/fixtures/memory_verify_eval")[0]
        value["card"]["scope"] = "fleet"
        card_id = value["card"]["card_id"]
        replacement = value["expected_ops"][0]["text"]
        commit = next(iter(value["commits"].values()))
        quotes = [f'"commit": "{commit}"', f"to commit {commit} on 2026-11-02"]

        def scripted(prompt):
            starts = list(re.finditer(r"<<([0-9a-f]{32}) evidence (\d+) [^\n]*>>\n", prompt))
            regions = [(m[2], prompt[m.end():prompt.index(f"<<{m[1]} ", m.end())]) for m in starts]
            return json.dumps({"schema": contract.SCHEMA, "card_id": card_id, "blocks": [{
                "block_id": "c-abcdef", "verdict": "contradicted", "confidence": "high",
                "quotes": [{"evidence": int(index), "text": quote} for quote in quotes
                           for index, text in regions if quote in text],
                "replacement": replacement, "previously": None, "superseded_by": None,
            }]})

        admin = hubc.MemoryHubClient(self.hub.url, self.hub.admin_token)
        registered = hc.request(self.hub.url, "POST", "/nodes", token=self.hub.admin_token,
                                body={"action": "add", "node_id": "verify-automatic-it"})
        self.assertEqual(registered.status, 200, registered)
        client = hubc.MemoryHubClient(self.hub.url, registered.json["token"])
        status = client.call("status")
        principal, epoch = status["principal"], status["log_epoch"]
        for action, fields in [
            ("grant", {"scope": "fleet", "rights": ["read", "reflect", "review"]}),
            ("verifier", {"verifier": True}),
        ]:
            admin.call(action, principal=principal, client_event_id=hubc.event_id(), **fields)
        with sqlite3.connect(self.hub.state_dir / "hub.db") as db:
            db.execute("UPDATE fleet_policy_revisions SET document=?", (
                json.dumps({"memory": {"curation_owner": "hub", "auto_apply": True}}),
            ))
        target = self.workspace("verify-automatic", "read")
        evidence_root = self.work / "verify-evidence"
        evidence_root.mkdir()
        (target / ".brigade/memory-care.toml").write_text(
            '[hub]\nmode="read"\nread_scopes=["fleet"]\n[verify]\nenabled=true\n'
            f'[[verify.workspace]]\nscope="fleet"\npath="{evidence_root}"\narchive_projects=[]\n'
        )
        # Persist the fixture evidence so show_payload also checks real file freshness.
        for item in value["evidence"]:
            path = evidence_root / item["source"].split(":", 3)[3]
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(item["text"])
            item["file_sha256"] = io.sha256(path.read_bytes())
        root = target / gate.ROOT
        root.mkdir(parents=True)
        policy = {**gate.DEFAULT_POLICY, "min_cases": 1, "repeats": 1, "consecutive_passes": 1}
        (target / gate.POLICY).write_bytes(io.json_bytes(policy))
        (target / gate.CORPUS).parent.mkdir()
        (target / gate.CORPUS).write_bytes(io.json_bytes({"schema": "brigade.memory-verify-corpus.v1", "cases": [value]}))
        pair = tuple(seats.Seat(role=role, name=role, cli="claude", adapter="claude", model=model,
            model_key=("anthropic", model), argv=("claude",), executable="/usr/bin/true",
            identity=(1, 2), fingerprint="0" * 64)
            for role, model in (("propose", "test-small"), ("confirm", "test-large")))
        binding = cmd._binding(client.identity, principal, epoch)
        settings = hubc.load_settings(target)

        # Each case is a fresh real proposal, with a correction followed by _manifest_op.
        for admitted_class in ("confirm", "correct"):
            # Independent class cases must clear the shared principal attempt window.
            if admitted_class == "correct":
                time.sleep(1.01)
            with self.subTest(admitted_class=admitted_class):
                card_id = value["card"]["card_id"] = f"verify-{admitted_class}"
                admin.call("put", scope="fleet", card_id=card_id, text=value["card"]["text"],
                           base_revision=None, base_epoch=epoch,
                           client_event_id=hubc.event_id())
                before = client.call("card", scope="fleet", card_id=card_id)["card"]
                original, assembly = evaluation.inputs(value)
                card = replace(original, revision=before["revision"])
                result = contract.verify_card(card, assembly, propose=scripted,
                    confirm=scripted, resolve_commit=value["commits"].get)
                self.assertIsNotNone(result.proposal, result)
                now = int(time.time() * 1000)
                key = fairness.key_digest("fleet", [card_id])
                state = vstate.fresh()
                state.update(binding=binding, scopes={"fleet": {"cards": {key: {"seen": now, "proposed": 0}}}})
                sender = runner.Sender(target=target, state=state, client=client, tally=runner.Tally({}),
                    tables={"fleet": state["scopes"]["fleet"]["cards"]}, epoch=epoch, observed=now,
                    now=now, pending=0, state_principal=principal, bound=frozenset({"fleet"}))
                runner._propose(sender, card=card, result=result, assembly=assembly,
                    calls=runner.Calls(None), pair=pair, dry_run=False)
                self.assertEqual(len(sender.tally.proposals), 1, sender.tally)
                ident = sender.tally.proposals[0]["proposal_id"]
                shown = client.call("proposal_show", proposal_id=ident)
                self.assertEqual(len(shown["verifier_manifests"]), 1)
                with sqlite3.connect(self.hub.state_dir / "fleet-hub-memory.db") as db:
                    stored = json.loads(db.execute("SELECT body FROM memory_proposals WHERE proposal_id=?", (ident,)).fetchone()[0])
                self.assertEqual([op["type"] for op in stored["ops"]], ["replace_block", "append_evidence"])
                report = evaluation.evaluate([value], propose=scripted, confirm=scripted, repeats=1)
                if admitted_class == "confirm":
                    report["classes"]["confirm"] = report["classes"].pop("correct")
                    report["ops"]["append_evidence"] = report["ops"]["replace_block"]
                record = gate.record(target, report, pair=pair, binding=binding, now_ms=now)
                admin.call("verify_eval.record", principal=principal, base_epoch=epoch,
                    record=record, policy=gate.policy_bytes(target).decode(), client_event_id=hubc.event_id())
                with patch.object(cmd, "_hub", return_value=(settings, client)):
                    applied = automatic.apply_pending(target, state, client, pair=pair, now_ms=int(time.time() * 1000))
                if admitted_class == "correct":
                    self.assertEqual(applied, [{"proposal_id": ident, "state": "applied", "decision_mode": "automatic"}])
                    self.assertIn(replacement, client.call("card", scope="fleet", card_id=card_id)["card"]["text"])
                else:
                    self.assertEqual(applied, [])
                    # Bypass the client prefilter to prove that the hub also refuses this class.
                    with self.assertRaises(hubc.HubRefusal) as refused:
                        client.call("verify_auto_apply", proposal_id=ident, base_epoch=epoch,
                            binding_sha256=record["binding_sha256"], policy_sha256=record["policy_sha256"],
                            expected_op_types=["replace_block", "append_evidence"], client_event_id=hubc.event_id())
                    self.assertIn("verify_class_not_admitted", str(refused.exception.body))
                    self.assertEqual(client.call("card", scope="fleet", card_id=card_id)["card"], before)
                    admin.call("decide", proposal_id=ident, decision="reject", base_epoch=epoch,
                        client_event_id=hubc.event_id())

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
        report = importer.run(
            self.client(),
            scope=SCOPE,
            paths=[target / "memory/cards/plum.md"],
            root=None,
            excludes=[],
            dry_run=False,
            update=False,
            yes=True,
        )
        [item] = report["items"]
        self.assertEqual(item.result, {"status": "refused", "code": "memory_grant_missing", "http": 403})
        self.assertFalse(report["ok"])
        with self.assertRaises(hubc.HubRefusal) as caught:
            self.client().call("card", scope=SCOPE, card_id="absent")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "memory_card_not_found"))

    def test_an_import_dry_run_plans_with_the_native_scanner_ruleset(self):
        # The dry run refuses unless the hub reports this client's ruleset id and digest.
        target = self.workspace("dry-run", "read", kiwi="# Kiwi\nfruit\n", mango="# Mango\n\u202e\n")
        report = importer.run(
            self.client(),
            scope=SCOPE,
            paths=[target / "memory/cards"],
            root=None,
            excludes=[],
            dry_run=True,
            update=False,
            yes=False,
        )
        self.assertEqual([(i.path, i.action) for i in report["items"]], [("kiwi.md", "import"), ("mango.md", "blocked")])
        status = self.client().call("status")
        self.assertEqual(status["scan"]["ruleset_version"], importer.scanner.ruleset_version())

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


class RestoredMemoryFile(unittest.TestCase):
    """#2292: `brigade-hub memory-restore-epoch` on a restored copy, then a client pull."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.work = Path(self.directory.name)
        (self.work / "hub").mkdir()
        names = ("BRIGADE_HOME", "BRIGADE_FLEET_HUB_URL", "BRIGADE_FLEET_NODE_TOKEN")
        self.saved_env = {k: os.environ.get(k) for k in names}
        self.saved_free_port = hc.free_port
        self.hub = None

    def tearDown(self):
        hc.free_port = self.saved_free_port
        if self.hub is not None:
            hc.stop_hub(self.hub)
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.directory.cleanup()

    def launch(self):
        self.hub = hc.launch_hub(
            TEMPLATE, brigade=[sys.executable, "-m", "brigade"], state_dir=self.work / "hub", startup_timeout=30
        )
        return self.hub

    def restore_epoch(self, memory: Path) -> tuple[int, dict]:
        done = subprocess.run(
            [str(BINARY), "memory-restore-epoch", "--memory-db", str(memory)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return done.returncode, json.loads(done.stdout)

    def test_a_restored_copy_gets_a_new_epoch_and_a_client_pull_rebuilds(self):
        hub = self.launch()
        memory = self.work / "hub" / "fleet-hub-memory.db"
        node = hc.request(
            hub.url, "POST", "/nodes", body={"action": "add", "node_id": "restore-it"}, token=hub.admin_token
        )
        self.assertEqual(node.status, 200, node)
        os.environ.update(
            BRIGADE_HOME=str(self.work / "home"),
            BRIGADE_FLEET_HUB_URL=hub.url,
            BRIGADE_FLEET_NODE_TOKEN=node.json["token"],
        )
        os.environ.pop("BRIGADE_FLEET_TOKEN", None)
        client = hubc.MemoryHubClient(hub.url, node.json["token"])
        target = self.work / "client"
        (target / "memory" / "cards").mkdir(parents=True)
        (target / ".brigade").mkdir()
        (target / ".brigade" / "memory-care.toml").write_text(
            '[hub]\nmode = "read"\nscope = "fleet"\nread_scopes = ["fleet"]\n', encoding="utf-8"
        )
        settings = hubc.load_settings(target)

        def put(card: str, text: str, base: int | None = None, epoch: str | None = None) -> dict:
            return client.call(
                "put",
                scope="fleet",
                card_id=card,
                base_revision=base,
                base_epoch=epoch,
                text=text,
                client_event_id=hubc.event_id(),
            )

        r1 = put("apple", "apple before the copy")["event"]["revision"]
        old_epoch = sync.pull(target, hubc.client_for(settings), settings)["log_epoch"]
        # The hourly backup's path: SQLite's online backup API on the live file.
        copy = self.work / "copy.db"
        with closing(sqlite3.connect(memory)) as live, closing(sqlite3.connect(copy)) as dest:
            live.backup(dest)
        r2 = put("apple", "apple after the copy", r1, old_epoch)["event"]["revision"]
        put("banana", "banana after the copy")
        sync.pull(target, hubc.client_for(settings), settings)
        self.assertIn(store.Replica.key("fleet", "banana"), store.replica(target).heads)

        # Refused while the hub holds the file, which keeps its epoch.
        code, refused = self.restore_epoch(memory)
        self.assertEqual(code, 1, refused)
        self.assertFalse(refused["ok"])
        error = refused["error"]
        self.assertTrue("held by another process" in error or "-wal sits beside" in error, refused)
        self.assertEqual(client.call("status")["log_epoch"], old_epoch)

        # Fence: stop the hub, put the copy back without the old sidecars, rotate.
        port = int(hub.url.rsplit(":", 1)[1])
        hc.stop_hub(hub)
        self.hub = None
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{memory}{suffix}").unlink(missing_ok=True)
        shutil.copyfile(copy, memory)
        code, rotated = self.restore_epoch(memory)
        self.assertEqual(code, 0, rotated)
        self.assertEqual(rotated["previous_log_epoch"], old_epoch)
        self.assertNotEqual(rotated["log_epoch"], old_epoch)

        # Same address, so the client sees the same hub under a new epoch.
        hc.free_port = lambda: port
        hub = self.launch()
        self.assertEqual(client.call("status")["log_epoch"], rotated["log_epoch"])
        report = sync.pull(target, hubc.client_for(settings), settings)
        self.assertTrue(report["rebuilt"], report)
        rebuilt = "the hub's log_epoch changed; the old replica was discarded and is being rebuilt"
        self.assertIn(rebuilt, report["warnings"])
        replica = store.replica(target)
        self.assertEqual(replica.meta["log_epoch"], rotated["log_epoch"])
        self.assertIn(store.Replica.key("fleet", "apple"), replica.heads)
        self.assertNotIn(store.Replica.key("fleet", "banana"), replica.heads)
        # A write based on a revision from before the restore is refused.
        with self.assertRaises(hubc.HubRefusal) as caught:
            put("apple", "edit of the lost revision", r2, old_epoch)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "memory_epoch_changed"))


if __name__ == "__main__":
    unittest.main()
