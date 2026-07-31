"""Acceptance contract for the local, read-only Run View (#631).

The serializer checks in this file exercise today's command-level contracts.
The HTTP/UI check deliberately imports the future surface at runtime: this
station can therefore collect the contract coverage before that surface lands.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from brigade import cli, runs_cmd


LIST_KEYS = {
    "run_id",
    "status",
    "active_phase",
    "task_summary",
    "started_at",
    "status_started_at",
    "finished_at",
    "duration_seconds",
    "failure_phase",
    "mode",
    "stale",
    "resume_available",
}
DETAIL_KEYS = {"schema", "run", "roster", "plan", "workers", "synthesis", "verification", "briefs"}
ROSTER_KEYS = {"name", "cli", "model", "reasoning", "timeout_seconds"}
PLAN_KEYS = {"stage", "order", "worker", "task_summary"}
WORKER_KEYS = {"worker", "task_summary", "status", "ok", "detail", "duration_seconds", "exit_code"}
SYNTHESIS_KEYS = {"orchestrator", "ok", "detail"}
VERIFICATION_KEYS = {"receipt_id", "status", "duration_seconds", "exit_code", "command_label"}
BRIEF_KEYS = {"attached", "size_bytes", "count", "summary"}
WATCH_KEYS = {
    "watch": {"schema", "type", "run_id"},
    "run": {"schema", "type", "run"},
    "plan": {"schema", "type", "assignments"},
    "event": {"schema", "type", "worker", "method", "item_type"},
    "workers": {"schema", "type", "workers"},
    "synthesis": {"schema", "type", "synthesis"},
    "final": {"schema", "type", "available", "size_bytes"},
    "summary": {"schema", "type", "run_id", "status", "duration_seconds", "failure_phase", "resume_available"},
}
PRIVATE_MARKERS = (
    "ENV_VALUE_DO_NOT_EXPOSE",
    "TOKEN_REF_DO_NOT_EXPOSE",
    "SECRET_REF_DO_NOT_EXPOSE",
    "PROMPT_BODY_DO_NOT_EXPOSE",
    "TRANSCRIPT_BODY_DO_NOT_EXPOSE",
    "STDOUT_BODY_DO_NOT_EXPOSE",
    "STDERR_BODY_DO_NOT_EXPOSE",
    "VERIFY_ARGUMENT_DO_NOT_EXPOSE",
    "/opt/brigade/private",
    r"C:\\Synthetic\\private",
    r"\\synthetic-host\\private",
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _run_meta(*, task: str, status: str, started_at: str, **extra: object) -> dict[str, object]:
    return {
        "task": task,
        "status": status,
        "started_at": started_at,
        "status_started_at": started_at,
        "cwd": "/opt/brigade/private/workspace",
        "environment": {"RUN_VIEW_TEST_ENV": "ENV_VALUE_DO_NOT_EXPOSE"},
        "token_ref": "TOKEN_REF_DO_NOT_EXPOSE",
        "secret_ref": "SECRET_REF_DO_NOT_EXPOSE",
        "prompt": "PROMPT_BODY_DO_NOT_EXPOSE",
        "transcript": "TRANSCRIPT_BODY_DO_NOT_EXPOSE",
        "stdout": "STDOUT_BODY_DO_NOT_EXPOSE",
        "stderr": "STDERR_BODY_DO_NOT_EXPOSE",
        "control_socket": r"C:\\Synthetic\\private\\control.sock",
        **extra,
    }


def _write_run(root: Path, name: str, meta: dict[str, object], *, detail: bool = False) -> Path:
    run_dir = root / name
    _write_json(run_dir / "run.json", meta)
    if detail:
        _write_json(
            run_dir / "roster.json",
            {
                "orchestrator": "planner",
                "agents": {
                    "planner": {
                        "cli": "codex",
                        "model": "synthetic-model",
                        "reasoning": "medium",
                        "timeout_seconds": 90,
                        "environment": {"not_allowed": "ENV_VALUE_DO_NOT_EXPOSE"},
                    },
                    "worker": {"cli": "codex", "model": "synthetic-model", "timeout_seconds": 45},
                },
            },
        )
        _write_json(
            run_dir / "plan.json",
            {
                "assignments": [
                    {
                        "stage": 1,
                        "worker": "worker",
                        "task": "render <img src=x onerror=alert('untrusted')> as text",
                        "prompt": "PROMPT_BODY_DO_NOT_EXPOSE",
                    }
                ]
            },
        )
        _write_json(
            run_dir / "worker-results.json",
            {
                "results": [
                    {
                        "worker": "worker",
                        "task": "render <img src=x onerror=alert('untrusted')> as text",
                        "status": "failed",
                        "ok": False,
                        "detail": "failed near /opt/brigade/private/detail",
                        "duration_seconds": 1.25,
                        "exit_code": 17,
                        "thread_id": "TRANSCRIPT_BODY_DO_NOT_EXPOSE",
                        "stdout": "STDOUT_BODY_DO_NOT_EXPOSE",
                        "stderr": "STDERR_BODY_DO_NOT_EXPOSE",
                    }
                ],
                "ground_truth": {
                    "verify_receipts": [
                        {
                            "run_id": "receipt-synthetic",
                            "status": "failed",
                            "commands": [
                                {
                                    "command": "pytest -q --api-key VERIFY_ARGUMENT_DO_NOT_EXPOSE /opt/brigade/private",
                                    "status": "failed",
                                    "exit_code": 17,
                                }
                            ],
                        }
                    ]
                },
            },
        )
        _write_json(
            run_dir / "synthesis.json",
            {
                "orchestrator": "planner",
                "result": {
                    "ok": False,
                    "detail": "summary mentions \\\\synthetic-host\\private",
                    "text": "TRANSCRIPT_BODY_DO_NOT_EXPOSE",
                },
            },
        )
        (run_dir / "final.txt").write_text("TRANSCRIPT_BODY_DO_NOT_EXPOSE\n")
    return run_dir


@pytest.fixture
def run_view_workspace(tmp_path: Path) -> dict[str, Path]:
    """Synthetic examples only, including every state the browser must display."""
    root = tmp_path / ".brigade" / "runs"
    active = _write_run(
        root,
        "active-run",
        _run_meta(
            task="active synthetic task",
            status="dispatching",
            started_at="2026-07-30T09:00:00Z",
            active_phase="stage 1",
        ),
    )
    successful = _write_run(
        root,
        "successful-run",
        _run_meta(
            task="successful synthetic task",
            status="ok",
            started_at="2026-07-30T10:00:00Z",
            finished_at="2026-07-30T10:00:02Z",
            duration_seconds=2.0,
        ),
    )
    failed = _write_run(
        root,
        "failed-run",
        _run_meta(
            task="failed synthetic task",
            status="failed",
            started_at="2026-07-30T11:00:00Z",
            finished_at="2026-07-30T11:00:03Z",
            duration_seconds=3.0,
            failure={"phase": "worker", "kind": "command", "detail": "failed at /opt/brigade/private/failure"},
            code_graph_brief={"attached": True, "bytes": 10, "pending_count": 1},
            drift_impact_brief={"attached": True, "bytes": 20, "pending_count": 2},
            evidence_brief={"attached": True, "bytes": 30, "pending_count": 3},
        ),
        detail=True,
    )
    resumable = _write_run(
        root,
        "resumable-run",
        _run_meta(task="resumable synthetic task", status="failed", started_at="2026-07-30T12:00:00Z"),
    )
    _write_json(
        resumable / "worker-results.json",
        {"results": [{"worker": "worker", "status": "interrupted", "ok": False, "thread_id": "synthetic-thread"}]},
    )
    markup = _write_run(
        root,
        "markup-run",
        _run_meta(
            task="<script>window.runViewExecuted=true</script><img src=x onerror=alert('untrusted')>",
            status="ok",
            started_at="2026-07-30T13:00:00Z",
        ),
    )
    (root / "invalid-json").mkdir(parents=True)
    (root / "invalid-json" / "run.json").write_text("{")
    _write_json(
        root / "invalid name" / "run.json",
        _run_meta(task="invalid name", status="ok", started_at="2026-07-30T14:00:00Z"),
    )
    outside = _write_run(
        tmp_path / "outside", "escaped-run", _run_meta(task="outside", status="ok", started_at="2026-07-30T15:00:00Z")
    )
    os.symlink(outside, root / "symlink-run", target_is_directory=True)
    return {
        "workspace": tmp_path,
        "root": root,
        "active": active,
        "successful": successful,
        "failed": failed,
        "resumable": resumable,
        "markup": markup,
    }


def _serialized(payload: object) -> str:
    return json.dumps(payload, sort_keys=True)


def _assert_private_values_are_absent(payload: object) -> None:
    rendered = _serialized(payload)
    for marker in PRIVATE_MARKERS:
        assert marker not in rendered
    assert "/opt/brigade/private" not in rendered
    assert r"C:\\Synthetic\\private" not in rendered
    assert r"\\synthetic-host\\private" not in rendered
    assert "--api-key" not in rendered


def test_cli_json_contracts_freeze_safe_fields_and_fixture_states(run_view_workspace: dict[str, Path], capsys) -> None:
    workspace = run_view_workspace["workspace"]

    assert cli.main(["runs", "list", "--json", "--cwd", str(workspace), "--limit", "3"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert set(listed) == {"schema", "runs", "skipped_invalid"}
    assert listed["schema"] == "brigade.runs-list.v1"
    assert len(listed["runs"]) == 3
    assert listed["skipped_invalid"] == 3
    assert {item["run_id"] for item in listed["runs"]} <= {
        "active-run",
        "successful-run",
        "failed-run",
        "resumable-run",
        "markup-run",
    }
    for summary in listed["runs"]:
        assert set(summary) == LIST_KEYS
    _assert_private_values_are_absent(listed)

    assert cli.main(["runs", "show", "--json", str(run_view_workspace["failed"])]) == 1
    detail = json.loads(capsys.readouterr().out)
    assert set(detail) == DETAIL_KEYS
    assert detail["schema"] == "brigade.run-detail.v1"
    assert set(detail["run"]) == LIST_KEYS | {"failure_kind", "failure_detail"}
    assert detail["run"]["status"] == "failed"
    assert detail["run"]["failure_phase"] == "worker"
    assert detail["run"]["failure_kind"] == "command"
    assert detail["run"]["failure_detail"] == "failed at [path]"
    assert all(set(item) <= ROSTER_KEYS for item in detail["roster"])
    assert all(set(item) <= PLAN_KEYS for item in detail["plan"])
    assert all(set(item) <= WORKER_KEYS for item in detail["workers"])
    assert set(detail["synthesis"]) <= SYNTHESIS_KEYS
    assert all(set(item) <= VERIFICATION_KEYS for item in detail["verification"])
    assert set(detail["briefs"]) == {"code_graph", "drift_impact", "evidence"}
    assert all(set(item) <= BRIEF_KEYS for name, item in detail["briefs"].items() if name != "evidence")
    assert set(detail["briefs"]["evidence"]) <= BRIEF_KEYS | {"untrusted"}
    assert detail["briefs"]["evidence"]["untrusted"] is True
    assert detail["verification"] == [
        {
            "receipt_id": "receipt-synthetic",
            "status": "failed",
            "exit_code": 17,
            "command_label": "pytest",
        }
    ]
    _assert_private_values_are_absent(detail)

    assert cli.main(["runs", "latest", "--json", "--cwd", str(workspace)]) == 0
    latest = json.loads(capsys.readouterr().out)
    assert set(latest) == DETAIL_KEYS
    assert latest["schema"] == "brigade.run-detail.v1"
    assert latest["run"]["run_id"] == "markup-run"
    _assert_private_values_are_absent(latest)


def test_watch_ndjson_freezes_record_shapes_and_omits_unknown_payloads(
    run_view_workspace: dict[str, Path], capsys
) -> None:
    records: list[dict[str, object]] = []
    assert (
        runs_cmd.watch(
            run_view_workspace["failed"],
            cwd=run_view_workspace["workspace"],
            interval=0,
            json_output=True,
            record_sink=records.append,
            recover_artifact_collection=False,
        )
        == 1
    )
    assert capsys.readouterr().out == ""
    assert [record["type"] for record in records] == [
        "watch",
        "run",
        "plan",
        "workers",
        "synthesis",
        "final",
        "summary",
    ]
    for record in records:
        assert record["schema"] == "brigade.run-watch.v1"
        record_type = record["type"]
        assert isinstance(record_type, str)
        assert set(record) <= WATCH_KEYS[record_type]
    assert set(records[1]["run"]) == LIST_KEYS
    assert all(set(item) <= PLAN_KEYS for item in records[2]["assignments"])
    assert all(set(item) <= WORKER_KEYS for item in records[3]["workers"])
    assert set(records[4]["synthesis"]) <= SYNTHESIS_KEYS
    _assert_private_values_are_absent(records)


def test_missing_runs_root_is_a_safe_empty_payload_but_cli_keeps_existing_error(tmp_path: Path, capsys) -> None:
    assert runs_cmd.runs_list_payload(cwd=tmp_path, missing_root_as_empty=True) == {
        "schema": "brigade.runs-list.v1",
        "runs": [],
        "skipped_invalid": 0,
        "diagnostic": "runs directory is unavailable",
    }
    assert cli.main(["runs", "list", "--json", "--cwd", str(tmp_path)]) == 2
    assert capsys.readouterr().err == f"error: runs directory not found: {tmp_path / '.brigade' / 'runs'}\n"


def test_existing_human_list_show_latest_and_watch_output_is_byte_compatible(tmp_path: Path, capsys) -> None:
    root = tmp_path / ".brigade" / "runs"
    failed = _write_run(
        root,
        "failed-run",
        _run_meta(
            task="failure task",
            status="failed",
            started_at="2026-07-30T11:00:00Z",
            finished_at="2026-07-30T11:00:03Z",
            duration_seconds=3,
            failure={"phase": "worker", "kind": "command", "detail": "synthetic failure"},
        ),
    )
    successful = _write_run(
        root,
        "successful-run",
        _run_meta(
            task="success task",
            status="ok",
            started_at="2026-07-30T12:00:00Z",
            finished_at="2026-07-30T12:00:02Z",
            duration_seconds=2,
        ),
    )

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--limit", "1"]) == 0
    assert capsys.readouterr().out == f"2026-07-30T12:00:00Z [ok] 2s {successful}\n  success task\n"

    assert cli.main(["runs", "show", str(failed)]) == 1
    assert capsys.readouterr().out == (
        f"run: {failed}\n"
        "status: failed\n"
        "task: failure task\n"
        "cwd: /opt/brigade/private/workspace\n"
        "mode: normal\n"
        "started: 2026-07-30T11:00:00Z\n"
        "finished: 2026-07-30T11:00:03Z\n"
        "duration: 3s\n"
        "failure phase: worker\n"
        "failure kind: command\n"
        "failure detail: synthetic failure\n"
    )

    assert cli.main(["runs", "latest", "--cwd", str(tmp_path)]) == 0
    assert capsys.readouterr().out == (
        f"run: {successful}\n"
        "status: ok\n"
        "task: success task\n"
        "cwd: /opt/brigade/private/workspace\n"
        "mode: normal\n"
        "started: 2026-07-30T12:00:00Z\n"
        "finished: 2026-07-30T12:00:02Z\n"
        "duration: 2s\n"
    )

    assert runs_cmd.watch(failed, cwd=tmp_path, interval=0) == 1
    assert capsys.readouterr().out == (
        f"watching: {failed}\n"
        "status: failed\n"
        "task: failure task\n"
        "failure phase: worker\n"
        "failure kind: command\n"
        "failure detail: synthetic failure\n"
        "summary: failed in 3s\n"
    )


def _run_view_module() -> Any:
    """Load the future surface at test runtime, keeping serializer coverage collectable."""
    try:
        return importlib.import_module("brigade.run_view_server")
    except ModuleNotFoundError:
        pytest.fail(
            "Run View implementation is absent. Add brigade.run_view_server and brigade.run_view_app with create_server() and serve() "
            "before enabling the integrated #631 acceptance lane.",
            pytrace=False,
        )


def _http_request(
    port: int, method: str, target: str, *, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(method, target, headers=headers or {"Host": f"127.0.0.1:{port}"})
    response = connection.getresponse()
    body = response.read()
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, body


@contextmanager
def _server_thread(server: Any) -> Iterator[tuple[Any, threading.Thread]]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, thread
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_run_view_http_and_ui_acceptance(run_view_workspace: dict[str, Path], monkeypatch, capsys) -> None:
    """One integrated test until the server/UI modules exist in this station.

    Required public seam: ``create_server(cwd)`` returns a standard library HTTP
    server. ``serve(cwd, *, no_open)`` is the foreground CLI entry point, so its
    browser choices are exercised through dispatch.
    """
    view = _run_view_module()
    assert hasattr(view, "create_server"), "Run View must expose create_server(cwd)."
    assert hasattr(view, "serve"), "Run View must expose serve(cwd, *, no_open)."

    server = view.create_server(run_view_workspace["workspace"])
    with _server_thread(server) as (live_server, thread):
        port = live_server.server_address[1]
        assert thread.is_alive()

        status, headers, body = _http_request(port, "GET", "/")
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        csp = headers["content-security-policy"]
        assert re.search(r"(?:script-src|style-src) 'nonce-[^']+'", csp)
        assert "connect-src 'self'" in csp
        assert "'unsafe-inline'" not in csp
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert "no-store" in headers["cache-control"]
        html = body.decode()
        assert "prefers-reduced-motion" in html
        assert "@media (max-width:" in html
        assert "aria-" in html
        assert "<button" in html
        assert ":focus-visible" in html
        assert "aria-label=" in html or "<label" in html
        assert not re.search(r"<(?:div|span|li)\b[^>]*\bonclick=", html)
        assert not re.search(r"querySelector(?:All)?\(\s*['\"](?:div|span|li)\b", html)
        assert "textContent" in html
        assert "innerHTML" not in html
        assert "http://" not in html and "https://" not in html

        status, headers, body = _http_request(port, "GET", "/api/runs?limit=2")
        assert status == 200
        assert headers["content-type"].startswith("application/json")
        assert "access-control-allow-origin" not in headers
        listed = json.loads(body)
        assert listed["schema"] == "brigade.runs-list.v1"
        assert len(listed["runs"]) == 2
        _assert_private_values_are_absent(listed)

        status, _, body = _http_request(port, "GET", "/api/runs/failed-run")
        assert status == 200
        detail = json.loads(body)
        assert detail["schema"] == "brigade.run-detail.v1"
        assert detail["run"]["status"] == "failed"
        _assert_private_values_are_absent(detail)

        status, headers, body = _http_request(port, "GET", "/api/runs/failed-run/events")
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        sse_records = [json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: ")]
        assert sse_records
        assert all(record["schema"] == "brigade.run-watch.v1" for record in sse_records)
        _assert_private_values_are_absent(sse_records)

        for target in (
            "/api/runs/../outside",
            "/api/runs/%2e%2e%2foutside",
            "/api/runs/symlink-run",
            "/api/runs/invalid%20name",
        ):
            status, _, _ = _http_request(port, "GET", target)
            assert status in {400, 404}
        status, _, _ = _http_request(port, "GET", "/api/runs/failed-run", headers={"Host": "attacker.invalid"})
        assert status == 400
        status, _, _ = _http_request(
            port,
            "GET",
            "/api/runs/failed-run/events",
            headers={"Host": f"127.0.0.1:{port}", "Origin": "https://attacker.invalid"},
        )
        assert status == 403
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT"):
            status, _, _ = _http_request(port, method, "/api/runs/failed-run")
            assert status == 405

    served: list[dict[str, object]] = []

    def fake_serve(cwd: Path, *, no_open: bool = False) -> int:
        served.append({"cwd": cwd, "no_open": no_open})
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(view, "serve", fake_serve)
        assert cli.main(["runs", "serve", "--cwd", str(run_view_workspace["workspace"]), "--no-open"]) == 0
        assert served == [{"cwd": run_view_workspace["workspace"], "no_open": True}]
        assert cli.main(["runs", "serve", "--cwd", str(run_view_workspace["workspace"])]) == 0
        assert served[-1] == {"cwd": run_view_workspace["workspace"], "no_open": False}

    bind_error = OSError("[Errno 98] Address already in use")

    def fail_to_bind(cwd: Path) -> Any:
        del cwd
        raise bind_error

    monkeypatch.setattr(view, "create_server", fail_to_bind)
    assert view.serve(run_view_workspace["workspace"], no_open=True) != 0
    assert str(bind_error) in capsys.readouterr().err
