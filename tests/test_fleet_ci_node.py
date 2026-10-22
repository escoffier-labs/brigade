"""Offline node repair, relay paging, cursor safety and shared read budget."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from brigade import fleet_hub
from brigade.github_reader import CommandResult, GitHubReader
from brigade.github_read_ledger import BudgetLedger

from tests.test_fleet_ci import HEAD, NOW, OLD, observation
from tests.test_fleet_ci_relay import relay_page


class Provider:
    def __init__(self):
        self.calls = []
        self.fail_status = None
        self.more_pulls = False
        self.check_state = "success"

    def __call__(self, argv, *, env, max_bytes, timeout):
        self.calls.append(list(argv))
        if argv[:2] == ["octopool", "whoami"]:
            return CommandResult(0, b"{}")
        relay = argv[0] == "octopool"
        endpoint = argv[argv.index("--path") + 1] if relay else argv[7]
        path = urlsplit(endpoint).path
        if self.fail_status:
            status, value = self.fail_status, {}
        elif path == "/repos/example/public":
            status, value = 200, {"private": False, "default_branch": "main"}
        elif path == "/repos/example/public/branches/main":
            status, value = 200, {"commit": {"sha": HEAD}}
        elif path.endswith("/protection/required_status_checks"):
            status, value = 200, {"contexts": [], "checks": [{"context": "ci", "app_id": 7}]}
        elif path.endswith("/rules/branches/main"):
            status, value = 200, []
        elif path.endswith("/actions/runs"):
            status, value = (
                200,
                {
                    "total_count": 1,
                    "workflow_runs": [
                        dict(
                            id=11,
                            run_attempt=2,
                            head_sha=HEAD,
                            status="completed",
                            conclusion="success",
                            updated_at=NOW,
                            created_at=NOW,
                        )
                    ],
                },
            )
        elif path.endswith("/pulls"):
            status, value = 200, [{"number": 5, "head": {"sha": OLD}}]
        elif path.endswith("/check-runs"):
            sha = path.split("/")[-2]
            status, value = (
                200,
                {
                    "total_count": 1,
                    "check_runs": [
                        dict(
                            id=22 if sha == HEAD else 23,
                            name="ci",
                            app={"id": 7},
                            head_sha=sha,
                            status="completed",
                            conclusion=self.check_state,
                            completed_at=NOW,
                        )
                    ],
                },
            )
        elif path.endswith("/statuses"):
            status, value = 200, []
        else:
            raise AssertionError(path)
        headers = {"link": '<next>; rel="next"'} if self.more_pulls and path.endswith("/pulls") else {}
        if relay:
            return CommandResult(
                0, json.dumps({"status": status, "headers": headers, "body_encoding": "json", "body": value}).encode()
            )
        data = f"HTTP/2 {status}\n" + "".join(f"{k}: {v}\n" for k, v in headers.items()) + "\n" + json.dumps(value)
        return CommandResult(0 if status == 200 else 1, data.encode())


def reader(tmp_path, provider, **kw):
    return GitHubReader(
        "example/public",
        visibility=kw.pop("visibility", "public"),
        runner=provider,
        ledger=BudgetLedger(tmp_path / "budget.db"),
        clock=lambda: 1792584000,
        **kw,
    )


def test_reconcile_alone_repairs_dropped_delivery_and_populates_prs(tmp_path):
    from brigade import fleet_ci_board as ci, fleet_ci_reconcile as node

    provider = Provider()
    payload = node.reconcile_repo(reader(tmp_path, provider), now=NOW)
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, payload, now=NOW)
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["head"]["sha"] == HEAD
    assert row["latest_run"]["attempt"] == 2
    assert row["required"]["state"] == "passing"
    assert row["pulls"][0]["number"] == 5
    assert row["pulls"][0]["head_sha"] == OLD
    assert row["pulls"][0]["rollup"]["state"] == "passing"
    assert row["last_event_received"] is None
    assert row["last_reconciled"] == NOW
    assert row["complete"]
    assert all("POST" not in call for call in provider.calls)
    conn.close()


@pytest.mark.parametrize("status", [403, 404, 429])
def test_reconcile_failures_explicitly_unknown(tmp_path, status):
    from brigade import fleet_ci_reconcile as node

    provider = Provider()
    provider.fail_status = status
    payload = node.reconcile_repo(reader(tmp_path, provider, visibility="private"), now=NOW)
    row = payload["repositories"][0]
    assert not row["complete"]
    assert row["head_sha"] is None
    assert not row["required_complete"]
    assert row["reason"]
    assert all(call[0] == "gh" for call in provider.calls)


def test_required_cache_daily_and_incomplete_pagination_stays_unknown(tmp_path):
    from brigade import fleet_ci_board as ci, fleet_ci_reconcile as node

    provider = Provider()
    source = reader(tmp_path, provider)
    first = node.reconcile_repo(source, now=NOW)
    provider.calls.clear()
    provider.more_pulls = True
    payload = node.reconcile_repo(source, now="2026-10-21T13:01:00Z", previous=first["repositories"][0])
    assert not any("protection" in str(call) or "/rules/" in str(call) for call in provider.calls)
    assert not payload["repositories"][0]["pulls_complete"]
    assert len([c for c in provider.calls if "/pulls" in str(c)]) == 2
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, payload, now=NOW)
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert not row["complete"]
    conn.close()


def test_budget_exhaustion_prevents_requests_including_forced_refresh(tmp_path):
    from brigade import fleet_ci_reconcile as node

    provider = Provider()
    payload = node.reconcile_repo(reader(tmp_path, provider, hourly_caps={"octopool": 0}), now=NOW, refresh_id=NOW)
    assert not payload["repositories"][0]["complete"]
    assert provider.calls == []


def test_relay_cursor_commits_only_after_successful_hub_post(tmp_path, monkeypatch):
    from brigade import fleet_ci_node as node

    calls = []
    reject = [True]

    def transport(url, *, token, body=None):
        calls.append((url, token, body))
        if "/v1/events?" in url:
            assert parse_qs(urlsplit(url).query) == {"after": ["0"], "limit": ["100"]}
            raw = relay_page()["events"][0]
            raw["cursor"] = 9
            return {"events": [raw], "next_cursor": 9}
        if reject[0]:
            raise ValueError("Hub unavailable")
        return {"events": 1}

    monkeypatch.setattr(node, "fetch_json", transport)
    state = node.NodeState(tmp_path / "ci.db")
    with pytest.raises(ValueError, match="Hub unavailable"):
        node.pull(
            state,
            relay_url="https://relay.example",
            relay_token="example-relay",
            hub_url="https://hub.example",
            node_token="example-node",
        )
    assert state.cursor("https://relay.example") == "0"
    reject[0] = False
    assert (
        node.pull(
            state,
            relay_url="https://relay.example",
            relay_token="example-relay",
            hub_url="https://hub.example",
            node_token="example-node",
        )["events"]
        == 1
    )
    assert state.cursor("https://relay.example") == "9"
    assert calls[-1][1] == "example-node"
    assert calls[-1][2]["events"][0]["run_id"] == 11


def test_node_hourly_due_and_operator_private_refresh(tmp_path, monkeypatch):
    from brigade import fleet_ci_board as ci, fleet_ci_node as node

    state = node.NodeState(tmp_path / "ci.db")
    requests = []

    def transport(url, *, token, body=None):
        requests.append((url, body))
        if url.endswith("/ci/work"):
            return {
                "repositories": [{"repo": "example/public", "visibility": "public"}],
                "refresh_requests": {ci.refresh_key("example/private"): NOW} if len(requests) > 4 else {},
            }
        return {"repositories": 1}

    reads = []

    def reconcile(source, **kw):
        reads.append(source)
        return {
            "source": "github-reconcile",
            "events": [],
            "repositories": [observation(source.repository, source.visibility, refresh_id=kw.get("refresh_id"))],
        }

    monkeypatch.setattr(node, "fetch_json", transport)
    monkeypatch.setattr(node, "reconcile_repo", reconcile)
    monkeypatch.setattr(node, "inventory", lambda target: [{"repo": "example/private", "visibility": "unknown"}])
    kwargs = dict(
        target=tmp_path, hub_url="https://hub.example", node_token="example-node", admin_token="example-admin", now=NOW
    )
    node.reconcile(state, **kwargs)
    assert len(reads) == 2
    node.reconcile(state, **kwargs)
    assert len(reads) == 2
    node.reconcile(state, **kwargs)
    assert len(reads) == 3
    assert reads[-1].repository == "example/private"
    assert reads[-1].visibility == "unknown"
    assert all(body is None and "example/private" not in url for url, body in requests if url.endswith("/ci/work"))


def test_cli_registers_pull_and_reconcile_with_node_identity(tmp_path, monkeypatch, capsys):
    from brigade import cli, fleet_ci_node as node

    monkeypatch.setenv("BRIGADE_EXTRAS", "0")
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example")
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "example-node")
    monkeypatch.setenv("BRIGADE_CI_RELAY_URL", "https://relay.example")
    monkeypatch.setenv("BRIGADE_CI_RELAY_TOKEN", "example-relay")
    calls = []

    def pull(state, **kw):
        calls.append(kw)
        return {"events": 0, "cursor": "0", "incomplete": 1}

    monkeypatch.setattr(node, "pull", pull)
    assert cli.main(["fleet", "ci", "pull", "--state", str(tmp_path / "ci.db"), "--json"]) == 0
    assert calls[0]["node_token"] == "example-node"
    assert json.loads(capsys.readouterr().out)["cursor"] == "0"
    monkeypatch.setattr(node, "reconcile", lambda state, **kw: {"reconciled": 0, "incomplete": 0})
    assert (
        cli.main(["fleet", "ci", "reconcile", "--target", str(tmp_path), "--state", str(tmp_path / "ci.db"), "--json"])
        == 0
    )


def test_node_inventory_uses_existing_repos_toml_identity(tmp_path, monkeypatch):
    from brigade import fleet_ci_node as node
    from brigade.fleet_session_presence import RepositoryIdentity

    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/repos.toml").write_text(
        '[[repo]]\nid="demo"\npath="demo"\n\n[[repo]]\nid="disabled"\npath="disabled"\nenabled=false\n'
    )
    monkeypatch.setattr(
        "brigade.fleet_session_presence.repository_identity",
        lambda path: RepositoryIdentity("github.com/example/private", "git-origin"),
    )
    assert node.inventory(tmp_path) == [{"repo": "example/private", "visibility": "unknown"}]


def test_relay_strips_payload_bodies_before_posting_to_hub(tmp_path, monkeypatch):
    from brigade import fleet_ci_node as node

    posts = []
    raw = relay_page()["events"][0]
    raw["output"] = {"text": "example arbitrary payload body"}
    raw["actor"] = {"login": "example-actor"}

    def transport(url, *, token, body=None):
        if "/v1/events?" in url:
            return {"events": [raw], "next_cursor": 1}
        posts.append(body)
        return {"events": 1}

    monkeypatch.setattr(node, "fetch_json", transport)
    node.pull(
        node.NodeState(tmp_path / "ci.db"),
        relay_url="https://relay.example",
        relay_token="example-relay",
        hub_url="https://hub.example",
        node_token="example-node",
    )
    assert "output" not in posts[0]["events"][0]
    assert "actor" not in posts[0]["events"][0]


@pytest.mark.parametrize("visibility", ["public", "private"])
@pytest.mark.parametrize("branch,escaped", [("release/1.x", "release%2F1.x"), ("release%beta", "release%25beta")])
def test_default_branch_is_encoded_in_paths_but_raw_in_workflow_query(tmp_path, visibility, branch, escaped):
    from brigade.fleet_ci_reconcile import reconcile_repo

    prefix = "/repos/example/public"
    expected = {
        prefix + "/branches/" + escaped: {"commit": {"sha": HEAD}, "protected": True},
        prefix + "/rules/branches/" + escaped: [],
        prefix + "/branches/" + escaped + "/protection/required_status_checks": {
            "contexts": [],
            "checks": [{"context": "ci", "app_id": 7}],
        },
    }

    class BranchProvider(Provider):
        def __call__(self, argv, **kw):
            if argv[:2] == ["octopool", "whoami"]:
                return super().__call__(argv, **kw)
            pooled = argv[0] == "octopool"
            endpoint = argv[argv.index("--path") + 1] if pooled else argv[7]
            path = urlsplit(endpoint).path
            if path == prefix:
                status, value = 200, {"private": visibility == "private", "default_branch": branch}
            elif path.startswith((prefix + "/branches/", prefix + "/rules/branches/")):
                status, value = (200, expected[path]) if path in expected else (404, {})
            else:
                return super().__call__(argv, **kw)
            self.calls.append(list(argv))
            if pooled:
                return CommandResult(
                    0, json.dumps({"status": status, "headers": {}, "body_encoding": "json", "body": value}).encode()
                )
            return CommandResult(
                0 if status == 200 else 1, f"HTTP/2 {status}\n\n".encode() + json.dumps(value).encode()
            )

    provider = BranchProvider()
    snapshot = reconcile_repo(reader(tmp_path, provider, visibility=visibility), now=NOW)["repositories"][0]
    assert snapshot["complete"]
    assert snapshot["head_sha"] == HEAD
    assert snapshot["default_branch"] == branch
    assert snapshot["required_checks"] == [{"context": "ci", "app_id": 7}]
    requests = [c for c in provider.calls if c[:2] != ["octopool", "whoami"]]
    paths = [urlsplit(c[c.index("--path") + 1] if c[0] == "octopool" else c[7]).path for c in requests]
    assert set(expected) <= set(paths)
    run = requests[paths.index(prefix + "/actions/runs")]
    if visibility == "public":
        assert "branch=" + branch in run
        assert all(c[0] == "octopool" for c in requests)
    else:
        assert parse_qs(urlsplit(run[7]).query)["branch"] == [branch]
        assert all(c[0] == "gh" for c in provider.calls)


def test_ci_bearers_reach_http_hub_without_environment_proxy(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from brigade import fleet_ci_node as node

    hub_requests, proxy_requests = [], []

    def handler(records, location):
        class Handler(BaseHTTPRequestHandler):
            def respond(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                records.append((self.command, self.path, self.headers.get("Authorization")))
                body = json.dumps({"location": location}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = respond

            def log_message(self, *args):
                pass

        return Handler

    hub = ThreadingHTTPServer(("localhost", 0), handler(hub_requests, "hub"))
    proxy = ThreadingHTTPServer(("localhost", 0), handler(proxy_requests, "proxy"))
    threads = [
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        for server in (hub, proxy)
    ]
    for thread in threads:
        thread.start()
    try:
        proxy_url = f"http://localhost:{proxy.server_port}"
        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(key, proxy_url)
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(key, "")
        origin = f"http://localhost:{hub.server_port}"
        node_result = node.fetch_json(origin + "/ci", token="example-node", body={"events": []})
        admin_result = node.fetch_json(origin + "/ci/work", token="example-admin")
        assert proxy_requests == []
        assert hub_requests == [("POST", "/ci", "Bearer example-node"), ("GET", "/ci/work", "Bearer example-admin")]
        assert node_result == admin_result == {"location": "hub"}
    finally:
        for server in (hub, proxy):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
