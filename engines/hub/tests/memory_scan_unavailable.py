"""Search-only build: missing scanner cannot raise trust or push pending text."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory import hc, hm, launch


class Unavailable(unittest.TestCase):
    def test_s18_pending_writes_refuse_every_raise_and_push(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            hub = launch(state, "--route-mode memory=native")
            self.addCleanup(hc.stop_hub, hub)

            def request(body):
                return hc.request(hub.url, "POST", "/memory", token=hub.admin_token, body=body)

            self.assertFalse(request({"action": "status"}).json["scan"]["available"])
            put = request({"action": "put", "scope": "fleet", "card_id": "pending",
                           "text": "pending release checklist", "client_event_id": hm.event_id()})
            self.assertEqual(put.status, 201)
            self.assertEqual(put.json["event"]["trust"], "agent-unreviewed")
            with sqlite3.connect(state / "fleet-hub-memory.db") as db:
                self.assertEqual(db.execute("SELECT scan_state FROM memory_card_revisions").fetchone()[0], "pending")
            raised = request({"action": "import", "scope": "fleet", "card_id": "raised",
                              "text": "release checklist", "client_event_id": hm.event_id()})
            self.assertEqual((raised.status, raised.json["code"]), (503, "memory_scan_unavailable"))
            self.assertIs(raised.json["retryable"], True)
            # Even preexisting trusted data must not push while the engine is missing.
            with sqlite3.connect(state / "fleet-hub-memory.db") as db:
                db.execute("UPDATE memory_cards SET trust='imported'")
            prompt = request({"action": "search", "query": "release", "query_source": "prompt"})
            self.assertEqual(prompt.json["matches"], [{"card_id": "pending", "blocked": "memory_scan_unavailable"}])


if __name__ == "__main__":
    unittest.main()
