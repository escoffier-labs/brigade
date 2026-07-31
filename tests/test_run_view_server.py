import http.client
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from brigade import runs_cmd


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def _write_run(workspace: Path, run_id: str = "run-001") -> Path:
    run_dir = workspace / ".brigade" / "runs" / run_id
    _write_json(
        run_dir / "run.json",
        {
            "status": "ok",
            "task": "inspect the server lane",
            "started_at": "2026-07-31T12:00:00Z",
            "finished_at": "2026-07-31T12:00:01Z",
            "duration_seconds": 1.0,
        },
    )
    return run_dir


@pytest.fixture
def server_module(monkeypatch):
    app = types.ModuleType("brigade.run_view_app")
    app.render_document = lambda nonce: f"<script nonce={nonce!r}>ok</script>"
    monkeypatch.setitem(sys.modules, "brigade.run_view_app", app)
    from brigade import run_view_server

    return run_view_server


@pytest.fixture
def local_server(server_module, tmp_path):
    server = server_module.create_server(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _request(server, method: str, path: str, *, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    headers_out = dict(response.getheaders())
    connection.close()
    return response.status, headers_out, body


def _request_with_headers(server, method: str, path: str, headers: list[tuple[str, str]]):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.putrequest(method, path, skip_host=True)
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    headers_out = dict(response.getheaders())
    connection.close()
    return response.status, headers_out, body


def _assert_security_headers(headers: dict[str, str]) -> None:
    assert "script-src 'nonce-" in headers["Content-Security-Policy"]
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers


def test_root_serves_embedded_document_with_security_headers(local_server):
    status, headers, body = _request(local_server, "GET", "/")

    assert status == 200
    assert b"<script nonce=" in body
    _assert_security_headers(headers)


def test_list_and_detail_routes_use_cli_owned_payloads(local_server, tmp_path):
    _write_run(tmp_path)

    status, headers, body = _request(local_server, "GET", "/api/runs?limit=1")
    assert status == 200
    assert json.loads(body) == runs_cmd.runs_list_payload(cwd=tmp_path, limit=1, missing_root_as_empty=True)
    _assert_security_headers(headers)

    status, _, body = _request(local_server, "GET", "/api/runs/run-001")
    assert status == 200
    assert json.loads(body) == runs_cmd.run_detail_payload(tmp_path / ".brigade" / "runs" / "run-001")


def test_missing_root_is_an_empty_list_with_diagnostic(local_server):
    status, _, body = _request(local_server, "GET", "/api/runs")

    assert status == 200
    assert json.loads(body) == {
        "schema": "brigade.runs-list.v1",
        "runs": [],
        "skipped_invalid": 0,
        "diagnostic": "runs directory is unavailable",
    }


def test_rejects_unexpected_host_with_safe_error_and_security_headers(local_server):
    status, headers, body = _request(local_server, "GET", "/", headers={"Host": "example.test"})

    assert status == 400
    assert b"example.test" not in body
    _assert_security_headers(headers)


def test_rejects_duplicate_host_values(local_server):
    status, headers, body = _request_with_headers(
        local_server,
        "GET",
        "/",
        [("Host", local_server.authority), ("Host", local_server.authority)],
    )

    assert status == 400
    assert b"127.0.0.1" not in body
    _assert_security_headers(headers)


def test_rejects_absolute_form_request_target(local_server):
    status, headers, body = _request(local_server, "GET", f"http://{local_server.authority}/api/runs")

    assert status == 400
    assert b"127.0.0.1" not in body
    _assert_security_headers(headers)


def test_renderer_type_error_is_a_bounded_503(local_server, monkeypatch):
    app = sys.modules["brigade.run_view_app"]
    monkeypatch.setattr(app, "render_document", lambda _nonce: (_ for _ in ()).throw(TypeError("bad render")))

    status, headers, body = _request(local_server, "GET", "/")

    assert status == 503
    assert b"bad render" not in body
    _assert_security_headers(headers)


def test_rejects_symlinked_brigade_directory_for_list(local_server, tmp_path):
    outside = tmp_path.parent / "outside-brigade"
    outside.mkdir()
    (tmp_path / ".brigade").symlink_to(outside, target_is_directory=True)

    status, _, body = _request(local_server, "GET", "/api/runs")

    assert status == 503
    assert str(outside).encode() not in body


def test_rejects_symlinked_runs_directory_for_list(local_server, tmp_path):
    outside = tmp_path.parent / "outside-runs"
    outside.mkdir()
    brigade = tmp_path / ".brigade"
    brigade.mkdir()
    (brigade / "runs").symlink_to(outside, target_is_directory=True)

    status, _, body = _request(local_server, "GET", "/api/runs")

    assert status == 503
    assert str(outside).encode() not in body


def test_sse_rejects_cross_origin_and_forwards_unknown_versioned_records(local_server, tmp_path, monkeypatch):
    _write_run(tmp_path)
    origin = f"http://127.0.0.1:{local_server.server_port}"
    status, _, _ = _request(
        local_server,
        "GET",
        "/api/runs/run-001/events",
        headers={"Origin": "http://example.test"},
    )
    assert status == 403

    def fake_watch(*args, record_sink, **kwargs):
        assert args == ("run-001",)
        assert kwargs["json_output"] is True
        assert kwargs["recover_artifact_collection"] is False
        record_sink({"schema": "brigade.run-watch.v1", "type": "future", "value": "safe"})
        return 0

    monkeypatch.setattr(runs_cmd, "watch", fake_watch)
    status, headers, body = _request(
        local_server,
        "GET",
        "/api/runs/run-001/events",
        headers={"Origin": origin},
    )
    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    assert b'data: {"schema":"brigade.run-watch.v1","type":"future","value":"safe"}' in body
    _assert_security_headers(headers)


@pytest.mark.parametrize("origins", [("matching", "matching"), ("matching", "http://example.test")])
def test_sse_rejects_duplicate_origin_values_without_control_calls(local_server, monkeypatch, origins):
    origin = f"http://{local_server.authority}"
    monkeypatch.setattr(runs_cmd, "watch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    expected_origins = [origin if value == "matching" else value for value in origins]

    status, headers, _ = _request_with_headers(
        local_server,
        "GET",
        "/api/runs/run-001/events",
        [("Host", local_server.authority)] + [("Origin", value) for value in expected_origins],
    )

    assert status == 403
    _assert_security_headers(headers)


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/../outside",
        "/api/runs/%2e%2e%2foutside",
        "/api/runs/%2Fetc",
        "/api/runs/C:%5coutside",
        "/api/runs/.hidden",
    ],
)
def test_rejects_traversal_and_malformed_run_ids(local_server, path):
    status, _, body = _request(local_server, "GET", path)

    assert status == 404
    assert b"/" not in body


def test_rejects_symlink_run_escape(local_server, tmp_path):
    outside = tmp_path.parent / "outside-run"
    _write_json(outside / "run.json", {"status": "ok"})
    root = tmp_path / ".brigade" / "runs"
    root.mkdir(parents=True)
    (root / "run-link").symlink_to(outside, target_is_directory=True)

    status, _, body = _request(local_server, "GET", "/api/runs/run-link")
    assert status == 404
    assert str(outside).encode() not in body


def test_unreadable_detail_is_not_exposed(local_server, tmp_path, monkeypatch):
    _write_run(tmp_path)
    monkeypatch.setattr(runs_cmd, "run_detail_payload", lambda _path: (_ for _ in ()).throw(PermissionError()))

    status, _, body = _request(local_server, "GET", "/api/runs/run-001")
    assert status == 404
    assert str(tmp_path).encode() not in body


@pytest.mark.parametrize(
    "method",
    ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "MKCOL", "COPY", "MOVE", "LOCK", "UNLOCK"],
)
def test_non_get_methods_are_rejected_without_control_calls(local_server, method, monkeypatch):
    monkeypatch.setattr(runs_cmd, "watch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))

    status, headers, _ = _request(local_server, method, "/api/runs/run-001/events")
    assert status == 405
    assert headers["Allow"] == "GET"
    _assert_security_headers(headers)


def test_non_get_methods_reject_invalid_host(local_server):
    status, headers, body = _request(
        local_server,
        "POST",
        "/api/runs/run-001/events",
        headers={"Host": "example.test"},
    )

    assert status == 400
    assert "Allow" not in headers
    assert b"example.test" not in body
    _assert_security_headers(headers)


def test_limit_must_be_a_small_positive_integer(local_server):
    assert _request(local_server, "GET", "/api/runs?limit=0")[0] == 400
    assert _request(local_server, "GET", "/api/runs?limit=101")[0] == 400
    assert _request(local_server, "GET", "/api/runs?limit=two")[0] == 400


def test_serve_opens_browser_unless_disabled_and_closes_after_ctrl_c(server_module, tmp_path, monkeypatch):
    opened = []
    closed = []

    class InterruptingServer:
        server_port = 43123
        authority = "127.0.0.1:43123"

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            closed.append(True)

    monkeypatch.setattr(server_module, "create_server", lambda _cwd: InterruptingServer())
    monkeypatch.setattr(server_module.webbrowser, "open", opened.append)

    assert server_module.serve(tmp_path, no_open=False) == 0
    assert opened == ["http://127.0.0.1:43123/"]
    assert closed == [True]

    opened.clear()
    assert server_module.serve(tmp_path, no_open=True) == 0
    assert opened == []


def test_serve_reports_bind_failure(server_module, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server_module, "create_server", lambda _cwd: (_ for _ in ()).throw(OSError("address in use")))

    assert server_module.serve(tmp_path, no_open=True) == 2
    assert "error: unable to bind Run View: address in use" in capsys.readouterr().err
