"""Memory route availability (D21) and persistence through the managed front."""

import http.client
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc  # noqa: E402 - repository-local drive helper
import hub_contract_memory as hm  # noqa: E402

# CI builds into engines/hub/target. A build elsewhere names its binary here.
BINARY = Path(os.environ.get("BRIGADE_HUB_BINARY", ROOT / "engines/hub/target/debug/brigade-hub"))
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
        # Declared lengths over the memory bound are refused from the headers,
        # the one past the hub's 8 MiB transport limit included.
        for length in (3 * 1024 * 1024, 9_000_000):
            status_code, headers, body = raw(
                hub, "POST", {"Authorization": "Bearer " + hub.admin_token, "Content-Length": str(length)}
            )
            self.assertEqual(status_code, 413, length)
            self.assertEqual(json.loads(body)["code"], "memory_card_too_large", length)

    def test_shared_refusals_carry_memory_codes(self):
        hub = self.run_hub("codes", "--route-mode memory=native")
        bearer = {"Authorization": "Bearer " + hub.admin_token}
        # 405 names the allowed method (RFC 9110 section 15.5.6).
        status_code, headers, body = raw(hub, "GET", bearer)
        self.assertEqual(status_code, 405)
        self.assertEqual(headers.get("allow"), "POST")
        self.assertEqual(json.loads(body)["code"], "memory_method_invalid")
        # A chunked body has no Content-Length: 411, never the shared code-less 400.
        status_code, _, body = raw(hub, "POST", {**bearer, "Transfer-Encoding": "chunked"}, b"0\r\n\r\n")
        self.assertEqual(
            (status_code, json.loads(body)),
            (411, {"error": "Content-Length is required", "code": "memory_request_invalid"}),
        )
        # A body that ends before its declared length.
        origin = urlsplit(hub.url)
        with socket.create_connection((origin.hostname, origin.port), timeout=30) as conn:
            conn.sendall(
                (
                    "POST /memory HTTP/1.1\r\nHost: hub\r\nContent-Type: application/json\r\n"
                    f"Authorization: Bearer {hub.admin_token}\r\nContent-Length: 100\r\n\r\n"
                ).encode("ascii")
                + b'{"action":'
            )
            conn.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := conn.recv(65536):
                reply += chunk
        head, _, payload = reply.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 400"), head)
        self.assertEqual(
            json.loads(payload), {"error": "incomplete request body", "code": "memory_request_invalid"}
        )

    def test_drive_profile_passes_twice_against_one_hub(self):
        # --hub-url runs reuse a hub: the second run must not meet the first run's cards.
        hub = self.run_hub("twice", "--route-mode memory=native")
        for attempt in (1, 2):
            result = hm.run_memory(hub)
            self.assertEqual(result["mismatches"], [], attempt)

    def test_token_query_keeps_the_shared_refusal(self):
        # Python refuses a `token` query on every GET and POST before routing
        # (fleet_hub_http._route); the hub front answers it before memory runs.
        hub = self.run_hub("query", "--route-mode memory=native")
        response = hc.request(hub.url, "POST", "/memory?token=x", body={"action": "status"}, token=hub.admin_token)
        self.assertEqual((response.status, response.body), (401, b"Dashboard request refused.\n"))


def raw(hub: hc.Hub, method: str, headers: dict, body: bytes = b"") -> "tuple[int, dict, bytes]":
    """One request with exactly the given framing headers, through http.client."""
    origin = urlsplit(hub.url)
    connection = http.client.HTTPConnection(origin.hostname, origin.port, timeout=30)
    try:
        connection.putrequest(method, "/memory")
        connection.putheader("Content-Type", "application/json")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders(body or None)
        response = connection.getresponse()
        payload = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, payload
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
