"""Memory route availability (D21) and persistence through the managed front."""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc  # noqa: E402 - repository-local drive helper
import hub_contract_memory as hm  # noqa: E402

BINARY = ROOT / "engines/hub/target/debug/brigade-hub"
BASE = f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}} --route-mode registration=native"
UNAVAILABLE = {"error": "hub busy", "reason": "memory-unavailable"}


def launch(state: Path, extra: str = "") -> hc.Hub:
    return hc.launch_hub(
        f"{BASE} {extra}".strip(),
        brigade=[sys.executable, "-m", "brigade"],
        state_dir=state,
        startup_timeout=30,
    )


def status(hub: hc.Hub, token: "str | None" = None) -> hc.Response:
    return hc.request(hub.url, "POST", "/memory", body={"action": "status"}, token=token or hub.admin_token)


class Memory(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.work = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def run_hub(self, name: str, extra: str = "", prepare=None) -> hc.Hub:
        state = self.work / name
        state.mkdir(exist_ok=True)
        if prepare:
            prepare(state)
        hub = launch(state, extra)
        self.addCleanup(hc.stop_hub, hub)
        return hub

    def test_not_configured_is_the_upstream_404(self):
        hub = self.run_hub("absent")
        response = status(hub)
        self.assertEqual((response.status, response.json), (404, {"error": "not found"}))

    def test_forced_proxy_and_unopenable_store_answer_503(self):
        forced = self.run_hub("forced", "--route-mode memory=proxy")
        # A directory where the memory file belongs: the store cannot open.
        broken = self.run_hub(
            "broken", "--route-mode memory=native", lambda s: (s / "fleet-hub-memory.db").mkdir()
        )
        # A regular file that is not SQLite: the store and maintenance both fail
        # to open it, and only /memory is unavailable.
        corrupt = self.run_hub(
            "corrupt",
            "--route-mode memory=native",
            lambda s: (s / "fleet-hub-memory.db").write_bytes(b"not a database, " * 256),
        )
        for hub in (forced, broken, corrupt):
            for token in (hub.admin_token, None):
                response = hc.request(hub.url, "POST", "/memory", body={"action": "status"}, token=token)
                self.assertEqual((response.status, response.json), (503, UNAVAILABLE), hub.argv)
            # The rest of the hub keeps serving.
            self.assertEqual(hc.request(hub.url, "GET", "/health").status, 200)
            nodes = hc.request(hub.url, "GET", "/nodes", token=hub.admin_token)
            self.assertEqual(nodes.status, 200)

    def test_native_memory_survives_restart_and_reports_metrics(self):
        state_name = "native"
        hub = self.run_hub(state_name, "--route-mode memory=native")
        first = status(hub)
        self.assertEqual(first.status, 200, first)
        put = hc.request(
            hub.url,
            "POST",
            "/memory",
            token=hub.admin_token,
            body={
                "action": "put",
                "scope": "fleet",
                "card_id": "restart",
                "base_revision": None,
                "text": "kept across a restart",
                "client_event_id": hm.event_id(),
            },
        )
        self.assertEqual(put.status, 201, put)
        metrics = hc.request(hub.url, "GET", "/metrics", token=hub.admin_token)
        text = metrics.body.decode("utf-8")
        self.assertIn("brigade_hub_memory_batches_total 1", text)
        self.assertIn("brigade_hub_memory_residence_bucket_seconds ", text)
        hc.stop_hub(hub)
        time.sleep(0.2)
        again = launch(self.work / state_name, "--route-mode memory=native")
        self.addCleanup(hc.stop_hub, again)
        card = hc.request(
            again.url,
            "POST",
            "/memory",
            token=again.admin_token,
            body={"action": "card", "card_id": "restart"},
        )
        self.assertEqual(card.status, 200, card)
        self.assertEqual(card.json["card"]["revision"], put.json["event"]["revision"])
        self.assertEqual(card.json["card"]["text"], "kept across a restart\n")
        self.assertEqual(status(again).json["log_epoch"], first.json["log_epoch"])

    def test_body_is_never_read_without_a_credential(self):
        hub = self.run_hub("bounds", "--route-mode memory=native")
        junk = hc.request(hub.url, "POST", "/memory", body={"action": "status"}, token=hm.JUNK)
        self.assertEqual((junk.status, junk.json), (401, {"error": "unauthorized"}))
        # A declared length over the memory bound is refused from the headers.
        import http.client
        import json
        from urllib.parse import urlsplit

        origin = urlsplit(hub.url)
        connection = http.client.HTTPConnection(origin.hostname, origin.port, timeout=30)
        try:
            connection.putrequest("POST", "/memory")
            connection.putheader("Authorization", "Bearer " + hub.admin_token)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(3 * 1024 * 1024))
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read())
        finally:
            connection.close()
        self.assertEqual(response.status, 413)
        self.assertEqual(body["code"], "memory_card_too_large")


if __name__ == "__main__":
    unittest.main()
