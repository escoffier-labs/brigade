"""Pull workers use local fakes and never open SQLite."""

import base64
import json
import os
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
            if any("poison" in text for text in body["input"]):
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
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
    def __init__(self, state="clean", texts=("ordinary body",)):
        self.calls = []
        self.state = state
        self.texts = texts

    def call(self, action, **fields):
        self.calls.append((action, fields))
        if action == "embed_lease":
            return {
                "lease_id": "fake-lease",
                "model_digest": "digest",
                "dimensions": 2,
                "jobs": [
                    {"text": text, "scan_state": self.state, "content_hash": f"{n:064x}"}
                    for n, text in enumerate(self.texts)
                ],
            }
        return {"stored": 1}


def test_worker_lease_http_submit_normalizes_vectors(endpoint):
    config, seen = endpoint
    client = Client()
    assert embed.step(client, config, "fake-token") == (1, 1)
    assert len(seen) == 1
    action, submitted = client.calls[-1]
    assert action == "embed_submit"
    vector = struct.unpack("<2f", base64.b64decode(submitted["vectors"][0]["vector"]))
    assert vector == pytest.approx((0.6, 0.8))
    assert "text" not in json.dumps(submitted)


def test_worker_pending_and_blocked_never_send(endpoint):
    config, seen = endpoint
    for state in ("pending", "flagged"):
        client = Client(state)
        assert embed.step(client, config, "fake-token") == (1, 0)
        action, fields = client.calls[-1]
        assert action == "embed_failed"
        assert fields["failures"] == [{"content_hash": "0" * 64, "reason": "scan-pending"}]
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


# Windows does not enforce POSIX mode bits, and the worker has no ACL check
# there to reuse, so only the read itself is checked on every platform.
def test_worker_token_reads_a_regular_file(tmp_path):
    token = tmp_path / "token"
    token.write_text("fake-token")
    token.chmod(0o600)
    assert embed.secret(token) == "fake-token"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_worker_token_refuses_group_readable_mode(tmp_path):
    token = tmp_path / "token"
    token.write_text("fake-token")
    token.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        embed.secret(token)


def test_worker_cli_registration():
    from brigade.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["memory", "embed-worker", "--help"])
    assert exc.value.code == 0


def test_worker_passes_s18_warning_but_prompt_scan_still_withholds_it(endpoint):
    from brigade.memory_content_scan import blocked_rule

    config, seen = endpoint
    warned = "a\u200db"
    assert blocked_rule([warned]) == "zero-width-joiner"
    assert blocked_rule([warned], blocking_only=True) is None
    assert embed.local_embed(config, [warned], "fake-token") == [[0.6, 0.8]]
    assert seen[0]["input"] == [warned]


def test_worker_retries_a_refused_batch_alone_and_reports_the_bad_document(endpoint):
    config, seen = endpoint
    client = Client(texts=("apple notes", "poison pill", "pear notes"))
    assert embed.step(client, config, "fake-token") == (3, 2)
    # One batch, then each document alone.
    assert [len(body["input"]) for body in seen] == [3, 1, 1, 1]
    submitted = dict(client.calls)["embed_submit"]
    assert [v["content_hash"] for v in submitted["vectors"]] == ["0" * 64, "2".zfill(64)]
    failed = dict(client.calls)["embed_failed"]
    assert failed["failures"] == [{"content_hash": "1".zfill(64), "reason": "embedder-rejected"}]


def test_worker_endpoint_fault_releases_the_lease_without_a_document_reason(endpoint):
    config, seen = endpoint
    stopped = {**config, "endpoint": "http://127.0.0.1:1/api/embed", "allowlist": ["http://127.0.0.1:1/api/embed"]}
    client = Client(texts=("apple notes", "pear notes"))
    assert embed.step(client, stopped, "fake-token") == (2, 0)
    failed = dict(client.calls)["embed_failed"]
    assert {f["reason"] for f in failed["failures"]} == {"embedder-unavailable"}
    assert "embed_submit" not in dict(client.calls)


def test_worker_loop_continues_and_sleeps_only_when_a_lease_is_empty(monkeypatch):
    from brigade import memory_hub_client as hubc

    class Stop(Exception):
        pass

    results = iter([(4, 4), hubc.HubRefusal(409, {"code": "memory_lease_expired"}), (2, 1), (0, 0), (3, 3), Stop()])

    def fake_step(*_):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps = []
    monkeypatch.setattr(embed, "step", fake_step)
    with pytest.raises(Stop):
        embed.serve(object(), {}, "fake-token", sleep=sleeps.append)
    assert sleeps == [1]
    monkeypatch.setattr(
        embed, "step", lambda *_: (_ for _ in ()).throw(hubc.HubRefusal(403, {"code": "memory_grant_missing"}))
    )
    with pytest.raises(hubc.HubRefusal):
        embed.serve(object(), {}, "fake-token", sleep=sleeps.append)


def test_identity_requires_the_keys_the_hub_requires():
    config = {"name": "m", "model_digest": "d", "dimensions": 2, "query_prefix_version": 1}
    assert len(embed.identity(config)) == 64
    for key in config:
        with pytest.raises(ValueError, match=f"{key} is required"):
            embed.identity({k: v for k, v in config.items() if k != key})
    with pytest.raises(ValueError, match="query_prefix_version is required"):
        embed.identity({**config, "query_prefix_version": 0})
