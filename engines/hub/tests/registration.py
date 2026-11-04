"""Native registration preserves Python wire bytes and immediate revocation."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "registry/skills/verify-brigade"))
import hub_contract as hc  # noqa: E402 - repository-local drive helper

BINARY = ROOT / "engines/hub/target/debug/brigade-hub"


class Registration(unittest.TestCase):
    def test_python_byte_parity(self):
        import json
        import http.client
        from urllib.parse import urlsplit
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clock = work / "clock"
            clock.write_text("1767225600", encoding="utf-8")
            hubs = []
            try:
                for native in (False, True):
                    state = work / ("native" if native else "proxy")
                    state.mkdir()
                    template = f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}} --test-clock-file {clock}"
                    if native:
                        template += " --route-mode registration=native"
                    hubs.append(
                        hc.launch_hub(
                            template,
                            brigade=[sys.executable, "-m", "brigade"],
                            state_dir=state,
                            startup_timeout=30,
                        )
                    )

                def compare(method, path, body=None):
                    responses = []
                    for hub in hubs:
                        origin = urlsplit(hub.url)
                        connection = http.client.HTTPConnection(
                            origin.hostname, origin.port, timeout=30
                        )
                        try:
                            payload = (
                                json.dumps(body).encode() if body is not None else None
                            )
                            connection.request(
                                method,
                                path,
                                body=payload,
                                headers={
                                    "Authorization": "Bearer " + hub.admin_token,
                                    "Content-Type": "application/json",
                                },
                            )
                            received = connection.getresponse()
                            data = received.read()
                            responses.append(
                                SimpleNamespace(
                                    status=received.status,
                                    body=data,
                                    json=json.loads(data),
                                    headers={
                                        k.lower(): v for k, v in received.getheaders()
                                    },
                                )
                            )
                        finally:
                            connection.close()
                    self.assertEqual(
                        responses[0].status, responses[1].status, (path, body)
                    )
                    bodies = []
                    for response in responses:
                        data = response.body
                        parsed = response.json
                        if isinstance(parsed, dict) and "token" in parsed:
                            data = data.replace(
                                json.dumps(parsed["token"]).encode(), b'"minted-token"'
                            )
                        bodies.append(data)
                    self.assertEqual(bodies[0], bodies[1], (path, body))
                    for header in ("content-type", "cache-control", "referrer-policy"):
                        self.assertEqual(
                            responses[0].headers.get(header),
                            responses[1].headers.get(header),
                        )

                compare("GET", "/nodes")
                for body in (
                    {"action": "add", "node_id": "byte-node", "label": "café 🎉"},
                    {"action": "add", "node_id": "byte-node"},
                    {"action": "revoke", "node_id": "missing-node"},
                ):
                    compare("POST", "/nodes", body)
                compare("GET", "/nodes")
                enrollment = {
                    "action": "add",
                    "name": "byte-agent",
                    "label": "café 🎉",
                    "request_id": "request_0123456789",
                    "token": "test_principal_012345678901234567890123456789",
                    "may_send_to": ["node:*"],
                }
                for body in (
                    enrollment,
                    enrollment,
                    {"action": "get", "name": "byte-agent"},
                    {
                        "action": "grants",
                        "name": "byte-agent",
                        "may_send_to": ["byte-node"],
                    },
                    {
                        "action": "revoke",
                        "name": "byte-agent",
                        "expected_enrollment_id": "request_0123456789",
                    },
                    enrollment,
                ):
                    compare("POST", "/message-principals", body)
                compare("POST", "/nodes", {"action": "revoke", "node_id": "byte-node"})
                compare("GET", "/nodes")
            finally:
                for hub in hubs:
                    hc.stop_hub(hub)

    def test_native_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = hc.launch_hub(
                f"{BINARY} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}} --route-mode registration=native",
                brigade=[sys.executable, "-m", "brigade"],
                state_dir=Path(directory),
                startup_timeout=30,
            )
            try:

                def request(method, path, token=None, body=None):
                    return hc.request(hub.url, method, path, token=token, body=body)

                response = request("GET", "/nodes", hub.admin_token)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.body, b'{"nodes": []}')
                added = request(
                    "POST",
                    "/nodes",
                    hub.admin_token,
                    {"action": "add", "node_id": "test-node"},
                )
                self.assertEqual(added.status, 200, added.body)
                token = added.json["token"]
                self.assertEqual(request("GET", "/nodes", token).status, 403)
                revoked = request(
                    "POST",
                    "/nodes",
                    hub.admin_token,
                    {"action": "revoke", "node_id": "test-node"},
                )
                self.assertEqual(revoked.status, 200, revoked.body)
                # A token query parameter is refused before any registration write.
                queried = request(
                    "POST",
                    "/nodes?%74oken=x",
                    hub.admin_token,
                    {"action": "add", "node_id": "query-node"},
                )
                self.assertEqual(queried.status, 401)
                listed = request("GET", "/nodes", hub.admin_token)
                self.assertNotIn(b"query-node", listed.body)
                refused = request("GET", "/nodes", token)
                self.assertEqual(refused.status, 401)
                self.assertEqual(
                    refused.body, b'{"error": "unauthorized: node token revoked"}'
                )
            finally:
                hc.stop_hub(hub)


if __name__ == "__main__":
    unittest.main()
