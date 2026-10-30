"""Native T3 presence uses the read client, independently of managed receipts."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

import pytest

from brigade import fleet_client
from brigade.fleet_session_presence import ProviderLinkState
from tests._home import set_home


@pytest.fixture
def native_t3(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / ".brigade"))
    root = tmp_path / "project"
    root.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package = tmp_path / "t3_fleet"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "agent.py").write_text(
        "import json, os, threading, time\nfrom pathlib import Path\n"
        "if os.environ.get('FIXTURE_NATIVE_TIME'): time.time = lambda: float(os.environ['FIXTURE_NATIVE_TIME'])\n"
        "_RECORD_LOCK = threading.Lock()\n"
        "def record(event):\n"
        " # Detail workers append concurrently; Windows append handles can overwrite each other.\n"
        " with _RECORD_LOCK, Path(os.environ['FIXTURE_CALLS_PATH']).open('a') as stream:\n"
        "  stream.write(json.dumps(event) + '\\n')\n"
        "class Client:\n"
        " def shell_snapshot(self):\n"
        "  record('shell')\n"
        "  calls = Path(os.environ['FIXTURE_CALLS_PATH']).read_text().count('\\\"shell\\\"')\n"
        "  if calls > 1 and os.environ.get('FIXTURE_PHASE_FAILURE') == 'error': raise RuntimeError()\n"
        "  if calls > 1 and os.environ.get('FIXTURE_PHASE_FAILURE') == 'timeout': time.sleep(30)\n"
        "  if os.environ.get('FIXTURE_MODE') == 'sleep': time.sleep(30)\n"
        "  time.sleep(float(os.environ.get('FIXTURE_SHELL_DELAY', '0')))\n"
        "  return json.loads(Path(os.environ['FIXTURE_SHELL_PATH']).read_text())\n"
        " def thread_snapshot(self, thread_id):\n"
        "  record(thread_id)\n"
        "  if os.environ.get('FIXTURE_DETAIL_ERROR'): raise RuntimeError()\n"
        "  time.sleep(float(os.environ.get('FIXTURE_DETAIL_DELAY', '0')))\n"
        "  shell = json.loads(Path(os.environ['FIXTURE_SHELL_PATH']).read_text())\n"
        "  detail = next(r for r in shell['threads'] if r['id'] == thread_id)\n"
        "  if os.environ.get('FIXTURE_PROVIDER_MISMATCH'): detail['activeProviderThreadId'] = 'provider-other'\n"
        "  return {'thread': detail}\n"
        "class Agent:\n"
        " t3 = Client()\n"
        " @property\n"
        " def target_store(self): raise AssertionError('managed receipt store forbidden')\n"
        "def _build_agent(environ): return Agent()\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("FIXTURE_CALLS_PATH", str(tmp_path / "calls.jsonl"))
    published = []
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"node_token": "test-only", "hub_url": "https://hub.example"}
    )
    monkeypatch.setattr(
        fleet_client, "publish_session", lambda s: published.append(s) or fleet_client.SessionWriteResult(ok=True)
    )

    def configure(rows=None, *, drop_archived=False, **patch):
        if rows is None:
            rows = [
                {
                    "id": "ordinary-user-thread",
                    "projectId": "p",
                    "status": "running",
                    "activeRunId": "run",
                    "activityRunStatus": "running",
                    "worktreePath": str(worktree),
                    "latestVisibleMessage": "private-chat-body",
                    "title": "private-title",
                    "activeProviderThreadId": "private-native-provider-handle",
                }
            ]
        shell = {
            "schemaVersion": 2,
            "projects": [
                {"id": "p", "workspaceRoot": str(root)},
                {"id": "obsolete", "workspaceRoot": str(root / "missing")},
            ],
            "threads": rows,
            "archivedThreads": [],
        }
        shell.update(patch)
        if drop_archived:
            shell.pop("archivedThreads")
        (tmp_path / "shell.json").write_text(json.dumps(shell))
        monkeypatch.setenv("FIXTURE_SHELL_PATH", str(tmp_path / "shell.json"))

    configure()
    return configure, published, root, worktree


def test_ordinary_native_thread_published_without_chat_or_managed_receipts(native_t3, capsys):
    from brigade.fleet_t3_observer import main

    configure, published, _, worktree = native_t3
    configure(drop_archived=True)
    assert main(["--t3-python", sys.executable]) == 0
    assert not published
    capsys.readouterr()
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["active_threads"] == report["published"] == 1
    row = published[0]
    assert row.harness == "t3" and row.session_id == "ordinary-user-thread"
    assert row.checkout_path == str(worktree) and row.identity_scope == "node"
    assert row.dirty_paths == () and row.dirty_truncated and row.ttl_seconds == 300
    assert "private-" not in json.dumps(asdict(row)) + json.dumps(report)


def test_only_active_states_and_run_ids_count_not_archived_or_terminal(native_t3, capsys):
    from brigade.fleet_t3_observer import main

    configure, published, root, _ = native_t3
    rows = [
        {"id": state, "projectId": "p", "status": state, "activeRunId": None, "worktreePath": None}
        for state in ("queued", "preparing", "starting", "running", "waiting", "idle", "completed", "failed")
    ]
    # Exercise all five without hitting the intentional per-run publication cap.
    configure(rows, archivedThreads=[{**rows[3], "id": "archived"}])
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    capsys.readouterr()
    assert {s.session_id for s in published} == {"queued", "preparing", "starting", "running", "waiting"}
    assert all(s.checkout_path == str(root) for s in published)
    published.clear()
    configure([{"id": "run-only", "projectId": "p", "status": "idle", "activeRunId": "run", "worktreePath": None}])
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    capsys.readouterr()
    assert published[0].session_id == "run-only"


@pytest.mark.parametrize("module_name", ["json", "t3_fleet"])
def test_native_metadata_ignores_modules_in_caller_checkout(native_t3, tmp_path, monkeypatch, capsys, module_name):
    from brigade.fleet_t3_observer import main

    _, published, _, _ = native_t3
    checkout = tmp_path / "untrusted-checkout"
    checkout.mkdir()
    marker = checkout / "module-executed"
    (checkout / (module_name + ".py")).write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('caller module executed')\n"
        "raise RuntimeError('caller module executed')\n"
    )
    monkeypatch.chdir(checkout)
    result = main(["--t3-python", sys.executable])
    assert not marker.exists(), "native metadata imported a module from the caller checkout"
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["active_threads"] == 1 and report["total_threads"] == 1
    assert not published


@pytest.mark.parametrize(
    "patch",
    [
        {"schemaVersion": 1},
        {"threads": None},
        {"threads": [{"id": "broken", "projectId": "p", "status": "mystery", "activeRunId": None}]},
        {
            "threads": [
                {"id": "broken", "projectId": "p", "status": "running", "activeRunId": None, "worktreePath": "relative"}
            ]
        },
        {
            "projects": [],
            "threads": [
                {"id": "orphan", "projectId": "p", "status": "running", "activeRunId": None, "worktreePath": None}
            ],
        },
        {
            "threads": [
                {
                    "id": "invalid\u00a0id",
                    "projectId": "p",
                    "status": "running",
                    "activeRunId": "run",
                    "worktreePath": None,
                }
            ]
        },
        {"threads": [{"id": "broken", "projectId": "p", "status": "running", "worktreePath": None}]},
        {
            "threads": [
                {
                    "id": "invalid\x01id",
                    "projectId": "p",
                    "status": "running",
                    "activeRunId": "run",
                    "worktreePath": None,
                }
            ]
        },
    ],
)
def test_incompatible_or_malformed_metadata_is_not_healthy_empty(native_t3, capsys, patch):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    configure(**patch)
    assert main(["--t3-python", sys.executable, "--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not published


@pytest.mark.parametrize("native_id", ["delegated-" + "a" * 250, "delegated/task", "delegated%task"])
def test_nonopaque_native_id_has_stable_distinct_private_presence_identity(native_t3, capsys, native_id):
    import hashlib
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    other = native_id + "-other"
    configure(
        [
            {"id": value, "projectId": "p", "status": "running", "activeRunId": "run", "worktreePath": None}
            for value in (native_id, other)
        ]
    )
    reports = []
    for _ in range(2):
        assert main(["--t3-python", sys.executable, "--publish"]) == 0
        reports.append(json.loads(capsys.readouterr().out))
    expected = {"t3-" + hashlib.sha256(value.encode()).hexdigest() for value in (native_id, other)}
    assert {row.session_id for row in published[:2]} == expected
    assert {row.session_id for row in published[2:]} == expected
    assert len(expected) == 2
    wire = json.dumps([asdict(row) for row in published]) + json.dumps(reports)
    assert native_id not in wire and other not in wire


def test_missing_interpreter_and_native_deadline_fail_closed(native_t3, monkeypatch, capsys):
    from brigade import fleet_t3_observer as observer

    _, published, _, _ = native_t3
    assert observer.main(["--t3-python", "relative-python"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    monkeypatch.setenv("FIXTURE_MODE", "sleep")
    monkeypatch.setattr(observer, "NATIVE_TIMEOUT_SECONDS", 0.1)
    assert observer.main(["--t3-python", sys.executable]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "observation_timeout"
    assert not published


def test_count_and_output_bounds_do_not_look_idle(native_t3, capsys):
    from brigade.fleet_t3_observer import main

    configure, _, _, _ = native_t3
    configure([{}] * 10_001)
    assert main(["--t3-python", sys.executable]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_large_idle_shell_projects_only_current_active_metadata(native_t3, capsys):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    rows = [
        {
            "id": f"idle-{i}",
            "projectId": "obsolete",
            "status": "idle",
            "activeRunId": None,
            "activityRunStatus": None,
            "worktreePath": None,
            "latestVisibleMessage": "private-history",
        }
        for i in range(4000)
    ]
    rows.append(
        {"id": "ordinary-current", "projectId": "p", "status": "running", "activeRunId": "run", "worktreePath": None}
    )
    configure(rows)
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_threads"] == 4001
    assert report["active_threads"] == report["published"] == 1
    assert published[0].session_id == "ordinary-current"


@pytest.mark.parametrize(
    "bad",
    [
        {"worktreePath": "/definitely-missing-observer-worktree"},
        {"worktreePath": "/" + "x" * 300},
        {"id": "bad id"},
        {"id": None},
        {"id": 27},
        {"projectId": "orphan", "worktreePath": None},
        {"projectId": [], "worktreePath": None},
    ],
)
def test_invalid_active_row_preserves_valid_publication(native_t3, capsys, bad):
    from brigade.fleet_t3_observer import main

    configure, published, _, worktree = native_t3
    valid = {"id": "valid", "projectId": "p", "status": "running", "activeRunId": None, "worktreePath": str(worktree)}
    configure([valid, {**valid, "id": "broken", **bad}])
    assert main(["--t3-python", sys.executable, "--publish"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["invalid_rows"] == 1 and report["ok"] is False
    assert report["published"] == 1 and report["skipped"] == 1
    assert [s.session_id for s in published] == ["valid"]


def test_actual_worktree_needs_no_project_lookup(native_t3, capsys):
    from brigade.fleet_t3_observer import main

    configure, published, _, worktree = native_t3
    configure(
        [
            {
                "id": "orphan",
                "projectId": "missing",
                "status": "running",
                "activeRunId": None,
                "worktreePath": str(worktree),
            }
        ],
        projects=[],
    )
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    assert json.loads(capsys.readouterr().out)["invalid_rows"] == 0
    assert published[0].checkout_path == str(worktree)


@pytest.mark.parametrize(
    "pending",
    [
        {
            "pendingRuntimeRequest": {
                "id": "request",
                "kind": "permission",
                "createdAt": "now",
                "prompt": "private-prompt",
            }
        },
        {"pendingBackgroundTasks": [{"taskId": "task", "prompt": "private-prompt"}]},
    ],
)
def test_idle_pending_work_is_active_without_serializing_contents(native_t3, capsys, pending):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    row = {"id": "pending", "projectId": "p", "status": "idle", "activeRunId": None, "worktreePath": None}
    configure([{**row, **pending}, {**row, "id": "truly-idle"}])
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["active_threads"] == report["published"] == 1
    assert published[0].session_id == "pending"
    assert "private-" not in json.dumps(report) + json.dumps([asdict(s) for s in published])


@pytest.mark.parametrize(
    "patch",
    [
        {"pendingRuntimeRequest": []},
        {"pendingRuntimeRequest": {"id": "x", "kind": "unknown", "createdAt": "now"}},
        {"pendingBackgroundTasks": {}},
        {"pendingBackgroundTasks": ["bad"]},
        {"activityRunStatus": "unknown"},
    ],
)
def test_unknown_pending_shapes_and_activity_fail_whole_collection(native_t3, capsys, patch):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    row = {"id": "valid", "projectId": "p", "status": "running", "activeRunId": None, "worktreePath": None}
    configure([row, {**row, "id": "bad", **patch}])
    assert main(["--t3-python", sys.executable, "--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not published


def test_duplicate_native_identity_fails_entire_collection(native_t3, capsys):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    row = {"id": "duplicate", "projectId": "p", "status": "running", "activeRunId": None, "worktreePath": None}
    configure([row, row])
    assert main(["--t3-python", sys.executable, "--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "native_identity_collision"
    assert not published


@pytest.mark.parametrize("driver,harness", [("claudeAgent", "claude"), ("codex", "codex")])
def test_observer_publishes_strong_native_link_not_internal_provider_id(native_t3, capsys, driver, harness):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    configure(
        [
            {
                "id": "app-thread",
                "projectId": "p",
                "status": "running",
                "activeRunId": "run",
                "worktreePath": None,
                "activeProviderThreadId": "provider-thread",
                "providerThreads": [
                    {
                        "id": "provider-thread",
                        "driver": driver,
                        "providerSessionId": "internal-session",
                        "nativeThreadRef": {"driver": driver, "nativeId": "native-session", "strength": "strong"},
                    }
                ],
            }
        ]
    )
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    capsys.readouterr()
    assert published[0].provider_link == {"harness": harness, "session_id": "native-session"}


@pytest.mark.parametrize("strength,driver", [("weak", "codex"), ("strong", "other")])
def test_observer_does_not_guess_a_provider_link(native_t3, capsys, strength, driver):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    configure(
        [
            {
                "id": "app-thread",
                "projectId": "p",
                "status": "running",
                "activeRunId": "run",
                "worktreePath": None,
                "activeProviderThreadId": "provider-thread",
                "providerThreads": [
                    {
                        "id": "provider-thread",
                        "driver": "codex",
                        "nativeThreadRef": {"driver": driver, "nativeId": "native-session", "strength": strength},
                    }
                ],
            }
        ]
    )
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    capsys.readouterr()
    assert published[0].provider_link is None


def test_observer_links_a_batch_within_the_native_budget(native_t3, capsys, monkeypatch):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    monkeypatch.setenv("FIXTURE_DETAIL_DELAY", "0.15")
    configure(
        [
            {
                "id": f"app-thread-{i:02d}",
                "projectId": "p",
                "status": "running",
                "activeRunId": "run",
                "worktreePath": None,
                "activeProviderThreadId": f"provider-{i}",
                "providerThreads": [
                    {
                        "id": f"provider-{i}",
                        "driver": "codex",
                        "nativeThreadRef": {"driver": "codex", "nativeId": f"native-{i}", "strength": "strong"},
                    }
                ],
            }
            for i in range(50)
        ]
    )
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    capsys.readouterr()
    assert len(published) == 50
    assert all(isinstance(s.provider_link, dict) for s in published)
    from pathlib import Path

    calls = [json.loads(line) for line in Path(os.environ["FIXTURE_CALLS_PATH"]).read_text().splitlines()]
    assert calls.count("shell") == 1 and len(calls) == 51


@pytest.mark.parametrize("cursor", [0, 73])
@pytest.mark.parametrize("failure", [None, "error", "timeout", "removed"])
def test_large_fleet_resolves_only_the_publication_batch(native_t3, capsys, monkeypatch, cursor, failure):
    import hashlib
    from pathlib import Path

    from brigade import fleet_openclaw_observer as shared, fleet_t3_observer as observer

    configure, published, _, _ = native_t3
    # The old wall-clock detail window starts at 50, independently of the cursor.
    monkeypatch.setenv("FIXTURE_NATIVE_TIME", "3000")
    if failure in {"error", "timeout"}:
        monkeypatch.setenv("FIXTURE_PHASE_FAILURE", failure)
        monkeypatch.setattr(observer, "NATIVE_TIMEOUT_SECONDS", 1)
    rows = [
        {
            "id": f"app/thread-{i:02d}",
            "projectId": "p",
            "status": "running",
            "activeRunId": "run",
            "worktreePath": None,
            "activeProviderThreadId": f"provider-{i}",
            "providerThreads": [
                {
                    "id": f"provider-{i}",
                    "driver": "codex",
                    "nativeThreadRef": {"driver": "codex", "nativeId": f"native-{i}", "strength": "strong"},
                }
            ],
        }
        for i in range(100)
    ]
    if failure == "removed":
        for row in rows:
            row["providerThreads"] = []
    configure(rows)
    cursor_path, _ = shared._rotation_state("t3")
    shared._save_rotation(cursor_path, cursor)
    identities = sorted(("t3-" + hashlib.sha256(row["id"].encode()).hexdigest(), row) for row in rows)
    expected = (identities[cursor:] + identities[:cursor])[:50]
    assert observer.main(["--t3-python", sys.executable, "--publish"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["published"] == report["skipped"] == 50
    assert report["active_threads"] == 100 and "error" not in report
    assert [s.session_id for s in published] == [identity for identity, _ in expected]
    calls = [json.loads(line) for line in Path(os.environ["FIXTURE_CALLS_PATH"]).read_text().splitlines()]
    assert calls.count("shell") == 2
    if failure in {"error", "timeout"}:
        assert calls == ["shell", "shell"]
        assert all(s.provider_link is ProviderLinkState.UNKNOWN for s in published)
        assert all("provider_link" not in fleet_client._session_request_body(s, action="upsert") for s in published)
    else:
        assert calls[0:2] == ["shell", "shell"]
        assert sorted(calls[2:]) == sorted(row["id"] for _, row in expected)
        if failure == "removed":
            assert all(s.provider_link is None for s in published)
            assert all(
                fleet_client._session_request_body(s, action="upsert")["provider_link"] is None for s in published
            )
        else:
            assert [s.provider_link for s in published] == [
                {"harness": "codex", "session_id": row["providerThreads"][0]["nativeThreadRef"]["nativeId"]}
                for _, row in expected
            ]
    assert json.loads(cursor_path.read_text())["offset"] == (cursor + 50) % 100


def test_stalled_detail_read_does_not_block_publication(native_t3, capsys, monkeypatch):
    import time

    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    monkeypatch.setenv("FIXTURE_DETAIL_DELAY", "30")
    configure(
        [
            {
                "id": "app-thread-stalled",
                "projectId": "p",
                "status": "running",
                "activeRunId": "run",
                "worktreePath": None,
                "activeProviderThreadId": "provider-stalled",
                "providerThreads": [
                    {
                        "id": "provider-stalled",
                        "driver": "codex",
                        "nativeThreadRef": {"driver": "codex", "nativeId": "native-stalled", "strength": "strong"},
                    }
                ],
            }
        ]
    )
    started = time.monotonic()
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    capsys.readouterr()
    assert time.monotonic() - started < 8
    assert len(published) == 1
    assert published[0].provider_link is ProviderLinkState.UNKNOWN
    assert "provider_link" not in fleet_client._session_request_body(published[0], action="upsert")


def test_slow_shell_read_skips_detail_waits_past_the_budget(native_t3, capsys, monkeypatch):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    monkeypatch.setenv("FIXTURE_SHELL_DELAY", "5")
    monkeypatch.setenv("FIXTURE_DETAIL_DELAY", "30")
    configure(
        [
            {
                "id": "app-thread-slow-shell",
                "projectId": "p",
                "status": "running",
                "activeRunId": "run",
                "worktreePath": None,
                "activeProviderThreadId": "provider-slow",
                "providerThreads": [
                    {
                        "id": "provider-slow",
                        "driver": "codex",
                        "nativeThreadRef": {"driver": "codex", "nativeId": "native-slow", "strength": "strong"},
                    }
                ],
            }
        ]
    )
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    capsys.readouterr()
    assert len(published) == 1
    assert published[0].provider_link is ProviderLinkState.UNKNOWN
    assert "provider_link" not in fleet_client._session_request_body(published[0], action="upsert")


@pytest.mark.parametrize("native_id", [None, "", "has space", "x" * 300])
def test_invalid_optional_link_still_publishes_the_session(native_t3, capsys, native_id):
    from brigade.fleet_t3_observer import main

    configure, published, _, _ = native_t3
    configure(
        [
            {
                "id": "app-thread",
                "projectId": "p",
                "status": "running",
                "activeRunId": "run",
                "worktreePath": None,
                "activeProviderThreadId": "provider-thread",
                "providerThreads": [
                    {
                        "id": "provider-thread",
                        "driver": "codex",
                        "nativeThreadRef": {"driver": "codex", "nativeId": native_id, "strength": "strong"},
                    }
                ],
            }
        ]
    )
    assert main(["--t3-python", sys.executable, "--publish"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["invalid_rows"] == 0
    assert len(published) == 1
    assert published[0].provider_link is ProviderLinkState.UNKNOWN
    assert "provider_link" not in fleet_client._session_request_body(published[0], action="upsert")


@pytest.mark.parametrize(
    "outcome", ["no_active", "no_provider", "mismatch", "weak", "changed", "error", "timeout", "malformed"]
)
def test_provider_lookup_updates_stored_link_and_visibility(native_t3, monkeypatch, outcome):
    import sqlite3

    from brigade import fleet_hub, fleet_hub_sessions, fleet_t3_observer as observer

    configure, published, _, _ = native_t3
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: 1_700_000_000.0)
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    node = "22222222-2222-4222-8222-222222222222"
    row = {
        "id": "app-thread",
        "projectId": "p",
        "status": "running",
        "activeRunId": "run",
        "worktreePath": None,
        "activeProviderThreadId": "provider-first",
        "providerThreads": [
            {
                "id": "provider-first",
                "driver": "codex",
                "nativeThreadRef": {
                    "driver": "codex",
                    "nativeId": "native-first",
                    "strength": "strong",
                },
            }
        ],
    }
    configure([row])
    observer.observe(t3_python=sys.executable, publish=True)
    first = fleet_client._session_request_body(published[-1], action="upsert")
    fleet_hub_sessions.handle_session(conn, first, caller_node=node)
    fleet_hub_sessions.handle_session(
        conn, {**first, "harness": "codex", "session_id": "native-first", "provider_link": None}, caller_node=node
    )
    assert fleet_hub_sessions.count_live_by_node(conn, now_epoch=1_700_000_000.0) == {node: 1}
    if outcome == "no_active":
        row.pop("activeProviderThreadId")
    elif outcome == "no_provider":
        row["providerThreads"] = []
    elif outcome == "weak":
        row["providerThreads"][0]["nativeThreadRef"]["strength"] = "weak"
    elif outcome == "changed":
        row["activeProviderThreadId"] = row["providerThreads"][0]["id"] = "provider-second"
        row["providerThreads"][0]["nativeThreadRef"]["nativeId"] = "native-second"
    elif outcome == "mismatch":
        monkeypatch.setenv("FIXTURE_PROVIDER_MISMATCH", "1")
    elif outcome == "error":
        monkeypatch.setenv("FIXTURE_DETAIL_ERROR", "1")
    elif outcome == "timeout":
        monkeypatch.setenv("FIXTURE_DETAIL_DELAY", "30")
    else:
        row["providerThreads"][0]["nativeThreadRef"]["nativeId"] = "bad id"
    configure([row])
    observer.observe(t3_python=sys.executable, publish=True)
    wire = fleet_client._session_request_body(published[-1], action="upsert")
    _, result = fleet_hub_sessions.handle_session(conn, wire, caller_node=node)
    stored = result["session"].get("provider_link")
    if outcome in {"error", "timeout", "malformed"}:
        assert "provider_link" not in wire
        assert stored == {"harness": "codex", "session_id": "native-first"}
        assert fleet_hub_sessions.count_live_by_node(conn, now_epoch=1_700_000_000.0) == {node: 1}
    else:
        assert "provider_link" in wire
        expected = {"harness": "codex", "session_id": "native-second"} if outcome == "changed" else None
        assert wire["provider_link"] == stored == expected
        assert fleet_hub_sessions.count_live_by_node(conn, now_epoch=1_700_000_000.0) == {node: 2}
        assert "app-thread" in {s["session_id"] for s in fleet_hub_sessions.list_sessions(conn)}
    conn.close()
