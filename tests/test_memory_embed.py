"""Pull workers use local fakes and never open SQLite."""

import base64
import json
import os
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    assert embed.step(client, config, "fake-token") == (1, 1, False)
    assert len(seen) == 1
    action, submitted = client.calls[-1]
    assert action == "embed_submit"
    vector = struct.unpack("<2f", base64.b64decode(submitted["vectors"][0]["vector"]))
    assert vector == pytest.approx((0.6, 0.8))
    assert "text" not in json.dumps(submitted)


def test_worker_pending_and_blocked_never_send(endpoint):
    config, seen = endpoint
    # scan-pending is transient, so that step counts as a fault that backs off. A
    # flagged or blocked receipt is a refusal the hub can park.
    for state, reason, fault in (("pending", "scan-pending", True), ("flagged", "content-blocked", False)):
        client = Client(state)
        assert embed.step(client, config, "fake-token") == (1, 0, fault)
        action, fields = client.calls[-1]
        assert action == "embed_failed"
        assert fields["failures"] == [{"content_hash": "0" * 64, "reason": reason}]
    with pytest.raises(ValueError, match="payload refused"):
        embed.local_embed(config, [" ".join(["-----BEGIN", "PRIVATE KEY-----"])], "fake-token")
    assert seen == []


def test_worker_sends_private_text_only_to_a_loopback_endpoint(endpoint):
    config, seen = endpoint
    # The fixture endpoint is numeric loopback, so a private receipt is embedded.
    client = Client("private", texts=("the gateway is at the home lab",))
    assert embed.step(client, config, "fake-token") == (1, 1, False)
    assert client.calls[-1][0] == "embed_submit"
    assert len(seen) == 1
    # Any other endpoint gets nothing. The job goes back as content-private,
    # which the hub parks, so the worker does not count it as an endpoint fault.
    remote = {**config, "endpoint": "http://192.0.2.1/api/embed", "allowlist": ["http://192.0.2.1/api/embed"]}
    client = Client("private")
    assert embed.step(client, remote, "fake-token") == (1, 0, False)
    action, fields = client.calls[-1]
    assert action == "embed_failed"
    assert fields["failures"] == [{"content_hash": "0" * 64, "reason": "content-private"}]
    assert len(seen) == 1


def test_content_private_is_not_transient_in_either_list():
    source = (Path(__file__).resolve().parents[1] / "engines/hub/src/memory/embedding.rs").read_text(encoding="utf-8")
    block = source.split("const TRANSIENT: &[&str] = &[", 1)[1].split("];", 1)[0]
    assert "content-private" not in re.findall(r'"([a-z-]+)"', block)
    assert "content-private" not in embed.TRANSIENT_REASONS


def test_transient_reasons_match_the_hub():
    source = (Path(__file__).resolve().parents[1] / "engines/hub/src/memory/embedding.rs").read_text(encoding="utf-8")
    block = source.split("const TRANSIENT: &[&str] = &[", 1)[1].split("];", 1)[0]
    assert set(re.findall(r'"([a-z-]+)"', block)) == set(embed.TRANSIENT_REASONS)


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
    assert embed.step(client, config, "fake-token") == (3, 2, False)
    # One batch, then each document alone.
    assert [len(body["input"]) for body in seen] == [3, 1, 1, 1]
    submitted = dict(client.calls)["embed_submit"]
    assert [v["content_hash"] for v in submitted["vectors"]] == ["0" * 64, "2".zfill(64)]
    failed = dict(client.calls)["embed_failed"]
    assert failed["failures"] == [{"content_hash": "1".zfill(64), "reason": "embedder-rejected", "alone": True}]


def test_worker_wrong_row_count_is_a_document_refusal_not_transient(monkeypatch, endpoint):
    config, _ = endpoint
    calls = []

    def short_reply(cfg, texts, token):
        calls.append(len(texts))
        raise embed.DocumentRefused("invalid vector count", "vector-invalid")

    monkeypatch.setattr(embed, "local_embed", short_reply)
    client = Client(texts=("apple notes", "pear notes"))
    assert embed.step(client, config, "fake-token") == (2, 0, False)
    # The batch, then each document alone, each reported as tried alone.
    assert calls == [2, 1, 1]
    failed = dict(client.calls)["embed_failed"]
    assert failed["failures"] == [
        {"content_hash": f"{n:064x}", "reason": "vector-invalid", "alone": True} for n in range(2)
    ]
    assert "vector-invalid" not in embed.TRANSIENT_REASONS


def test_worker_exchange_refuses_a_reply_with_the_wrong_row_count(endpoint):
    config, _ = endpoint

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def getheader(self, _name):
            return None

        def read1(self, _n):
            body, self.body = self.body, b""
            return body

    class Conn:
        sock = None

        def __init__(self, body):
            self.body = body

        def connect(self):
            self.sock = type("Sock", (), {"settimeout": lambda *_: None})()

        def request(self, *_):
            pass

        def getresponse(self):
            return Response(self.body)

    for shape, body in (
        ("api/embed", {"model_digest": "digest", "embeddings": [[3.0, 4.0]] * 2}),
        ("v1/embeddings", {"model_digest": "digest", "data": [{"index": 0, "embedding": [3.0, 4.0]}]}),
    ):
        conn = Conn(json.dumps(body).encode())
        with pytest.raises(embed.DocumentRefused) as exc:
            embed._exchange(
                conn,
                "/",
                b"{}",
                "fake-token",
                {**config, "shape": shape},
                1 if shape == "api/embed" else 2,
                lambda: 5.0,
            )
        assert exc.value.reason == "vector-invalid"


def test_worker_exchange_survives_huge_json_numbers(endpoint):
    config, _ = endpoint

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def getheader(self, _name):
            return None

        def read1(self, _n):
            body, self.body = self.body, b""
            return body

    class Conn:
        sock = None

        def __init__(self, body):
            self.body = body

        def connect(self):
            self.sock = type("Sock", (), {"settimeout": lambda *_: None})()

        def request(self, *_):
            pass

        def getresponse(self):
            return Response(self.body)

    def exchange(row):
        body = json.dumps({"model_digest": "digest", "embeddings": [row]}).encode()
        return embed._exchange(Conn(body), "/", b"{}", "fake-token", {**config, "shape": "api/embed"}, 1, lambda: 5.0)

    # Squaring 1e200 overflows a float. The norm no longer squares, so it is a valid vector.
    assert exchange([1e200, 1e200])[0] == pytest.approx([2**-0.5, 2**-0.5])
    # An integer past float range raises OverflowError. It is an endpoint fault, not a crash.
    with pytest.raises(embed.EndpointError) as exc:
        exchange([10**400, 1])
    assert exc.value.reason == "embedder-unavailable"


def test_worker_endpoint_fault_releases_the_lease_without_a_document_reason(endpoint):
    config, seen = endpoint
    stopped = {**config, "endpoint": "http://127.0.0.1:1/api/embed", "allowlist": ["http://127.0.0.1:1/api/embed"]}
    client = Client(texts=("apple notes", "pear notes"))
    assert embed.step(client, stopped, "fake-token") == (2, 0, True)
    failed = dict(client.calls)["embed_failed"]
    assert {f["reason"] for f in failed["failures"]} == {"embedder-unavailable"}
    assert "embed_submit" not in dict(client.calls)


def test_worker_loop_continues_and_sleeps_only_when_a_lease_is_empty(monkeypatch):
    from brigade import memory_hub_client as hubc

    class Stop(Exception):
        pass

    results = iter(
        [
            (4, 4, False),
            hubc.HubRefusal(409, {"code": "memory_lease_expired"}),
            (2, 1, False),
            (0, 0, False),
            (3, 3, False),
            Stop(),
        ]
    )

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


def test_worker_backs_off_exponentially_while_the_endpoint_is_down_and_resets(monkeypatch):
    class Stop(Exception):
        pass

    down, good, refused = (3, 0, True), (3, 3, False), (3, 2, False)
    # A refused document is not an endpoint fault, so it never backs off.
    results = iter([down, down, down, refused, down, good, down, Stop()])

    def fake_step(*_):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps = []
    monkeypatch.setattr(embed, "step", fake_step)
    with pytest.raises(Stop):
        embed.serve(object(), {}, "fake-token", sleep=sleeps.append)
    # 1, 2, 4 while down. The refusal resets the delay, so the next outage restarts at 1.
    # The good step resets it again.
    assert sleeps == [1.0, 2.0, 4.0, 1.0, 1.0]


def test_worker_backoff_caps_at_sixty_seconds(monkeypatch):
    class Stop(Exception):
        pass

    results = iter([(1, 0, True)] * 9 + [Stop()])

    def fake_step(*_):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps = []
    monkeypatch.setattr(embed, "step", fake_step)
    with pytest.raises(Stop):
        embed.serve(object(), {}, "fake-token", sleep=sleeps.append)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]


def test_worker_with_a_dead_endpoint_backs_off_and_a_refused_document_does_not(endpoint):
    """End to end through step: the flag comes from the failures, not from a guess."""
    config, _ = endpoint
    dead = {**config, "endpoint": "http://127.0.0.1:1/api/embed", "allowlist": ["http://127.0.0.1:1/api/embed"]}

    class Stop(Exception):
        pass

    class Looping(Client):
        def __init__(self, texts):
            super().__init__(texts=texts)
            self.leases = 0

        def call(self, action, **fields):
            if action == "embed_lease":
                self.leases += 1
                if self.leases > 3:
                    raise Stop()
            return super().call(action, **fields)

    sleeps = []
    with pytest.raises(Stop):
        embed.serve(Looping(("apple notes",)), dead, "fake-token", sleep=sleeps.append)
    assert sleeps == [1.0, 2.0, 4.0]
    sleeps.clear()
    with pytest.raises(Stop):
        embed.serve(Looping(("poison pill",)), config, "fake-token", sleep=sleeps.append)
    assert sleeps == []


def test_identity_requires_the_keys_the_hub_requires():
    config = {"name": "m", "model_digest": "d", "dimensions": 2, "query_prefix_version": 1}
    assert len(embed.identity(config)) == 64
    for key in config:
        with pytest.raises(ValueError, match=f"{key} is required"):
            embed.identity({k: v for k, v in config.items() if k != key})
    with pytest.raises(ValueError, match="query_prefix_version is required"):
        embed.identity({**config, "query_prefix_version": 0})


@pytest.fixture
def scripted_reply():
    """An endpoint whose reply body and status the test sets per request."""
    reply = {"status": 200, "body": b""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(reply["status"])
            self.send_header("Content-Length", str(len(reply["body"])))
            self.end_headers()
            self.wfile.write(reply["body"])

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
    yield config, reply
    server.shutdown()
    server.server_close()
    thread.join()


MALFORMED_REPLIES = [
    pytest.param("api/embed", {"model_digest": "digest"}, id="missing-embeddings"),
    pytest.param("api/embed", {"model_digest": "digest", "embeddings": 7}, id="embeddings-not-a-list"),
    pytest.param("api/embed", {"model_digest": "digest", "embeddings": [7]}, id="row-not-a-list"),
    pytest.param("api/embed", {"model_digest": "digest", "embeddings": [None]}, id="row-null"),
    pytest.param("api/embed", ["not", "an", "object"], id="body-is-a-list"),
    pytest.param("api/embed", "text", id="body-is-a-string"),
    pytest.param("v1/embeddings", {"model_digest": "digest"}, id="missing-data"),
    pytest.param("v1/embeddings", {"model_digest": "digest", "data": [7]}, id="entry-not-a-dict"),
    pytest.param("v1/embeddings", {"model_digest": "digest", "data": [{"embedding": [3.0, 4.0]}]}, id="no-index"),
    pytest.param(
        "v1/embeddings",
        {
            "model_digest": "digest",
            "data": [{"index": 0, "embedding": [3.0, 4.0]}, {"index": "1", "embedding": [3.0, 4.0]}],
        },
        id="mixed-index-types",
    ),
    pytest.param("v1/embeddings", {"model_digest": "digest", "data": [{"index": 0}]}, id="no-embedding"),
]


@pytest.mark.parametrize(("shape", "body"), MALFORMED_REPLIES)
def test_worker_malformed_reply_is_an_endpoint_fault_not_a_crash(scripted_reply, shape, body):
    config, reply = scripted_reply
    reply["body"] = json.dumps(body).encode()
    client = Client()
    # One bad reply is an endpoint fault for that step: the job is released
    # with a transient reason and step returns instead of raising.
    assert embed.step(client, {**config, "shape": shape}, "fake-token") == (1, 0, True)
    action, fields = client.calls[-1]
    assert action == "embed_failed"
    assert [f["reason"] for f in fields["failures"]] == ["embedder-unavailable"]
    assert "embed_submit" not in dict(client.calls)


def test_worker_loop_keeps_running_after_a_malformed_reply(scripted_reply):
    config, reply = scripted_reply
    reply["body"] = json.dumps(["not", "an", "object"]).encode()
    sleeps = []

    class Stop(Exception):
        pass

    class StopAfterSubmit(Client):
        def call(self, action, **fields):
            result = super().call(action, **fields)
            if action == "embed_submit":
                raise Stop
            return result

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            # The endpoint recovers between the second backoff and the next lease.
            reply["body"] = json.dumps({"model_digest": "digest", "embeddings": [[3.0, 4.0]]}).encode()

    client = StopAfterSubmit()
    with pytest.raises(Stop):
        embed.serve(client, config, "fake-token", sleep=sleep)
    # Two malformed replies back off 1 s then 2 s and the loop leases again.
    assert sleeps == [1.0, 2.0]
    assert [a for a, _ in client.calls].count("embed_failed") == 2


@pytest.mark.parametrize("retry_failed", [False, True])
def test_admin_rescan_cli_uses_explicit_token_and_one_bounded_batch(tmp_path, monkeypatch, capsys, retry_failed):
    from brigade.cli import main

    token = tmp_path / "admin-token"
    token.write_text("fake-admin-token")
    token.chmod(0o600)
    settings = embed.hubc.HubSettings(mode="read", hub_url="https://example.com", token="fake-node-token")
    monkeypatch.setattr(embed.hubc, "load_settings", lambda _: settings)
    calls = []

    class AdminClient:
        def __init__(self, url, credential):
            assert url == settings.hub_url
            assert credential == "fake-admin-token"

        def call(self, action, **fields):
            calls.append((action, fields))
            return {"scanned": 2, "complete": False}

    monkeypatch.setattr(embed.hubc, "MemoryHubClient", AdminClient)
    assert (
        main(
            [
                "memory",
                "rescan",
                "--target",
                str(tmp_path),
                "--admin-token-file",
                str(token),
                "--limit",
                "2",
                *(["--retry-failed"] if retry_failed else []),
            ]
        )
        == 0
    )
    assert len(calls) == 1 and calls[0][0] == "rescan"
    assert calls[0][1]["limit"] == 2
    assert calls[0][1].get("retry_failed", False) is retry_failed
    assert embed.hubc.event_id_ms(calls[0][1]["client_event_id"]) is not None
    assert json.loads(capsys.readouterr().out) == {"scanned": 2, "complete": False}


@pytest.mark.parametrize("limit", ["0", "17", "-1"])
def test_admin_rescan_cli_refuses_an_unbounded_batch(limit):
    from brigade.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["memory", "rescan", "--admin-token-file", "unused", "--limit", limit])
    assert exc.value.code == 2


def rescan_reply(scanned=1, complete=False, failed=0, corrupt=0):
    return {
        "scanned": scanned,
        "complete": complete,
        "counts": {
            "clean": scanned - failed - corrupt,
            "private": 0,
            "flagged": 0,
            "blocked": 0,
            "failed": failed,
            "corrupt": corrupt,
        },
    }


class DrainClient:
    def __init__(self, live, replies):
        self.live, self.replies, self.calls = live, iter(replies), []

    def call(self, action, **fields):
        if action == "status":
            return {"cards": self.live}
        assert action == "rescan"
        self.calls.append(fields)
        return next(self.replies)


def test_rescan_until_done_uses_fresh_ids_and_totals_including_failed(capsys):
    client = DrainClient(3, [rescan_reply(2, failed=1), rescan_reply(1, complete=True)])
    sleeps = []
    assert embed.rescan_until_done(client, 2, sleep=sleeps.append) == 0
    assert len({call["client_event_id"] for call in client.calls}) == 2
    assert [call["limit"] for call in client.calls] == [2, 2]
    assert sleeps == [0.05]
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] is True and result["scanned"] == 3 and result["batches"] == 2
    assert result["counts"] == {"clean": 2, "private": 0, "flagged": 0, "blocked": 0, "failed": 1, "corrupt": 0}


def test_rescan_until_done_handles_short_byte_budget_batches(capsys):
    client = DrainClient(32, [*[rescan_reply() for _ in range(31)], rescan_reply(complete=True)])
    assert embed.rescan_until_done(client, 16, sleep=lambda _: None) == 0
    assert len(client.calls) == 32
    assert json.loads(capsys.readouterr().out)["scanned"] == 32


def test_rescan_until_done_refuses_no_progress(capsys):
    client = DrainClient(10, [rescan_reply(0)])
    with pytest.raises(ValueError, match="no progress"):
        embed.rescan_until_done(client, 4, sleep=lambda _: None)
    assert len(client.calls) == 1
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_rescan_until_done_caps_a_server_that_never_completes(capsys):
    import itertools

    client = DrainClient(0, itertools.repeat(rescan_reply()))
    with pytest.raises(ValueError, match="iteration limit"):
        embed.rescan_until_done(client, 1, sleep=lambda _: None)
    assert len(client.calls) == 16
    assert json.loads(capsys.readouterr().out)["batches"] == 16


@pytest.mark.parametrize("status", [200, 201, 204, 403, 409, 429, 503])
@pytest.mark.parametrize("corrupt", [False, True])
def test_rescan_until_done_cli_stops_on_completion_or_first_non_200(status, tmp_path, monkeypatch, capsys, corrupt):
    from brigade.cli import main

    token = tmp_path / "admin-token"
    token.write_text("fake-admin-token")
    token.chmod(0o600)
    settings = embed.hubc.HubSettings(mode="read", hub_url="https://example.com", token="fake-node-token")
    monkeypatch.setattr(embed.hubc, "load_settings", lambda _: settings)
    calls = []

    def transport(url, credential, body, timeout):
        calls.append(body)
        if body["action"] == "status":
            return 200, {"cards": 1}
        return status, rescan_reply(complete=True, corrupt=int(corrupt))

    client = embed.hubc.MemoryHubClient(settings.hub_url, "fake-admin-token", transport=transport)
    monkeypatch.setattr(embed.hubc, "MemoryHubClient", lambda *_: client)
    result = main(["memory", "rescan", "--until-done", "--target", str(tmp_path), "--admin-token-file", str(token)])
    assert result == (0 if status == 200 and not corrupt else 1)
    assert [call["action"] for call in calls] == ["status", "rescan"]
    assert json.loads(capsys.readouterr().out)["complete"] is (status == 200)


def test_rescan_until_done_finishes_before_reporting_corruption(capsys):
    client = DrainClient(2, [rescan_reply(corrupt=1), rescan_reply(complete=True)])
    assert embed.rescan_until_done(client, 1, sleep=lambda _: None) == 1
    assert len(client.calls) == 2
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert summary["complete"] is True and summary["scanned"] == 2
    assert summary["counts"]["corrupt"] == 1 and summary["counts"]["failed"] == 0
    assert "1 corrupt blob(s)" in output.err


def test_rescan_retry_drain_uses_a_cursor_even_when_the_scanner_keeps_failing(capsys):
    first = rescan_reply(failed=1)
    first["next_after"] = "a" * 64
    client = DrainClient(2, [first, rescan_reply(failed=1, complete=True)])
    assert embed.rescan_until_done(client, 1, retry_failed=True, sleep=lambda _: None) == 0
    assert client.calls[0]["retry_failed"] is True and "after" not in client.calls[0]
    assert client.calls[1]["retry_failed"] is True and client.calls[1]["after"] == "a" * 64
    assert json.loads(capsys.readouterr().out)["counts"]["failed"] == 2


@pytest.mark.parametrize("cursor", [None, "bad", "A" * 64])
def test_rescan_retry_drain_refuses_an_invalid_cursor(cursor, capsys):
    reply = rescan_reply(failed=1)
    reply["next_after"] = cursor
    client = DrainClient(2, [reply])
    with pytest.raises(ValueError, match="retry cursor"):
        embed.rescan_until_done(client, 1, retry_failed=True, sleep=lambda _: None)
    assert len(client.calls) == 1
    assert json.loads(capsys.readouterr().out)["complete"] is False
