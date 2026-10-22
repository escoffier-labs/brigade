"""Optional OpenClaw presence observation at the CLI/public-client boundary."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict

import pytest

from brigade import fleet_client, proc
from tests._home import set_home


@pytest.fixture
def native_cli(tmp_path, monkeypatch):
    """Only the two metadata commands exist. No chat/transcript command is allowed."""
    set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / ".brigade"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    browser_workspace = tmp_path / "browser-workspace"
    browser_workspace.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"node_token": "test-only", "hub_url": "https://hub.example"}
    )
    published = []
    monkeypatch.setattr(
        fleet_client,
        "publish_session",
        lambda snapshot: published.append(snapshot) or fleet_client.SessionWriteResult(ok=True),
    )

    # Translate only this fake CLI; retain proc.run's real subprocess supervision.
    script = tmp_path / "openclaw"
    real_run = proc.run

    def run_fixture(argv, **kwargs):
        if argv[0] == "openclaw":
            argv = [sys.executable, str(script), *argv[1:]]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(proc, "run", run_fixture)

    def configure(rows=None, *, has_more=False, total=None, agents=None, mode="ok"):
        if rows is None:
            rows = [{"sessionId": "native-session.v2", "agentId": "main", "updatedAt": int(time.time() * 1000)}]
        payload = {
            "count": len(rows),
            "totalCount": len(rows) if total is None else total,
            "hasMore": has_more,
            "sessions": rows,
        }
        if agents is None:
            agents = [{"id": "main", "workspace": str(workspace)}]
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys, time\n"
            f"mode = {mode!r}\n"
            "if mode == 'error':\n"
            "    print('private-error-body', file=sys.stderr)\n"
            "    sys.exit(3)\n"
            "if mode == 'oversize':\n"
            "    print('x' * (2 * 1024 * 1024))\n"
            "    sys.exit(0)\n"
            "if mode == 'malformed':\n"
            "    print('{private-invalid-json')\n"
            "    sys.exit(0)\n"
            "if sys.argv[1:] == ['agents', 'list', '--json']:\n"
            f"    print(json.dumps({agents!r}))\n"
            "elif sys.argv[1:] == ['sessions', '--all-agents', '--active', '15', '--limit', '200', '--json']:\n"
            f"    print(json.dumps({payload!r}))\n"
            "else:\n"
            "    sys.exit('non-metadata command forbidden')\n",
            encoding="utf-8",
        )
        script.chmod(0o700)

    configure()
    return configure, published, workspace, browser_workspace


def test_opt_in_publishes_recent_native_presence_without_private_metadata(native_cli, capsys):
    from brigade.fleet_openclaw_observer import main

    configure, published, workspace, browser_workspace = native_cli
    now = int(time.time() * 1000)
    configure(
        [
            {
                "sessionId": "native-session.v2",
                "agentId": "main",
                "updatedAt": now,
                "status": "done",
                "key": "private-participant-key",
                "model": "private-model",
                "message": "private-chat-body",
            },
            {"sessionId": "browser-session", "agentId": "browser-operator", "updatedAt": now - 2000},
            {"sessionId": "stale", "agentId": "main", "updatedAt": now - 901_000},
        ],
        agents=[
            {"id": "main", "workspace": str(workspace)},
            {"id": "browser-operator", "workspace": str(browser_workspace)},
        ],
    )
    assert main([]) == 0
    assert not published
    capsys.readouterr()
    assert main(["--publish"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recent_sessions"] == report["published"] == 2
    assert report["window_seconds"] == 900
    assert 0 <= report["newest_activity_age_seconds"] < 10
    assert {s.session_id for s in published} == {"native-session.v2", "browser-session"}
    assert {s.checkout_path for s in published} == {str(workspace), str(browser_workspace)}
    assert all(s.harness == "openclaw" and s.identity_scope == "node" for s in published)
    assert all(s.branch is None and s.ttl_seconds == 300 for s in published)
    wire = json.dumps([asdict(s) for s in published]) + json.dumps(report)
    assert "private-" not in wire
    assert "running" not in wire


def test_truncation_is_explicit_partial_result(native_cli, capsys):
    from brigade.fleet_openclaw_observer import main

    configure, published, _, _ = native_cli
    configure(has_more=True, total=201)
    assert main(["--publish"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["truncated"] is True
    assert report["total_sessions"] == 201
    assert report["published"] == len(published) == 1
    assert report["ok"] is False


@pytest.mark.parametrize("mode", ["error", "malformed", "oversize"])
def test_native_failure_is_not_healthy_empty_and_does_not_publish(native_cli, capsys, mode):
    from brigade.fleet_openclaw_observer import main

    configure, published, _, _ = native_cli
    configure(mode=mode)
    assert main(["--publish"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is False
    assert "private-" not in output
    assert not published


@pytest.mark.parametrize(
    "patch",
    [
        {"sessionId": "bad/session"},
        {"sessionId": "x" * 129},
        {"updatedAt": True},
        {"updatedAt": 9_999_999_999_999},
        {"updatedAt": None},
        {"agentId": "unconfigured"},
    ],
)
def test_invalid_native_metadata_preserves_valid_observations(native_cli, capsys, patch):
    from brigade.fleet_openclaw_observer import main

    configure, published, _, _ = native_cli
    row = {"sessionId": "session-ok", "agentId": "main", "updatedAt": int(time.time() * 1000)}
    configure([row, {**row, **patch}])
    assert main(["--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert len(published) == 1


def test_old_rows_stop_refreshing_and_empty_observation_is_reported(native_cli, capsys):
    from brigade.fleet_openclaw_observer import main

    configure, published, _, _ = native_cli
    assert main(["--publish"]) == 0
    assert len(published) == 1
    configure([{"sessionId": "native-session.v2", "agentId": "main", "updatedAt": 1}])
    assert main(["--publish"]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report["recent_sessions"] == report["published"] == 0
    assert report["newest_activity_age_seconds"] is None
    assert len(published) == 1


def test_node_credentials_required_and_publish_failure_is_visible(native_cli, monkeypatch, capsys):
    from brigade.fleet_openclaw_observer import main

    _, published, _, _ = native_cli
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"node_token": "", "hub_url": ""})
    assert main(["--publish"]) == 1
    assert not published
    assert json.loads(capsys.readouterr().out)["error"] == "node_auth_required"
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"node_token": "test-only", "hub_url": "https://hub.example"}
    )
    monkeypatch.setattr(
        fleet_client, "publish_session", lambda snapshot: fleet_client.SessionWriteResult(ok=False, reason="timeout")
    )
    assert main(["--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "publication_failed"


def test_unreferenced_missing_workspace_does_not_erase_valid_presence(native_cli, capsys):
    from brigade.fleet_openclaw_observer import main

    configure, published, workspace, _ = native_cli
    configure(
        agents=[{"id": "main", "workspace": str(workspace)}, {"id": "unused", "workspace": str(workspace / "missing")}]
    )
    assert main(["--publish"]) == 0
    assert len(published) == 1
    assert json.loads(capsys.readouterr().out)["invalid_rows"] == 0


def test_spawned_workspace_does_not_claim_shared_dirty_files(native_cli, capsys):
    import subprocess
    from brigade.fleet_openclaw_observer import main

    configure, published, _, actual = native_cli
    subprocess.run(["git", "init", "-q", str(actual)], check=True)
    (actual / "other-session.txt").write_text("unrelated dirty content")
    marker = actual / "monitor-invoked"
    monitor = actual / ("monitor.cmd" if os.name == "nt" else "monitor")
    # Git executes configured monitors through its shell; use a native Windows command.
    body = (
        f'@echo off\r\ntype nul > "{marker}"\r\n'
        if os.name == "nt"
        else (f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
    )
    monitor.write_text(body, encoding="utf-8")
    monitor.chmod(0o700)
    subprocess.run(["git", "-C", str(actual), "config", "core.fsmonitor", str(monitor)], check=True)
    configure(
        [
            {
                "sessionId": "spawned",
                "agentId": "main",
                "updatedAt": int(time.time() * 1000),
                "spawnedWorkspaceDir": str(actual),
            }
        ]
    )
    assert main(["--publish"]) == 0
    capsys.readouterr()
    assert published[0].checkout_path == str(actual)
    assert published[0].dirty_paths == ()
    assert published[0].dirty_truncated is True
    assert not marker.exists()


def test_empty_agents_and_cross_agent_collision_fail_visible(native_cli, capsys):
    from brigade.fleet_openclaw_observer import main

    configure, published, workspace, other = native_cli
    configure([], agents=[])
    assert main(["--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    now = int(time.time() * 1000)
    configure(
        [{"sessionId": "collision", "agentId": agent, "updatedAt": now} for agent in ("main", "other")],
        agents=[{"id": "main", "workspace": str(workspace)}, {"id": "other", "workspace": str(other)}],
    )
    assert main(["--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "native_identity_collision"
    assert not published


def test_shared_workspace_cached_and_publication_bounded_rotates(native_cli, monkeypatch, capsys):
    from brigade import fleet_openclaw_observer as observer

    configure, published, _, _ = native_cli
    now = int(time.time() * 1000)
    configure([{"sessionId": f"session-{i:03}", "agentId": "main", "updatedAt": now} for i in range(200)])
    calls = []
    original = observer._workspace_snapshot

    def snapshot(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(observer, "_workspace_snapshot", snapshot)
    monkeypatch.setattr(observer.time, "time_ns", lambda: now * 1_000_000)
    assert observer.main(["--publish"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["observed"] == 200
    assert report["published"] == observer.PUBLISH_LIMIT
    assert report["skipped"] == 200 - observer.PUBLISH_LIMIT
    assert len(calls) == 1
    first = {s.session_id for s in published}
    published.clear()
    monkeypatch.setattr(observer.time, "time_ns", lambda: (now + 120_000) * 1_000_000)
    assert observer.main(["--publish"]) == 2
    assert json.loads(capsys.readouterr().out)["observed_at_ms"] == now + 120_000
    assert first.isdisjoint({s.session_id for s in published})


def test_total_deadline_stops_before_fixed_post_and_counts_partial(native_cli, monkeypatch, capsys):
    from brigade import fleet_openclaw_observer as observer

    configure, published, _, _ = native_cli
    now = int(time.time() * 1000)
    configure([{"sessionId": f"session-{i}", "agentId": "main", "updatedAt": now} for i in range(4)])
    clock = [0.0]
    monkeypatch.setattr(observer.time, "monotonic", lambda: clock[0])
    original = fleet_client.publish_session

    def publish(snapshot):
        result = original(snapshot)
        clock[0] += 7
        return result

    monkeypatch.setattr(fleet_client, "publish_session", publish)
    assert observer.main(["--publish"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["error"] == "observation_timeout"
    assert report["published"] == len(published) == 2
    assert report["skipped"] == 2


def test_native_calls_share_remaining_budget(native_cli, monkeypatch, capsys):
    from brigade import fleet_openclaw_observer as observer
    from brigade.proc import Result

    clock = [0.0]
    timeouts = []
    monkeypatch.setattr(observer.time, "monotonic", lambda: clock[0])

    def run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 13
        return Result(code=0, stdout='[{"id":"main","workspace":"/missing"}]', stderr="")

    monkeypatch.setattr(observer.proc, "run", run)
    assert observer.main([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert all(t <= 15 for t in timeouts)
    assert len(timeouts) <= 2
    if len(timeouts) == 2:
        assert timeouts[1] < 2


@pytest.mark.parametrize(
    "payload",
    [
        {"sessions": [], "count": 1, "totalCount": 1, "hasMore": False},
        {"sessions": [], "count": 0, "totalCount": 1, "hasMore": False},
        {"sessions": [], "count": 0, "totalCount": -1, "hasMore": False},
    ],
)
def test_inconsistent_native_counts_are_visible(native_cli, monkeypatch, capsys, payload):
    from brigade import fleet_openclaw_observer as observer

    _, published, workspace, _ = native_cli
    monkeypatch.setattr(
        observer,
        "_native_json",
        lambda args, deadline: [{"id": "main", "workspace": str(workspace)}] if args[0] == "agents" else payload,
    )
    assert observer.main(["--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not published


def test_mid_publish_failure_keeps_success_count(native_cli, monkeypatch, capsys):
    from brigade.fleet_openclaw_observer import main

    configure, published, _, _ = native_cli
    now = int(time.time() * 1000)
    configure([{"sessionId": f"session-{i}", "agentId": "main", "updatedAt": now} for i in range(2)])
    original = fleet_client.publish_session

    def publish(snapshot):
        return original(snapshot) if not published else fleet_client.SessionWriteResult(ok=False)

    monkeypatch.setattr(fleet_client, "publish_session", publish)
    assert main(["--publish"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["published"] == len(published) == 1
    assert report["skipped"] == 1
    assert report["error"] == "publication_failed"


def test_thirteen_fast_publications_all_refresh_in_one_poll(native_cli, capsys):
    from brigade.fleet_openclaw_observer import main

    configure, published, _, _ = native_cli
    now = int(time.time() * 1000)
    configure([{"sessionId": f"session-{i:03}", "agentId": "main", "updatedAt": now} for i in range(13)])
    assert main(["--publish"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["published"] == len(published) == 13
    assert report["skipped"] == 0
    assert report["truncated"] is False
    assert all(s.ttl_seconds == 300 for s in published)


def test_slow_publication_rotation_does_not_alias_two_minute_polling(native_cli, monkeypatch, capsys):
    from brigade import fleet_openclaw_observer as observer

    configure, published, _, _ = native_cli
    now = int(time.time() * 1000)
    configure([{"sessionId": f"session-{i:03}", "agentId": "main", "updatedAt": now} for i in range(10)])
    clock = [0.0]
    epoch = [now]
    monkeypatch.setattr(observer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(observer.time, "time_ns", lambda: epoch[0] * 1_000_000)
    original = fleet_client.publish_session

    def publish(snapshot):
        result = original(snapshot)
        clock[0] += 7
        return result

    monkeypatch.setattr(fleet_client, "publish_session", publish)
    for poll in range(3):
        clock[0] = 0
        assert observer.main(["--publish"]) == 2
        report = json.loads(capsys.readouterr().out)
        assert report["published"] == 2
        assert report["observed_at_ms"] == now + poll * 120_000
        assert report["newest_activity_age_seconds"] == poll * 120
        epoch[0] += 120_000
    assert len({s.session_id for s in published}) == 6


def test_budget_exhausted_before_first_publish_is_reported_as_timeout(native_cli, monkeypatch, capsys):
    from brigade import fleet_openclaw_observer as observer

    _, published, workspace, _ = native_cli
    clock = [0.0]
    monkeypatch.setattr(observer.time, "monotonic", lambda: clock[0])
    row = {"sessionId": "recent", "agentId": "main", "updatedAt": int(time.time() * 1000)}

    def native(args, deadline):
        clock[0] += 7
        if args[0] == "agents":
            return [{"id": "main", "workspace": str(workspace)}]
        return {"sessions": [row], "count": 1, "totalCount": 1, "hasMore": False}

    monkeypatch.setattr(observer, "_native_json", native)
    assert observer.main(["--publish"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "observation_timeout"
    assert report["published"] == 0 and report["skipped"] == 1
    assert not published


def test_snapshot_failure_rotates_past_unavailable_workspace(native_cli, monkeypatch, capsys):
    from brigade import fleet_openclaw_observer as observer

    configure, published, bad, good = native_cli
    now = int(time.time() * 1000)
    configure(
        [
            {"sessionId": "bad", "agentId": "main", "updatedAt": now},
            {"sessionId": "good", "agentId": "other", "updatedAt": now},
        ],
        agents=[{"id": "main", "workspace": str(bad)}, {"id": "other", "workspace": str(good)}],
    )
    original = observer._workspace_snapshot

    def snapshot(target, **kwargs):
        if target == bad:
            raise observer.ObservationError("workspace_metadata_failed")
        return original(target, **kwargs)

    monkeypatch.setattr(observer, "_workspace_snapshot", snapshot)
    assert observer.main(["--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["published"] == 1
    assert published[0].session_id == "good"


def test_rotation_uses_portable_private_file_fallback(native_cli, monkeypatch, capsys):
    from brigade import fleet_openclaw_observer as observer

    _, published, _, _ = native_cli
    monkeypatch.setattr(fleet_client, "_dir_fd_supported", lambda: False)
    monkeypatch.delattr(observer.os, "getuid", raising=False)
    monkeypatch.delattr(observer.os, "O_NOFOLLOW", raising=False)
    assert observer.main(["--publish"]) == 0
    assert json.loads(capsys.readouterr().out)["published"] == 1
    assert len(published) == 1


@pytest.mark.parametrize("raises", [False, True])
def test_failed_publication_does_not_starve_later_rows(native_cli, monkeypatch, capsys, raises):
    from brigade.fleet_openclaw_observer import main

    configure, published, _, _ = native_cli
    now = time.time_ns() // 1_000_000
    configure([{"sessionId": name, "agentId": "main", "updatedAt": now} for name in ("a-bad", "b-bad", "c-good")])
    original = fleet_client.publish_session

    def publish(snapshot):
        if snapshot.session_id != "c-good":
            if raises:
                raise OSError("private-error")
            return fleet_client.SessionWriteResult(ok=False)
        return original(snapshot)

    monkeypatch.setattr(fleet_client, "publish_session", publish)
    assert main(["--publish"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["published"] == 1 and report["skipped"] == 2 and report["ok"] is False
    assert [s.session_id for s in published] == ["c-good"]


def test_detached_head_has_no_branch(native_cli, capsys):
    import subprocess
    from brigade.fleet_openclaw_observer import main

    _, published, workspace, _ = native_cli
    for args in (
        ["init", "-q"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        ["checkout", "-q", "--detach"],
    ):
        subprocess.run(["git", "-C", str(workspace), *args], check=True)
    assert main(["--publish"]) == 0
    capsys.readouterr()
    assert published[0].branch is None


def test_workspace_limit_matches_hub(native_cli, capsys):
    from brigade.fleet_openclaw_observer import main

    configure, published, _, _ = native_cli
    configure(agents=[{"id": "main", "workspace": "/" + "x" * 1024}])
    assert main(["--publish"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["invalid_rows"] >= 1
    assert not published
