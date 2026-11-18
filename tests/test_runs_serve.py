"""Issue #631 BROWSER slice: local read-only Run View via ``runs serve``."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from brigade import cli
from brigade import runs_cmd
from brigade import runs_serve


PLANTED_MARKUP = '<script>document.title="pwned"</script>'
FORBIDDEN_KEYS = frozenset(
    {
        "env",
        "auth_token",
        "owner_token",
        "prompt",
        "transcript",
        "stdout",
        "stderr",
        "stdout_log",
        "stderr_log",
        "cwd",
        "log_path",
        "control_transport",
        "artifacts",
        "handoff",
    }
)
MUTATING = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def _runs_root(workspace: Path) -> Path:
    root = workspace / ".brigade" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _plant_run(
    run_dir: Path,
    *,
    status: str,
    task: str,
    started_at: str,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    failure: dict[str, object] | None = None,
    resume: bool = False,
    briefs: bool = False,
    lineage: dict[str, object] | None = None,
    plan_task: str | None = None,
    worker_detail: str = "worker detail",
    synthesis_detail: str = "merged",
    command: str = "pytest -q planted-verify",
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {
        "task": task,
        "status": status,
        "started_at": started_at,
        "orchestrator": "chef",
        "read_only": False,
        "dry_run": False,
    }
    if finished_at is not None:
        meta["finished_at"] = finished_at
    if duration_seconds is not None:
        meta["duration_seconds"] = duration_seconds
    if failure is not None:
        meta["failure"] = failure
        meta["error"] = failure.get("detail", "failed")
    if lineage is not None:
        meta["lineage"] = lineage
    if briefs:
        meta["code_graph_brief"] = {"attached": True, "bytes": 12, "pending_count": 0}
        meta["drift_impact_brief"] = {"attached": True, "bytes": 8, "pending_count": 1}
        meta["evidence_brief"] = {"attached": True, "bytes": 4, "pending_count": 0}
    _write_json(run_dir / "run.json", meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "max_workers": 1,
            "timeout_seconds": 60,
            "allow_models": ["codex"],
            "agents": {
                "chef": {
                    "cli": "codex",
                    "model": "gpt-5.5",
                    "reasoning": "medium",
                    "role": "orchestrator",
                    "timeout_seconds": 60,
                },
                "coder": {
                    "cli": "codex",
                    "model": "gpt-5.5",
                    "reasoning": "low",
                    "role": "implement",
                    "timeout_seconds": 45,
                },
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {"assignments": [{"stage": 1, "worker": "coder", "task": plan_task or task}]},
    )
    result: dict[str, object] = {
        "worker": "coder",
        "ok": status == "ok",
        "status": "ok" if status == "ok" else ("interrupted" if resume else status),
        "task": task,
        "detail": worker_detail,
        "duration_seconds": 1.5,
        "exit_code": 0 if status == "ok" else 1,
    }
    if resume:
        result["thread_id"] = "thread-resumable"
        result["ok"] = False
        result["status"] = "interrupted"
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [result],
            "ground_truth": {
                "available": True,
                "verify_receipts": [
                    {
                        "run_id": "verify-" + run_dir.name[-6:],
                        "status": "ok" if status == "ok" else "failed",
                        "started_at": started_at,
                        "duration_seconds": 0.4,
                        "commands": [
                            {
                                "command": command,
                                "exit_code": 0 if status == "ok" else 1,
                                "duration_seconds": 0.3,
                            }
                        ],
                    }
                ],
            },
        },
    )
    _write_json(
        run_dir / "synthesis.json",
        {"orchestrator": "chef", "mode": "merge", "result": {"ok": status == "ok", "detail": synthesis_detail}},
    )


def _plant_suite(workspace: Path) -> dict[str, Path]:
    root = _runs_root(workspace)
    planted = {
        "active": root / "20260821-100000-active1",
        "ok": root / "20260821-100100-success",
        "failed": root / "20260821-100200-failed1",
        "resumable": root / "20260821-100300-resume1",
        "invalid": root / "20260821-100400-invalid",
        "markup": root / "20260821-100500-markup",
        "parent": root / "20260821-100600-parent1",
        "child": root / "20260821-100700-child01",
    }
    _plant_run(
        planted["active"],
        status="dispatching",
        task="dispatch planted-active work",
        started_at="2026-08-21T10:00:00Z",
    )
    _plant_run(
        planted["ok"],
        status="ok",
        task="finish planted-success work",
        started_at="2026-08-21T10:01:00Z",
        finished_at="2026-08-21T10:01:04Z",
        duration_seconds=4.0,
        briefs=True,
    )
    _plant_run(
        planted["failed"],
        status="failed",
        task="fail planted-failed work",
        started_at="2026-08-21T10:02:00Z",
        finished_at="2026-08-21T10:02:03Z",
        duration_seconds=3.0,
        failure={"phase": "worker", "kind": "exit-error", "detail": "coder exited 1"},
    )
    _plant_run(
        planted["resumable"],
        status="failed",
        task="resume planted-resumable work",
        started_at="2026-08-21T10:03:00Z",
        finished_at="2026-08-21T10:03:02Z",
        duration_seconds=2.0,
        resume=True,
        failure={"phase": "worker", "kind": "interrupted", "detail": "worker interrupted"},
    )
    planted["invalid"].mkdir()
    planted["invalid"].joinpath("run.json").write_text("{not-json\n")
    _plant_run(
        planted["markup"],
        status="ok",
        task=PLANTED_MARKUP,
        started_at="2026-08-21T10:05:00Z",
        finished_at="2026-08-21T10:05:01Z",
        duration_seconds=1.0,
    )
    _plant_run(
        planted["parent"],
        status="ok",
        task="parent planted lineage",
        started_at="2026-08-21T10:06:00Z",
        finished_at="2026-08-21T10:06:02Z",
        duration_seconds=2.0,
    )
    _plant_run(
        planted["child"],
        status="ok",
        task="child planted lineage",
        started_at="2026-08-21T10:07:00Z",
        finished_at="2026-08-21T10:07:02Z",
        duration_seconds=2.0,
        lineage={
            "kind": "branch",
            "parent_run_id": planted["parent"].name,
            "branch_point_event_id": "evt-parent-1",
        },
    )
    return planted


def _wait_until_ready(server: ThreadingHTTPServer, timeout: float = 5.0) -> None:
    host, port = server.server_address
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def _raw_request(
    server: ThreadingHTTPServer,
    host_header: str,
    *,
    method: str = "GET",
    path: str = "/",
    extra_headers: list[str] | None = None,
    timeout: float = 5.0,
) -> str:
    host, port = server.server_address
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host_header}"]
    if extra_headers:
        lines.extend(extra_headers)
    lines.extend(["", ""])
    request = "\r\n".join(lines).encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(timeout)
        response = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            response += chunk
    return response.decode("utf-8", errors="replace")


def _status_code(response: str) -> str:
    return response.splitlines()[0].split()[1]


def _headers_and_body(response: str) -> tuple[str, str]:
    parts = response.split("\r\n\r\n", 1)
    if len(parts) != 2:
        return parts[0], ""
    return parts[0], parts[1]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return _workspace(tmp_path)


@pytest.fixture
def planted(workspace: Path) -> dict[str, Path]:
    return _plant_suite(workspace)


@pytest.fixture
def server(workspace: Path, planted: dict[str, Path]) -> ThreadingHTTPServer:
    httpd = runs_serve.make_server(cwd=workspace, port=0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    _wait_until_ready(httpd)
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive(), "Run View accept loop survived shutdown (#1916)"


def _host(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address
    return f"{host}:{port}"


def _get_json(server: ThreadingHTTPServer, path: str) -> tuple[int, dict[str, object], str]:
    response = _raw_request(server, _host(server), path=path)
    headers, body = _headers_and_body(response)
    payload = json.loads(body) if body.strip().startswith("{") else {}
    return int(_status_code(response)), payload, headers


def test_cli_dispatches_serve_foreground_no_open(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    def fake_serve(**kwargs: object) -> int:
        called.update(kwargs)
        return 0

    monkeypatch.setattr("brigade.runs_cmd.serve", fake_serve)
    assert cli.main(["runs", "serve", "--cwd", str(workspace), "--no-open"]) == 0
    assert called["open_browser"] is False
    assert called["cwd"] == workspace


def test_cli_help_describes_foreground_read_only_view(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["runs", "serve", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "read-only" in help_text.lower()
    assert "--no-open" in help_text
    assert "--daemon" not in help_text
    assert "--install" not in help_text
    assert "--background" not in help_text


def test_serve_prints_url_and_stops_on_ctrl_c(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ThreadingHTTPServer,
        "serve_forever",
        lambda self: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    opened: list[str] = []
    assert runs_serve.serve(cwd=workspace, open_browser=True, open_url=opened.append) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out
    assert "Ctrl+C" in out
    assert opened and opened[0].startswith("http://127.0.0.1:")


def test_serve_no_open_skips_browser(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        ThreadingHTTPServer,
        "serve_forever",
        lambda self: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert runs_serve.serve(cwd=workspace, open_browser=False, open_url=opened.append) == 0
    assert opened == []


def test_bind_failure_prints_exact_address_error(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_serve_forever(self: ThreadingHTTPServer) -> None:
        pytest.fail("occupied port unexpectedly entered serve_forever")

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", unexpected_serve_forever)
    blocker = runs_serve.make_server(cwd=workspace, port=0)
    port = blocker.server_address[1]
    try:
        rc = runs_serve.serve(cwd=workspace, port=port, open_browser=False)
    finally:
        blocker.server_close()
    assert rc != 0
    err = capsys.readouterr().err
    assert "cannot bind" in err
    assert f"127.0.0.1:{port}" in err


def test_non_loopback_bind_is_refused(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = runs_serve.serve(cwd=workspace, host="192.0.2.1", open_browser=False)
    assert rc != 0
    assert "loopback" in capsys.readouterr().err


def test_serve_writes_no_service_files(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = {path.relative_to(workspace) for path in workspace.rglob("*")}
    monkeypatch.setattr(
        ThreadingHTTPServer,
        "serve_forever",
        lambda self: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert runs_serve.serve(cwd=workspace, open_browser=False) == 0
    after = {path.relative_to(workspace) for path in workspace.rglob("*")}
    assert after == before


@pytest.mark.parametrize(
    "module_path",
    [
        Path("src/brigade/cli/run.py"),
        Path("src/brigade/doctor.py"),
        Path("src/brigade/cli/doctor.py"),
        Path("src/brigade/work_cmd/session/briefing.py"),
        Path("src/brigade/brief_sections.py"),
    ],
)
def test_other_commands_do_not_start_run_view(module_path: Path) -> None:
    if not module_path.is_file():
        pytest.skip(f"{module_path} is not present")
    source = module_path.read_text()
    assert "runs_serve" not in source
    assert "runs serve" not in source


def test_http_layer_has_no_independent_artifact_reader() -> None:
    source = Path("src/brigade/runs_serve.py").read_text()
    assert "run.json" not in source
    assert "roster.json" not in source
    assert "worker-results.json" not in source
    assert "_read_json" not in source
    assert "runs_list_contract" in source
    assert "run_detail_contract" in source
    assert "iter_watch_records" in source


def test_list_route_matches_cli_contract(server: ThreadingHTTPServer, workspace: Path) -> None:
    payload, diagnostic, rc = runs_cmd.runs_list_contract(cwd=workspace, limit=10)
    assert rc == 0
    assert diagnostic is None
    status, body, headers = _get_json(server, "/api/runs?limit=10")
    assert status == 200
    assert body == payload
    assert body["schema"] == "brigade.runs-list.v2"
    assert body["skipped_invalid"] == 1
    ids = {item["run_id"] for item in body["runs"]}
    assert "20260821-100000-active1" in ids
    assert "20260821-100100-success" in ids
    assert "20260821-100200-failed1" in ids
    assert "20260821-100300-resume1" in ids
    assert "20260821-100400-invalid" not in ids
    assert "Cache-Control: no-store" in headers
    assert "Access-Control-Allow-Origin" not in headers


def test_detail_route_matches_cli_contract(server: ThreadingHTTPServer, planted: dict[str, Path]) -> None:
    for key in ("active", "ok", "failed", "resumable", "markup"):
        run_id = planted[key].name
        expected, diagnostic, _rc = runs_cmd.run_detail_contract(planted[key])
        assert diagnostic is None
        status, body, _headers = _get_json(server, f"/api/runs/{run_id}")
        assert status == 200
        assert body == expected
        assert body["schema"] == "brigade.run-detail.v2"
        assert body["run"]["run_id"] == run_id
        for item in _walk(body):
            if isinstance(item, str):
                assert item not in FORBIDDEN_KEYS


def test_detail_includes_lineage_roster_verify_and_briefs(
    server: ThreadingHTTPServer,
    planted: dict[str, Path],
) -> None:
    status, parent, _ = _get_json(server, f"/api/runs/{planted['parent'].name}")
    assert status == 200
    children = parent["run"]["lineage"]["children"]
    assert any(child["run_id"] == planted["child"].name for child in children)

    status, child, _ = _get_json(server, f"/api/runs/{planted['child'].name}")
    assert status == 200
    assert child["run"]["lineage"]["parent_run_id"] == planted["parent"].name

    status, success, _ = _get_json(server, f"/api/runs/{planted['ok'].name}")
    assert status == 200
    coder_agent = next(iter(success["roster"]["agents"].values()))
    assert set(coder_agent["model"]) == {"digest", "chars"}
    assert set(coder_agent["reasoning"]) == {"digest", "chars"}
    assert success["verification"]
    assert {brief["name"] for brief in success["briefs"]} >= {"code-graph", "drift-impact", "evidence"}
    assert success["run"]["resume_available"] is not True

    status, resumable, _ = _get_json(server, f"/api/runs/{planted['resumable'].name}")
    assert status == 200
    assert resumable["run"]["resume_available"] is True

    status, failed, _ = _get_json(server, f"/api/runs/{planted['failed'].name}")
    assert status == 200
    assert failed["run"]["failure"]["phase"] == "worker"
    assert failed["run"]["failure"]["kind"]["chars"] == len("exit-error")


def test_missing_runs_dir_is_empty_state_with_diagnostic(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    httpd = runs_serve.make_server(cwd=workspace, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_until_ready(httpd)
    try:
        status, body, headers = _get_json(httpd, "/api/runs")
        assert status == 200
        assert body == {"schema": "brigade.runs-list.v2", "runs": [], "skipped_invalid": 0}
        assert "X-Brigade-Diagnostic: runs-directory-missing" in headers
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unreadable_selected_run_returns_stale_signal(
    server: ThreadingHTTPServer,
    planted: dict[str, Path],
) -> None:
    run_id = planted["ok"].name
    status, body, _ = _get_json(server, f"/api/runs/{run_id}")
    assert status == 200
    assert body["run"]["run_id"] == run_id
    planted["ok"].joinpath("run.json").write_text("{broken\n")
    response = _raw_request(server, _host(server), path=f"/api/runs/{run_id}")
    assert _status_code(response) == "502"
    headers, body = _headers_and_body(response)
    assert body == "error: run unreadable\n"
    assert str(planted["ok"]) not in response
    assert "X-Brigade-Diagnostic: run-unreadable" in headers


def test_app_route_is_embedded_and_same_origin(server: ThreadingHTTPServer) -> None:
    response = _raw_request(server, _host(server), path="/")
    headers, body = _headers_and_body(response)
    assert _status_code(response) == "200"
    assert "text/html" in headers
    assert "Content-Security-Policy:" in headers
    assert "default-src 'none'" in headers
    assert "frame-ancestors 'none'" in headers
    assert "X-Frame-Options: DENY" in headers
    assert "X-Content-Type-Options: nosniff" in headers
    assert "Referrer-Policy: no-referrer" in headers
    assert "Cache-Control: no-store" in headers
    assert "Access-Control-Allow-Origin" not in headers
    assert "cdn." not in body.lower()
    assert "https://" not in body.lower()
    assert "innerHTML" not in body
    assert "textContent" in body
    assert "prefers-color-scheme: dark" in body
    assert "prefers-reduced-motion: reduce" in body
    assert "max-width: 720px" in body
    assert "<nav" in body
    assert "<main" in body
    assert "<article" in body
    assert 'for="filter-status"' in body
    assert "navigator.clipboard" not in body
    assert "download=" not in body
    assert "text/csv" not in body
    assert "EventSource" in body
    assert "MAX_BACKOFF" in body
    assert "last readable state" in body


def test_planted_markup_is_text_not_html(server: ThreadingHTTPServer, planted: dict[str, Path]) -> None:
    run_id = planted["markup"].name
    status, detail, _ = _get_json(server, f"/api/runs/{run_id}")
    assert status == 200
    assert detail["run"]["task"]["chars"] == len(PLANTED_MARKUP)
    assert PLANTED_MARKUP not in json.dumps(detail)
    assert "<script>" not in json.dumps(detail)
    app = _raw_request(server, _host(server), path="/")
    _headers, body = _headers_and_body(app)
    assert PLANTED_MARKUP not in body
    assert "<script>document.title" not in body
    assert "textContent" in body
    assert "innerHTML" not in body
    source = Path("src/brigade/runs_view.html").read_text()
    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source
    assert "textContent" in source


def test_events_stream_uses_watch_contract(server: ThreadingHTTPServer, planted: dict[str, Path]) -> None:
    run_id = planted["ok"].name
    response = _raw_request(server, _host(server), path=f"/api/runs/{run_id}/events", timeout=4)
    assert _status_code(response) == "200"
    headers, body = _headers_and_body(response)
    assert "text/event-stream" in headers
    assert "Cache-Control: no-store" in headers
    records = []
    for block in body.split("\n\n"):
        line = block.strip()
        if line.startswith("data:"):
            records.append(json.loads(line[5:].strip()))
    assert records
    assert all(record["schema"] == "brigade.run-watch.v2" for record in records)
    types = {record.get("type") for record in records}
    assert "watch" in types
    assert "run" in types


def test_unexpected_sse_origin_is_rejected(server: ThreadingHTTPServer, planted: dict[str, Path]) -> None:
    run_id = planted["ok"].name
    response = _raw_request(
        server,
        _host(server),
        path=f"/api/runs/{run_id}/events",
        extra_headers=["Origin: http://evil.example.invalid"],
    )
    assert _status_code(response) == "403"
    assert "Access-Control-Allow-Origin" not in response


@pytest.mark.parametrize("method", MUTATING)
@pytest.mark.parametrize("path", ["/", "/api/runs", "/api/runs/20260821-100100-success"])
def test_mutating_methods_return_405(server: ThreadingHTTPServer, method: str, path: str) -> None:
    response = _raw_request(server, _host(server), method=method, path=path)
    assert _status_code(response) == "405"
    headers, _ = _headers_and_body(response)
    assert "default-src 'none'" in headers
    assert "Access-Control-Allow-Origin" not in headers


def test_forged_host_is_rejected_before_routing(server: ThreadingHTTPServer) -> None:
    response = _raw_request(server, "evil.example.invalid", path="/api/runs")
    headers, _ = _headers_and_body(response)
    assert _status_code(response) == "403"
    assert "default-src 'none'" in headers


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/../secret",
        "/api/runs/%2e%2e%2fsecret",
        "/api/runs/..",
        "/api/runs/.",
        "/api/runs/%2Ftmp%2Foutside",
        "/api/runs/latest",
        "/api/runs/not-a-real-run",
    ],
)
def test_malformed_and_traversal_ids_are_rejected(server: ThreadingHTTPServer, path: str) -> None:
    response = _raw_request(server, _host(server), path=path)
    assert _status_code(response) in {"404", "400"}


def test_symlink_escape_is_rejected(workspace: Path, planted: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside-run"
    _plant_run(
        outside,
        status="ok",
        task="outside planted run",
        started_at="2026-08-21T11:00:00Z",
        finished_at="2026-08-21T11:00:01Z",
        duration_seconds=1.0,
    )
    link = _runs_root(workspace) / "20260821-110000-symlink"
    link.symlink_to(outside)
    httpd = runs_serve.make_server(cwd=workspace, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_until_ready(httpd)
    try:
        response = _raw_request(httpd, _host(httpd), path=f"/api/runs/{link.name}")
        assert _status_code(response) == "404"
        assert runs_serve.resolve_served_run_id(link.name, _runs_root(workspace)) is None
        status, body, _headers = _get_json(httpd, "/api/runs?limit=200")
        assert status == 200
        listed = json.dumps(body)
        assert "outside planted run" not in listed
        assert link.name not in {item["run_id"] for item in body["runs"]}
        assert body["skipped_invalid"] == 2
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_symlinked_out_of_root_run_is_absent_from_list_and_cli_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    root = _runs_root(workspace)
    _plant_run(
        root / "20260821-130000-inside1",
        status="ok",
        task="inside planted run",
        started_at="2026-08-21T13:00:00Z",
        finished_at="2026-08-21T13:00:01Z",
        duration_seconds=1.0,
    )
    marker = "ESCAPED-CROSS-REPO-MARKER-631"
    outside = tmp_path / "foreign-root" / "20260821-130100-foreign"
    _plant_run(
        outside,
        status="ok",
        task=marker,
        started_at="2026-08-21T13:01:00Z",
        finished_at="2026-08-21T13:01:01Z",
        duration_seconds=1.0,
    )
    link = root / "escapelink"
    link.symlink_to(outside)

    httpd = runs_serve.make_server(cwd=workspace, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_until_ready(httpd)
    try:
        status, body, _headers = _get_json(httpd, "/api/runs?limit=200")
        assert status == 200
        assert marker not in json.dumps(body)
        assert body["skipped_invalid"] == 1
        assert [item["run_id"] for item in body["runs"]] == ["20260821-130000-inside1"]
    finally:
        httpd.shutdown()
        httpd.server_close()

    payload, diagnostic, rc = runs_cmd.runs_list_contract(cwd=workspace, limit=200)
    assert rc == 0
    assert diagnostic is None
    assert payload is not None
    assert marker not in json.dumps(payload)
    assert payload["skipped_invalid"] == 1

    assert cli.main(["runs", "list", "--cwd", str(workspace), "--json", "--limit", "200"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert marker not in json.dumps(listed)
    assert listed["skipped_invalid"] == 1
    assert [item["run_id"] for item in listed["runs"]] == ["20260821-130000-inside1"]


def test_absolute_and_alternate_root_ids_are_rejected(
    workspace: Path, planted: dict[str, Path], tmp_path: Path
) -> None:
    other = tmp_path / "other-root" / "20260821-120000-other1"
    _plant_run(
        other,
        status="ok",
        task="alternate root run",
        started_at="2026-08-21T12:00:00Z",
        finished_at="2026-08-21T12:00:01Z",
        duration_seconds=1.0,
    )
    root = _runs_root(workspace)
    assert runs_serve.resolve_served_run_id(str(other), root) is None
    assert runs_serve.resolve_served_run_id(other.name, root) is None
    assert runs_serve.resolve_served_run_id("/tmp/20260821-120000-other1", root) is None
    assert runs_serve.resolve_served_run_id("../" + other.name, root) is None
    assert runs_serve.resolve_served_run_id(planted["ok"].name, root) == planted["ok"].resolve()


def test_list_and_detail_have_no_home_paths(server: ThreadingHTTPServer) -> None:
    _status, listed, _ = _get_json(server, "/api/runs?limit=50")
    blob = json.dumps(listed)
    assert "/home/" not in blob
    assert "/Users/" not in blob
    for item in listed["runs"]:
        status, detail, _ = _get_json(server, f"/api/runs/{item['run_id']}")
        assert status == 200
        rendered = json.dumps(detail)
        assert "/home/" not in rendered
        assert "/Users/" not in rendered


def test_served_views_and_stream_carry_no_prompt_text(server: ThreadingHTTPServer, workspace: Path) -> None:
    """Issue #2046: every served endpoint and the SSE stream use the closed projection."""
    private_home = "/home/someuser"  # content-guard: allow home-path
    markers = {
        "task": f"PRIVATE-TASK-MARKER under {private_home}/project",
        "assignment": f"PRIVATE-ASSIGNMENT-MARKER under {private_home}/plan.py",
        "worker": f"PRIVATE-WORKER-MARKER under {private_home}/worker.py",
        "synthesis": f"PRIVATE-SYNTHESIS-MARKER under {private_home}/synthesis.md",
        "command": f"{private_home}/.venv/bin/pytest -q PRIVATE-COMMAND-MARKER",
        "failure": f"PRIVATE-FAILURE-MARKER under {private_home}/failure.py",
        "final": f"PRIVATE-FINAL-MARKER under {private_home}/final.txt",
    }
    run_dir = _runs_root(workspace) / "20260821-140000-private"
    _plant_run(
        run_dir,
        status="failed",
        task=markers["task"],
        plan_task=markers["assignment"],
        started_at="2026-08-21T14:00:00Z",
        finished_at="2026-08-21T14:00:02Z",
        duration_seconds=2.0,
        failure={"phase": "worker", "kind": "exit-error", "detail": markers["failure"]},
        worker_detail=markers["worker"],
        synthesis_detail=markers["synthesis"],
        command=markers["command"],
    )
    (run_dir / "final.txt").write_text(markers["final"] + "\n")
    local_files = {
        path.name: path.read_bytes()
        for path in (
            run_dir / "run.json",
            run_dir / "plan.json",
            run_dir / "worker-results.json",
            run_dir / "synthesis.json",
            run_dir / "final.txt",
        )
    }
    status, listed, _ = _get_json(server, "/api/runs?limit=50")
    assert status == 200
    status, detail, _ = _get_json(server, f"/api/runs/{run_dir.name}")
    assert status == 200
    response = _raw_request(server, _host(server), path=f"/api/runs/{run_dir.name}/events", timeout=5)
    assert _status_code(response) == "200"
    _headers, body = _headers_and_body(response)
    streamed = [json.loads(block.strip()[5:]) for block in body.split("\n\n") if block.strip().startswith("data:")]

    served = "\n".join((json.dumps(listed), json.dumps(detail), json.dumps(streamed)))
    for marker in markers.values():
        assert marker not in served
    assert private_home not in served
    assert "/home/" not in served
    _assert_text_values_digested(listed)
    _assert_text_values_digested(detail)
    _assert_text_values_digested(streamed)
    assert any(record.get("type") == "final" for record in streamed)

    assert detail["run"]["task"]["chars"] == len(markers["task"])
    assert listed["runs"][0]["task"] == detail["run"]["task"]
    assert "task_preview" not in json.dumps(listed)
    assert "task_preview" not in json.dumps(detail)

    for name, content in local_files.items():
        assert (run_dir / name).read_bytes() == content


def _refuse_digest_key(workspace: Path) -> None:
    key_dir = workspace / ".brigade" / "memory"
    key_dir.mkdir(parents=True, exist_ok=True)
    key = key_dir / "search-log-key"
    key.write_text("ab" * 32 + "\n")
    key.chmod(0o644)


def _block_digest_key(workspace: Path) -> None:
    (workspace / ".brigade" / "memory").write_text("not a directory")


@pytest.mark.parametrize("block", [_refuse_digest_key, _block_digest_key])
def test_served_endpoints_fail_closed_without_digest_key(tmp_path: Path, block) -> None:
    """Issue #2046 review: a refused or unusable key yields 503, never raw text."""
    workspace = _workspace(tmp_path)
    run_dir = _runs_root(workspace) / "20260821-180000-keyfail"
    _plant_run(
        run_dir,
        status="ok",
        task="PRIVATE-KEY-TASK-MARKER",
        started_at="2026-08-21T18:00:00Z",
        finished_at="2026-08-21T18:00:01Z",
        duration_seconds=1.0,
    )
    block(workspace)

    httpd = runs_serve.make_server(cwd=workspace, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_until_ready(httpd)
    try:
        for path in ("/api/runs", f"/api/runs/{run_dir.name}", f"/api/runs/{run_dir.name}/events"):
            response = _raw_request(httpd, _host(httpd), path=path)
            assert _status_code(response) == "503", path
            headers, body = _headers_and_body(response)
            assert body == "error: run view unavailable\n"
            assert "X-Brigade-Diagnostic: digest-key-unavailable" in headers
            assert "PRIVATE-KEY-TASK-MARKER" not in response
            assert str(run_dir) not in response
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_watch_stream_sends_one_closed_error_record_without_key(tmp_path: Path) -> None:
    """The serve-only watch feed degrades to one closed record and ends."""
    workspace = _workspace(tmp_path)
    run_dir = _runs_root(workspace) / "20260821-181000-keyfail"
    _plant_run(
        run_dir,
        status="ok",
        task="PRIVATE-KEY-TASK-MARKER",
        started_at="2026-08-21T18:10:00Z",
        finished_at="2026-08-21T18:10:01Z",
        duration_seconds=1.0,
    )
    _refuse_digest_key(workspace)

    records = list(runs_cmd.iter_watch_records(run_dir, interval=0))

    assert records == [
        {
            "schema": "brigade.run-watch.v2",
            "type": "error",
            "error": "digest-key-unavailable",
        }
    ]


def test_served_detail_and_stream_close_roster_and_worker_text(server: ThreadingHTTPServer, workspace: Path) -> None:
    """Issue #2046 review: roster, agent, worker and failure kind leaks stay closed."""
    run_dir = _runs_root(workspace) / "20260821-190000-closed"
    _plant_run(
        run_dir,
        status="failed",
        task="PRIVATE-SERVED-TASK",
        started_at="2026-08-21T19:00:00Z",
        finished_at="2026-08-21T19:00:01Z",
        duration_seconds=1.0,
    )
    (run_dir / "final.txt").write_text("PRIVATE-SERVED-FINAL\n")
    meta = json.loads((run_dir / "run.json").read_text())
    meta["failure"] = {
        "phase": "dispatch",
        "kind": "auth failed: sk-ant-secret",
        "detail": "auth failed: sk-ant-secret",
    }
    _write_json(run_dir / "run.json", meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "ORCH-MARKER",
            "max_workers": 1,
            "allow_models": ["MODEL-MARKER", "/etc/ssl/private/key.pem"],
            "agents": {
                "AGENT-MARKER": {
                    "cli": "codex",
                    "model": "MODEL-AGENT-MARKER",
                    "reasoning": "Bearer tok_123",
                    "role": "ROLE-MARKER /etc/ssl/private/key.pem",
                }
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {"assignments": [{"stage": 1, "worker": "PLAN-WORKER-MARKER", "task": "PLAN-TASK-MARKER"}]},
    )
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "WORKER-NAME-MARKER",
                    "ok": False,
                    "status": "failed",
                    "task": "WORKER-TASK-MARKER",
                    "detail": "WORKER-DETAIL-MARKER",
                    "requested_model": "REQ-MODEL-MARKER",
                    "transport": "cli",
                    "failure": {
                        "class": "auth failed: sk-ant-secret",
                        "detail": "missing /etc/ssl/private/key.pem",
                    },
                }
            ]
        },
    )

    status, detail, _ = _get_json(server, f"/api/runs/{run_dir.name}")
    assert status == 200
    response = _raw_request(server, _host(server), path=f"/api/runs/{run_dir.name}/events", timeout=5)
    assert _status_code(response) == "200"
    _headers, body = _headers_and_body(response)
    streamed = [json.loads(block.strip()[5:]) for block in body.split("\n\n") if block.strip().startswith("data:")]
    served = json.dumps(detail) + json.dumps(streamed)

    for marker in (
        "ORCH-MARKER",
        "MODEL-MARKER",
        "AGENT-MARKER",
        "MODEL-AGENT-MARKER",
        "Bearer tok_123",
        "ROLE-MARKER",
        "PLAN-WORKER-MARKER",
        "PLAN-TASK-MARKER",
        "WORKER-NAME-MARKER",
        "WORKER-TASK-MARKER",
        "WORKER-DETAIL-MARKER",
        "REQ-MODEL-MARKER",
        "auth failed: sk-ant-secret",
        "/etc/ssl/private/key.pem",
    ):
        assert marker not in served, marker

    agent_key = next(iter(detail["roster"]["agents"]))
    assert agent_key.startswith("hmac-sha256:")
    assert set(detail["run"]["failure"]["kind"]) == {"digest", "chars"}
    assert detail["run"]["failure"]["phase"] == "dispatch"


def _assert_text_values_digested(value: object) -> None:
    """Every text-bearing key in a served payload holds the closed digest object."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in {"task", "detail", "text", "command", "error", "failure_detail", "role"}:
                if inner is not None:
                    assert isinstance(inner, dict), f"{key} is not a digest object: {inner!r}"
                    assert set(inner) == {"digest", "chars"}
                    assert str(inner["digest"]).startswith("hmac-sha256:")
                    assert isinstance(inner["chars"], int)
            _assert_text_values_digested(inner)
    elif isinstance(value, list):
        for inner in value:
            _assert_text_values_digested(inner)


def _walk(value: object):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _walk(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk(inner)
    else:
        yield value
