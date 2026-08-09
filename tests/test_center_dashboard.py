import re
import subprocess
import threading
from types import SimpleNamespace

import pytest

from brigade.center_cmd.dashboard import data, render
from brigade.center_cmd.dashboard.views import all_views
from brigade.center_cmd.serve import _make_server
from tests.test_center_serve import _headers_and_body, _raw_request, _status_code, _wait_until_ready


@pytest.fixture
def dashboard_server(tmp_target):
    server = _make_server(
        host="127.0.0.1",
        port=0,
        token=None,
        allowed_hosts=None,
        target=tmp_target,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_until_ready(server)
    yield server
    server.shutdown()
    server.server_close()


def test_esc_escapes_html_special_characters():
    assert render.esc("<>&\"'") == "&lt;&gt;&amp;&quot;&#x27;"


def test_run_json_returns_error_on_nonzero_exit(monkeypatch, tmp_target):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="command failed badly")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = data.run_json(tmp_target, ["status"])
    assert result == {"error": "command failed badly"}


def test_run_json_returns_error_on_malformed_json(monkeypatch, tmp_target):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = data.run_json(tmp_target, ["status"])
    assert result == {"error": "invalid JSON from command"}


def test_run_json_never_uses_shell_true(monkeypatch, tmp_target):
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    data.run_json(tmp_target, ["status"])
    assert captured.get("shell") is not True


@pytest.mark.parametrize(
    "attr",
    ["NAME", "TITLE", "ORDER", "fetch", "render"],
)
def test_registered_views_expose_contract(attr):
    for module in all_views():
        assert hasattr(module, attr)


def test_registered_view_names_are_unique():
    names = [module.NAME for module in all_views()]
    assert len(names) == len(set(names))


def test_get_view_status_returns_200(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/status")
    assert _status_code(response) == "200"


@pytest.mark.parametrize("view_name", [module.NAME for module in all_views()])
def test_get_each_registered_view_returns_200(dashboard_server, view_name):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path=f"/view/{view_name}")
    assert _status_code(response) == "200"
    _, body = _headers_and_body(response)
    assert "failed to render" not in body


def test_get_unknown_view_returns_404(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/nope")
    assert _status_code(response) == "404"


def test_rendered_page_has_no_inline_handlers_and_nonce_matches(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/status")
    headers, body = _headers_and_body(response)

    assert re.search(r"\bon\w+\s*=", body, re.IGNORECASE) is None

    nonce_match = re.search(r"script-src 'nonce-([^'\s]+)'", headers)
    assert nonce_match is not None
    nonce = nonce_match.group(1)
    assert f'<script nonce="{nonce}"' in body
    assert f'<style nonce="{nonce}"' in body


def test_rendered_page_includes_polling_reload(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/status")
    _, body = _headers_and_body(response)
    assert "location.reload" in body


def test_view_exception_degrades_to_panel_not_500(monkeypatch):
    """A raising view must render an inline error, never a traceback page."""
    from brigade.center_cmd.dashboard.views import status_grid

    def _boom(target):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(status_grid, "fetch", _boom)

    server = _make_server(host="127.0.0.1", port=0, token=None, allowed_hosts=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        response = _raw_request(server, f"{host}:{port}", path="/view/status")
        headers, body = _headers_and_body(response)
        assert _status_code(response) == "200"
        assert "default-src 'none'" in headers
        assert "failed to render" in body
        assert "Traceback" not in body
        assert "fixture failure" not in body
    finally:
        server.shutdown()
        server.server_close()
