from __future__ import annotations

import argparse
import contextlib
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brigade import cli, worklore_client
from brigade.cli import fleet as fleet_cli


def test_client_fails_closed_when_hub_is_configured_and_down(monkeypatch):
    monkeypatch.setattr(
        worklore_client,
        "load_fleet_settings",
        lambda: {
            "hub_url": "http://127.0.0.1:9",
            "admin_token": "admin",
            "node_token": "node",
        },  # content-guard: allow loopback-ipv4
    )
    with pytest.raises(worklore_client.FleetClientError, match="hub-unavailable"):
        worklore_client.create_item({"title": "Native", "kind": "fleet"})


def test_fleet_work_create_and_burn_json(monkeypatch, capsys):
    monkeypatch.setattr(
        worklore_client,
        "create_item",
        lambda body, idempotency_key=None: {
            "item": {"work_id": "wl-aaa", "title": body["title"], "status": "captured"}
        },
    )
    paging: dict[str, object] = {}

    def fake_burn_queue(*, limit=None, cursor=None):
        paging.update({"limit": limit, "cursor": cursor})
        return {"items": [], "exclusions": {"acceptance-required": 1, "source-policy": 0}, "next_cursor": None}

    monkeypatch.setattr(worklore_client, "burn_queue", fake_burn_queue)
    assert (
        fleet_cli._dispatch_work_create(argparse.Namespace(title="X", kind="fleet", json=True, idempotency_key=None))
        == 0
    )
    assert '"wl-aaa"' in capsys.readouterr().out
    assert fleet_cli._dispatch_work_burn(argparse.Namespace(json=True, limit=10, cursor="cur-1")) == 0
    assert "acceptance-required" in capsys.readouterr().out
    assert paging == {"limit": 10, "cursor": "cur-1"}


def test_public_client_exports_required_operations():
    for name in (
        "create_item",
        "get_item",
        "list_items",
        "list_events",
        "list_all_events",
        "burn_queue",
        "patch_item",
        "transition",
        "record_attempt",
        "add_link",
        "delete_link",
        "import_batch",
        "link_execution",
    ):
        assert callable(getattr(worklore_client, name))


def test_fleet_work_parser_registers_public_commands():
    parser = cli._build_parser()
    create = parser.parse_args(
        [
            "fleet",
            "work",
            "create",
            "--title",
            "X",
            "--kind",
            "fleet",
            "--idempotency-key",
            "create-1",
            "--json",
        ]
    )
    assert create.func is fleet_cli._dispatch_work_create
    assert create.title == "X"
    assert create.kind == "fleet"
    assert create.idempotency_key == "create-1"
    assert create.json is True
    omitted = parser.parse_args(["fleet", "work", "create", "--title", "X", "--kind", "fleet"])
    assert omitted.idempotency_key is None

    show = parser.parse_args(["fleet", "work", "show", "wl-aaa", "--json"])
    assert show.func is fleet_cli._dispatch_work_show
    assert show.work_id == "wl-aaa"

    listed = parser.parse_args(["fleet", "work", "list", "--source", "github", "--json"])
    assert listed.func is fleet_cli._dispatch_work_list
    assert listed.source == "github"

    burn = parser.parse_args(["fleet", "work", "burn", "--json"])
    assert burn.func is fleet_cli._dispatch_work_burn

    patch = parser.parse_args(["fleet", "work", "patch", "wl-aaa", "--if-match", "1", "--priority", "high", "--json"])
    assert patch.func is fleet_cli._dispatch_work_patch
    assert patch.work_id == "wl-aaa"
    assert patch.if_match == "1"
    assert patch.priority == "high"
    assert patch.burn_eligible is None

    burn_eligible = parser.parse_args(["fleet", "work", "patch", "wl-aaa", "--if-match", "1", "--burn-eligible"])
    assert burn_eligible.burn_eligible is True
    burn_ineligible = parser.parse_args(["fleet", "work", "patch", "wl-aaa", "--if-match", "1", "--no-burn-eligible"])
    assert burn_ineligible.burn_eligible is False
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "fleet",
                "work",
                "patch",
                "wl-aaa",
                "--if-match",
                "1",
                "--burn-eligible",
                "--no-burn-eligible",
            ]
        )

    transitioned = parser.parse_args(
        ["fleet", "work", "transition", "wl-aaa", "--to-status", "ready", "--if-match", "2", "--json"]
    )
    assert transitioned.func is fleet_cli._dispatch_work_transition
    assert transitioned.to_status == "ready"
    assert transitioned.if_match == "2"

    attempt = parser.parse_args(
        ["fleet", "work", "attempt", "wl-aaa", "--action", "reset", "--if-match", "3", "--json"]
    )
    assert attempt.func is fleet_cli._dispatch_work_attempt
    assert attempt.action == "reset"

    link = parser.parse_args(
        [
            "fleet",
            "work",
            "link",
            "wl-aaa",
            "--link-type",
            "url",
            "--external-key",
            "https://example.invalid/a",
            "--json",
        ]
    )
    assert link.func is fleet_cli._dispatch_work_link
    assert link.link_type == "url"
    assert link.external_key == "https://example.invalid/a"

    unlink = parser.parse_args(["fleet", "work", "unlink", "wl-aaa", "lnk-bbb", "--json"])
    assert unlink.func is fleet_cli._dispatch_work_unlink
    assert unlink.work_id == "wl-aaa"
    assert unlink.link_id == "lnk-bbb"


def test_fleet_work_show_list_and_patch_dispatch_json(monkeypatch, capsys):
    patch_bodies: list[dict[str, object]] = []
    monkeypatch.setattr(
        worklore_client,
        "get_item",
        lambda work_id: {"item": {"work_id": work_id, "title": "Shown"}},
    )
    monkeypatch.setattr(
        worklore_client,
        "list_items",
        lambda source=None: {"items": [{"work_id": "wl-src", "source": source}]},
    )
    monkeypatch.setattr(
        worklore_client,
        "patch_item",
        lambda work_id, body, if_match=None: (
            patch_bodies.append(body)
            or {"item": {"work_id": work_id, "priority": body.get("priority"), "version": if_match}}
        ),
    )
    assert fleet_cli._dispatch_work_show(argparse.Namespace(work_id="wl-show", json=True)) == 0
    assert '"wl-show"' in capsys.readouterr().out
    assert fleet_cli._dispatch_work_list(argparse.Namespace(source="native", json=True)) == 0
    assert '"wl-src"' in capsys.readouterr().out
    assert (
        fleet_cli._dispatch_work_patch(
            argparse.Namespace(
                work_id="wl-patch",
                if_match="1",
                title=None,
                description=None,
                scope=None,
                priority="high",
                burn_rank=None,
                burn_eligible=True,
                token_appetite=None,
                execution_mode=None,
                acceptance=None,
                blocker=None,
                review_after=None,
                spend_by=None,
                json=True,
            )
        )
        == 0
    )
    assert '"wl-patch"' in capsys.readouterr().out
    assert patch_bodies == [{"priority": "high", "burn_eligible": True}]


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, amt: int | None = None) -> bytes:
        return self._payload if amt is None else self._payload[:amt]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _capture_urlopen(monkeypatch, captured: dict[str, object], payload: dict[str, object] | None = None) -> None:
    monkeypatch.setattr(
        worklore_client,
        "load_fleet_settings",
        lambda: {
            "hub_url": "http://127.0.0.1:9",
            "admin_token": "admin-secret",
            "node_token": "node-secret",
        },  # content-guard: allow loopback-ipv4
    )

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["timeout"] = timeout
        return _FakeResponse(payload or {"item": {"work_id": "wl-1"}})

    monkeypatch.setattr(worklore_client, "_hub_open", fake_open)


def test_create_item_uses_node_auth_and_idempotency_key(monkeypatch):
    captured: dict[str, object] = {}
    _capture_urlopen(monkeypatch, captured)
    payload = worklore_client.create_item({"title": "Native", "kind": "fleet"}, idempotency_key="create-1")
    assert payload["item"]["work_id"] == "wl-1"
    headers = captured["headers"]
    assert captured["method"] == "POST"
    assert headers["authorization"] == "Bearer node-secret"
    assert headers["idempotency-key"] == "create-1"
    assert "admin-secret" not in headers.values()


def test_create_item_generates_bounded_idempotency_key(monkeypatch):
    captured: dict[str, object] = {}
    _capture_urlopen(monkeypatch, captured)
    worklore_client.create_item({"title": "Native", "kind": "fleet"})
    headers = captured["headers"]
    key = headers["idempotency-key"]
    assert isinstance(key, str) and 1 <= len(key) <= 256
    assert headers["authorization"] == "Bearer node-secret"


def test_patch_and_reset_use_node_auth(monkeypatch):
    captured: dict[str, object] = {}
    _capture_urlopen(monkeypatch, captured, {"item": {"work_id": "wl-1", "version": 2}})
    worklore_client.patch_item("wl-1", {"priority": "high"}, if_match=1)
    headers = captured["headers"]
    assert headers["authorization"] == "Bearer node-secret"
    assert headers["if-match"] == "1"
    worklore_client.record_attempt("wl-1", {"action": "reset"}, if_match=2)
    headers = captured["headers"]
    assert headers["authorization"] == "Bearer node-secret"


def test_fleet_work_create_passes_idempotency_key(monkeypatch, capsys):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        worklore_client,
        "create_item",
        lambda body, idempotency_key=None: (
            seen.update({"body": body, "idempotency_key": idempotency_key})
            or {"item": {"work_id": "wl-key", "title": body["title"]}}
        ),
    )
    assert (
        fleet_cli._dispatch_work_create(
            argparse.Namespace(title="Keyed", kind="fleet", json=True, idempotency_key="cli-1")
        )
        == 0
    )
    assert seen == {"body": {"title": "Keyed", "kind": "fleet"}, "idempotency_key": "cli-1"}
    assert '"wl-key"' in capsys.readouterr().out


def _paging_urlopen(monkeypatch, pages: list[dict[str, object]], urls: list[str]) -> None:
    monkeypatch.setattr(
        worklore_client,
        "load_fleet_settings",
        lambda: {
            "hub_url": "http://127.0.0.1:9",
            "admin_token": "admin-secret",
            "node_token": "node-secret",
        },  # content-guard: allow loopback-ipv4
    )
    remaining = list(pages)

    def fake_open(request, timeout=None):
        urls.append(request.full_url)
        return _FakeResponse(remaining.pop(0) if remaining else {"items": [], "next_cursor": None})

    monkeypatch.setattr(worklore_client, "_hub_open", fake_open)


def test_list_items_all_follows_every_page(monkeypatch):
    urls: list[str] = []
    _paging_urlopen(
        monkeypatch,
        [
            {"items": [{"work_id": "wl-1"}, {"work_id": "wl-2"}], "next_cursor": "cur-1"},
            {"items": [{"work_id": "wl-3"}], "next_cursor": "cur-2"},
            {"items": [{"work_id": "wl-4"}], "next_cursor": None},
        ],
        urls,
    )
    items = worklore_client.list_items_all(source="github", page_size=2)
    assert [item["work_id"] for item in items] == ["wl-1", "wl-2", "wl-3", "wl-4"]
    assert len(urls) == 3
    assert "cursor" not in urls[0] and "limit=2" in urls[0] and "source=github" in urls[0]
    assert "cursor=cur-1" in urls[1]
    assert "cursor=cur-2" in urls[2]


def test_list_items_all_refuses_a_cursor_cycle(monkeypatch):
    urls: list[str] = []
    _paging_urlopen(
        monkeypatch,
        [
            {"items": [{"work_id": "wl-1"}], "next_cursor": "loop"},
            {"items": [{"work_id": "wl-2"}], "next_cursor": "loop"},
        ],
        urls,
    )
    with pytest.raises(worklore_client.WorkloreListingError, match="did not advance"):
        worklore_client.list_items_all(source="github", page_size=1)
    assert len(urls) == 2


def test_list_items_all_is_bounded_by_pages_and_items(monkeypatch):
    urls: list[str] = []
    _paging_urlopen(
        monkeypatch,
        [{"items": [{"work_id": f"wl-{index}"}], "next_cursor": f"cur-{index}"} for index in range(10)],
        urls,
    )
    with pytest.raises(worklore_client.WorkloreListingError, match="exceeded 3 pages"):
        worklore_client.list_items_all(page_size=1, max_pages=3)
    urls.clear()
    _paging_urlopen(
        monkeypatch,
        [{"items": [{"work_id": f"wl-{index}"}], "next_cursor": f"cur-{index}"} for index in range(10)],
        urls,
    )
    with pytest.raises(worklore_client.WorkloreListingError, match="exceeded 2 items"):
        worklore_client.list_items_all(page_size=1, max_items=2)


def test_list_items_all_rejects_a_malformed_page(monkeypatch):
    urls: list[str] = []
    _paging_urlopen(monkeypatch, [{"items": "not-a-list"}], urls)
    with pytest.raises(worklore_client.WorkloreListingError, match="items must be an array"):
        worklore_client.list_items_all()
    urls.clear()
    _paging_urlopen(monkeypatch, [{"items": [], "next_cursor": 7}], urls)
    with pytest.raises(worklore_client.WorkloreListingError, match="next_cursor must be a string"):
        worklore_client.list_items_all()


def test_list_item_links_all_follows_every_page_under_listing_bounds(monkeypatch):
    urls: list[str] = []
    _paging_urlopen(
        monkeypatch,
        [
            {"item": {"work_id": "wl-1"}, "links": [{"link_id": "one"}], "links_next_cursor": "cur-1"},
            {"item": {"work_id": "wl-1"}, "links": [{"link_id": "two"}], "links_next_cursor": None},
        ],
        urls,
    )

    links = worklore_client.list_item_links_all("wl-1", page_size=1)

    assert [link["link_id"] for link in links] == ["one", "two"]
    assert len(urls) == 2
    assert "links_limit=1" in urls[0] and "links_cursor" not in urls[0]
    assert "links_cursor=cur-1" in urls[1]


def test_worklore_listing_error_stays_a_fleet_client_error():
    assert issubclass(worklore_client.WorkloreListingError, worklore_client.FleetClientError)


# --- transport hardening (Daybreak finding 1) --------------------------------

_LOOPBACK = "127.0.0.1"  # content-guard: allow loopback-ipv4


class _RecordingHandler(BaseHTTPRequestHandler):
    """Records every request it sees; replies with the class's scripted response."""

    seen: list[tuple[str, dict[str, str]]] = []
    redirect_to: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).seen.append((self.path, {key.lower(): value for key, value in self.headers.items()}))
        if type(self).redirect_to is not None:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
            return
        body = json.dumps({"items": [], "next_cursor": None}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@contextlib.contextmanager
def _recording_server(*, redirect_to: str | None = None):
    handler = type("_Handler", (_RecordingHandler,), {"seen": [], "redirect_to": redirect_to})
    server = ThreadingHTTPServer((_LOOPBACK, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{_LOOPBACK}:{server.server_address[1]}", handler.seen
    finally:
        server.shutdown()
        server.server_close()


def _settings(monkeypatch, hub_url: str, **overrides: str) -> None:
    settings = {"hub_url": hub_url, "admin_token": "admin-secret", "node_token": "node-secret"}
    settings.update(overrides)
    monkeypatch.setattr(worklore_client, "load_fleet_settings", lambda: dict(settings))


@pytest.mark.parametrize(
    "hub_url",
    [
        "http://hub.example",
        "http://hub.example:8080",
        "http://192.0.2.10:8080",
        "ftp://hub.example",
        "file:///tmp/hub",
        "hub.example",
    ],
)
def test_cleartext_remote_hub_is_refused_before_the_token_is_sent(monkeypatch, hub_url):
    _settings(monkeypatch, hub_url)

    def unreachable(*_args, **_kwargs):
        raise AssertionError("a refused hub URL must never be opened")

    monkeypatch.setattr(worklore_client, "_hub_open", unreachable)
    with pytest.raises(worklore_client.FleetClientError, match="must use https"):
        worklore_client.list_items()
    with pytest.raises(worklore_client.FleetClientError, match="must use https"):
        worklore_client.create_item({"title": "Native", "kind": "fleet"})


@pytest.mark.parametrize("hub_url", ["https://hub.example", "http://127.0.0.1:8787", "http://localhost:8787"])
def test_https_and_loopback_http_hubs_are_allowed(monkeypatch, hub_url):
    _settings(monkeypatch, hub_url)
    seen: list[str] = []

    def fake_open(request, timeout=None):
        seen.append(request.full_url)
        return _FakeResponse({"items": []})

    monkeypatch.setattr(worklore_client, "_hub_open", fake_open)
    assert worklore_client.list_items() == {"items": []}
    assert seen == [f"{hub_url}/work/items"]


def test_worklore_reuses_the_fleet_hub_opener():
    """Worklore shares the fleet opener: an empty ProxyHandler (build_opener drops it
    from ``handlers`` because it resolves nothing) and the origin-pinned redirect
    handler, rather than bare ``urllib.request.urlopen``."""
    from brigade import fleet_client

    assert worklore_client._hub_open is fleet_client._hub_open
    handlers = fleet_client._HUB_OPENER.handlers
    assert not [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert [h for h in handlers if isinstance(h, fleet_client._HubRedirectHandler)]


def test_hub_traffic_ignores_proxy_environment(monkeypatch):
    with _recording_server() as (hub_url, hub_seen), _recording_server() as (proxy_url, proxy_seen):
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        _settings(monkeypatch, hub_url)
        assert worklore_client.list_items() == {"items": [], "next_cursor": None}
    assert [path for path, _ in hub_seen] == ["/work/items"]
    assert proxy_seen == [], "hub traffic reached a configured proxy"


def test_cross_origin_redirect_is_refused_and_never_replays_the_token():
    with _recording_server() as (other_url, other_seen):
        with _recording_server(redirect_to=f"{other_url}/work/items") as (hub_url, hub_seen):
            settings = {"hub_url": hub_url, "admin_token": "admin-secret", "node_token": "node-secret"}
            original = worklore_client.load_fleet_settings
            worklore_client.load_fleet_settings = lambda: dict(settings)  # type: ignore[assignment]
            try:
                with pytest.raises(worklore_client.FleetClientError, match="HTTP 302"):
                    worklore_client.list_items()
            finally:
                worklore_client.load_fleet_settings = original  # type: ignore[assignment]
        assert [path for path, _ in hub_seen] == ["/work/items"]
    assert other_seen == [], "the bearer token followed a cross-origin redirect"


def test_reads_prefer_the_node_token_and_admin_writes_stay_explicit(monkeypatch):
    captured: dict[str, object] = {}
    _capture_urlopen(monkeypatch, captured)
    worklore_client.list_items()
    assert captured["headers"]["authorization"] == "Bearer node-secret"
    worklore_client.get_item("wl-1")
    assert captured["headers"]["authorization"] == "Bearer node-secret"
    worklore_client.burn_queue()
    assert captured["headers"]["authorization"] == "Bearer node-secret"
    assert worklore_client._token(admin=True) == "admin-secret"
    assert worklore_client._token(admin=False) == "node-secret"


def test_reads_fall_back_to_the_admin_token_only_when_no_node_token_exists(monkeypatch):
    _settings(monkeypatch, f"http://{_LOOPBACK}:9", node_token="")
    seen: list[str] = []

    def fake_open(request, timeout=None):
        seen.append(dict(request.header_items())["Authorization"])
        return _FakeResponse({"items": []})

    monkeypatch.setattr(worklore_client, "_hub_open", fake_open)
    worklore_client.list_items()
    assert seen == ["Bearer admin-secret"]
    with pytest.raises(worklore_client.FleetClientError, match="no fleet node token"):
        worklore_client.create_item({"title": "Native", "kind": "fleet"})


def test_oversized_success_response_is_refused(monkeypatch):
    _settings(monkeypatch, f"http://{_LOOPBACK}:9")
    oversized = b"x" * (worklore_client.MAX_RESPONSE_BYTES + 1)

    class _Flood:
        def read(self, amt=None):
            return oversized if amt is None else oversized[:amt]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(worklore_client, "_hub_open", lambda request, timeout=None: _Flood())
    with pytest.raises(worklore_client.FleetClientError, match="response exceeded"):
        worklore_client.list_items()


def test_oversized_http_error_body_still_maps_to_a_stable_refusal(monkeypatch):
    _settings(monkeypatch, f"http://{_LOOPBACK}:9")
    oversized = io.BytesIO(b"{" + b"x" * (worklore_client.MAX_ERROR_RESPONSE_BYTES + 1))

    def fake_open(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "Server Error", {}, oversized)

    monkeypatch.setattr(worklore_client, "_hub_open", fake_open)
    with pytest.raises(worklore_client.FleetClientError) as excinfo:
        worklore_client.list_items()
    message = str(excinfo.value)
    assert message == "fleet hub work failed: HTTP 500"
    assert "x" * 100 not in message


def test_hub_error_text_is_sanitized_and_preserves_its_error_code(monkeypatch):
    _settings(monkeypatch, f"http://{_LOOPBACK}:9")
    refusal = io.BytesIO(json.dumps({"code": "version-conflict", "error": "stale\r\n\x1b[2J version"}).encode())

    def fake_open(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, refusal)

    monkeypatch.setattr(worklore_client, "_hub_open", fake_open)
    with pytest.raises(worklore_client.WorkloreClientError) as excinfo:
        worklore_client.list_items()
    assert excinfo.value.code == "version-conflict"
    assert str(excinfo.value) == "fleet hub work failed: HTTP 409: stale [2J version"


def test_fleet_work_json_error_sanitizes_terminal_controls_and_keeps_code(monkeypatch, capsys):
    monkeypatch.setattr(
        worklore_client,
        "list_items",
        lambda source=None: (_ for _ in ()).throw(
            worklore_client.WorkloreClientError("fleet hub work failed: stale\x1b[2J version", code="version-conflict")
        ),
    )

    assert fleet_cli._dispatch_work_list(argparse.Namespace(source=None, json=True)) == 1
    assert json.loads(capsys.readouterr().err) == {
        "code": "version-conflict",
        "error": "fleet hub work failed: stale [2J version",
    }


def test_a_response_without_a_bounded_read_is_refused(monkeypatch):
    _settings(monkeypatch, f"http://{_LOOPBACK}:9")

    class _Unbounded:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(worklore_client, "_hub_open", lambda request, timeout=None: _Unbounded())
    with pytest.raises(worklore_client.FleetClientError, match="bounded reads"):
        worklore_client.list_items()


def test_burn_queue_passes_paging_through_and_omits_it_by_default(monkeypatch):
    captured: dict[str, object] = {}
    _capture_urlopen(monkeypatch, captured, {"items": [], "exclusions": {}, "next_cursor": None})
    worklore_client.burn_queue()
    assert captured["url"] == "http://127.0.0.1:9/work/queue/burn"  # content-guard: allow loopback-ipv4
    worklore_client.burn_queue(limit=10, cursor="cur-1")
    assert "limit=10" in str(captured["url"]) and "cursor=cur-1" in str(captured["url"])
