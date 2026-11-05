"""Pull workers use local fakes and never open SQLite."""

import base64
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brigade import memory_embed as embed


@pytest.fixture
def endpoint():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer fake-token"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            result = json.dumps({"model_digest": "digest", "embeddings": [[3.0, 4.0]] * len(body["input"])}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/embed"
    config = {
        "name": "test-model",
        "model_digest": "digest",
        "dimensions": 2,
        "query_prefix_version": 1,
        "endpoint": url,
        "allowlist": [url],
        "shape": "api/embed",
    }
    yield config, seen
    server.shutdown()
    server.server_close()
    thread.join()


class Client:
    def __init__(self, state="clean"):
        self.calls = []
        self.state = state

    def call(self, action, **fields):
        self.calls.append((action, fields))
        if action == "embed_lease":
            return {
                "lease_id": "fake-lease",
                "model_digest": "digest",
                "dimensions": 2,
                "jobs": [{"text": "ordinary body", "scan_state": self.state, "content_hash": "a" * 64}],
            }
        return {"stored": 1}


def test_worker_lease_http_submit_normalizes_vectors(endpoint):
    config, seen = endpoint
    client = Client()
    assert embed.step(client, config, "fake-token") == 1
    assert len(seen) == 1
    action, submitted = client.calls[-1]
    assert action == "embed_submit"
    vector = struct.unpack("<2f", base64.b64decode(submitted["vectors"][0]["vector"]))
    assert vector == pytest.approx((0.6, 0.8))
    assert "text" not in json.dumps(submitted)


def test_worker_pending_and_blocked_never_send(endpoint):
    config, seen = endpoint
    with pytest.raises(ValueError, match="pending"):
        embed.step(Client("pending"), config, "fake-token")
    with pytest.raises(ValueError, match="payload refused"):
        embed.local_embed(config, [" ".join(["-----BEGIN", "PRIVATE KEY-----"])], "fake-token")
    assert seen == []


def test_worker_rejects_remote_endpoint_and_digest_swap(endpoint):
    config, seen = endpoint
    changed = {**config, "endpoint": "https://192.0.2.1/api/embed", "allowlist": ["https://192.0.2.1/api/embed"]}
    with pytest.raises(ValueError, match="loopback"):
        embed.local_embed(changed, ["body"], "fake-token")
    with pytest.raises(ValueError, match="digest-mismatch"):
        embed.local_embed({**config, "model_digest": "changed"}, ["body"], "fake-token")
    assert len(seen) == 1


def test_worker_token_permissions_and_cli_registration(tmp_path):
    from brigade.cli import main

    token = tmp_path / "token"
    token.write_text("fake-token")
    token.chmod(0o600)
    assert embed.secret(token) == "fake-token"
    token.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        embed.secret(token)
    with pytest.raises(SystemExit) as exc:
        main(["memory", "embed-worker", "--help"])
    assert exc.value.code == 0
