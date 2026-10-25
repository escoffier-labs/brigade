"""Antigravity's silent, detached hook transport and presence lifecycle."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import antigravity_presence, cli, fleet_client
from brigade import fleet_session_presence as presence
from brigade.config import Config, write_config
from brigade.selection import Selection


@pytest.fixture(autouse=True)
def _configured_presence_hub(monkeypatch):
    # Target/payload tests must reach those boundaries instead of skipping for no Hub.
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:3774")
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "example-node-token")


def _stub_publisher_process(monkeypatch, factory):
    # Keep the stdlib class intact for asyncio's Windows Popen subclass.
    publisher_subprocess = SimpleNamespace(**vars(subprocess))
    publisher_subprocess.Popen = factory
    monkeypatch.setattr(antigravity_presence, "subprocess", publisher_subprocess)


@pytest.fixture()
def workspace(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "topic", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/example/project.git"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Example Dev",
            "-c",
            "user.email=dev@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    write_config(
        repo, Config(version=1, selection=Selection(depth="repo", harnesses=["antigravity"], owner="antigravity"))
    )
    return repo


@pytest.mark.parametrize("expected", ["start", "heartbeat"])
def test_hook_detaches_silent_publisher_and_preserves_session(workspace, monkeypatch, capsys, expected):
    launches = []

    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        return object()  # No wait/communicate methods: the hook cannot wait for the Hub.

    _stub_publisher_process(monkeypatch, spawn)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "conversationId": "conversation-1",
                    "workspacePaths": [str(workspace)],
                    "prompt": "PLANTED_PROMPT_DO_NOT_LEAK",
                    "repo_identity": "evil.invalid/planted",
                    "dirty_paths": ["planted.txt"],
                }
            )
        ),
    )
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", expected]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert len(launches) == 1
    argv, options = launches[0]
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["close_fds"] is True
    if os.name == "nt":
        assert options["creationflags"] & subprocess.DETACHED_PROCESS
    else:
        assert options["start_new_session"] is True
    assert "PLANTED" not in repr(launches)
    assert argv[:3] == [sys.executable, "-I", "-c"]
    assert argv[5:8] == ["brigade", "work", "presence-hook"]
    assert argv[argv.index("--event") + 1] == expected

    # Run the detached command's real entry point and inspect what reaches the client.
    snapshots = []
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:3774")
    monkeypatch.setattr(fleet_client, "upsert_session", lambda snap: snapshots.append(snap) or True)
    monkeypatch.setattr(fleet_client, "end_session", lambda snap: pytest.fail("Stop must not end a session"))
    assert cli.main(argv[6:]) == 0
    assert capsys.readouterr().out == ""
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert (snapshot.harness, snapshot.session_id, snapshot.branch) == ("antigravity", "conversation-1", "topic")
    assert snapshot.repo_identity == "github.com/example/project"
    assert snapshot.checkout_path == str(workspace.resolve())
    assert "planted.txt" not in snapshot.dirty_paths


@pytest.mark.parametrize(
    "raw",
    ["{}", "not json", "[]", '{"conversationId":1}', "x" * (presence.MAX_PRESENCE_STDIN_BYTES + 1)],
    ids=["empty-object", "malformed-json", "array", "invalid-session", "oversized"],
)
def test_invalid_payload_is_silent_and_does_not_spawn(monkeypatch, capsys, raw):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    _stub_publisher_process(monkeypatch, lambda *a, **kw: pytest.fail("invalid payload spawned a publisher"))
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", "start"]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_spawn_failure_is_silent(workspace, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "conversationId": "conversation-1",
                    "workspacePaths": [str(workspace)],
                }
            )
        ),
    )

    def fail(*args, **kwargs):
        raise OSError("publisher unavailable")

    _stub_publisher_process(monkeypatch, fail)
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", "start"]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_init_installs_named_antigravity_hooks_without_replacing_foreign_entries(tmp_path, monkeypatch, capsys):
    import shlex

    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    hooks_path = tmp_path / ".agents" / "hooks.json"
    hooks_path.parent.mkdir()
    foreign = {"formatter": {"PostToolUse": [{"matcher": "write_to_file", "hooks": [{"command": "format-code"}]}]}}
    hooks_path.write_text(json.dumps(foreign), encoding="utf-8")
    argv = ["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "antigravity"]
    assert cli.main(argv) == 0
    doc = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert doc["formatter"] == foreign["formatter"]
    managed = doc["brigade-presence"]
    for event, expected in (
        ("PreInvocation", "start"),
        ("PostToolUse", "heartbeat"),
        ("PostInvocation", "heartbeat"),
        ("Stop", "heartbeat"),
    ):
        handlers = managed[event]
        if event == "PostToolUse":
            assert handlers[0]["matcher"] == "*"
            handlers = handlers[0]["hooks"]
        assert len(handlers) == 1
        command = shlex.split(handlers[0]["command"])
        assert command == [
            "brigade",
            "work",
            "presence-hook",
            "--harness",
            "antigravity",
            "--stdin",
            "--event",
            expected,
        ]
    first = hooks_path.read_bytes()
    assert cli.main(argv) == 0
    assert hooks_path.read_bytes() == first
    capsys.readouterr()


@pytest.mark.parametrize("flag", ["--no-wire", "--dry-run"])
def test_init_without_wiring_does_not_install_hook(tmp_path, monkeypatch, flag):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    assert cli.main(["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "antigravity", flag]) == 0
    assert not (tmp_path / ".agents" / "hooks.json").exists()


@pytest.mark.parametrize("existing", ["{broken", "[]", '{"brigade-presence":{"enabled":false}}'])
def test_init_preserves_conflicting_hook_config(tmp_path, monkeypatch, capsys, existing):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    hooks_path = tmp_path / ".agents" / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text(existing, encoding="utf-8")
    assert cli.main(["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "antigravity"]) == 0
    assert hooks_path.read_text(encoding="utf-8") == existing
    assert "warning:" in capsys.readouterr().err


@pytest.mark.parametrize("selection", [None, "claude", "home"])
def test_hook_skips_unwired_unselected_and_operator_home(tmp_path, monkeypatch, capsys, selection):
    repo = tmp_path / "project"
    repo.mkdir()
    if selection is not None:
        write_config(
            repo,
            Config(
                version=1,
                selection=Selection(
                    depth="repo", harnesses=["antigravity" if selection == "home" else selection], owner="this-repo"
                ),
            ),
        )
    if selection == "home":
        monkeypatch.setattr(Path, "home", lambda: repo)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"conversationId": "conversation-1", "workspacePaths": [str(repo)]}))
    )
    _stub_publisher_process(monkeypatch, lambda *a, **kw: pytest.fail("unwired target spawned publisher"))
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", "start"]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_hook_process_returns_while_detached_publisher_is_blocked(workspace, tmp_path):
    """A live publisher cannot keep agy's hook stdout pipe or hook process open."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            started.set()
            release.wait(timeout=15)
            try:
                self.send_response(200)
                self.end_headers()
            finally:
                finished.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(Path(presence.__file__).resolve().parents[1]),
        BRIGADE_EXTRAS="1",
        BRIGADE_FLEET_HUB_URL=f"http://127.0.0.1:{server.server_port}",
        BRIGADE_FLEET_NODE_TOKEN="example-node-token",
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade",
                "work",
                "presence-hook",
                "--harness",
                "antigravity",
                "--stdin",
                "--event",
                "start",
            ],
            input=json.dumps({"conversationId": "conversation-1", "workspacePaths": [str(workspace)]}),
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
            check=True,
        )
        assert result.stdout == result.stderr == ""
        assert started.wait(timeout=10), "detached publisher did not reach the loopback endpoint"
        assert not finished.is_set(), "publisher was released before the hook returned"
        assert requests[0][0] == "/sessions"
        assert requests[0][1]["session_id"] == "conversation-1"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if started.is_set():
            assert finished.wait(timeout=5)


@pytest.mark.parametrize("shadow", ["brigade.py", "brigade/__init__.py"])
def test_publisher_does_not_import_workspace_copy(workspace, tmp_path, monkeypatch, capsys, shadow):
    executed = tmp_path / "workspace-code-executed"
    shadow_path = workspace / shadow
    shadow_path.parent.mkdir(parents=True, exist_ok=True)
    shadow_path.write_text(f"from pathlib import Path\nPath({str(executed)!r}).touch()\n", encoding="utf-8")
    processes = []

    def spawn(argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)
        processes.append(process)
        return process

    _stub_publisher_process(monkeypatch, spawn)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("PYTHONPATH", str(workspace))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"conversationId": "conversation-1", "workspacePaths": [str(workspace)]}))
    )
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", "start"]) == 0
    assert len(processes) == 1
    result = processes[0].wait(timeout=10)
    assert not executed.exists(), "publisher executed the workspace's Brigade copy"
    assert result == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_hook_finds_unique_wired_workspaces_after_unwired_roots(workspace, tmp_path, monkeypatch, capsys):
    unwired = tmp_path / "unwired"
    unwired.mkdir()
    other = tmp_path / "other-project"
    other.mkdir()
    write_config(
        other, Config(version=1, selection=Selection(depth="repo", harnesses=["antigravity"], owner="antigravity"))
    )
    launches = []
    _stub_publisher_process(monkeypatch, lambda argv, **kw: launches.append(argv) or object())
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "conversationId": "conversation-1",
                    "workspacePaths": [str(unwired), str(workspace), str(workspace), str(other)],
                }
            )
        ),
    )
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", "start"]) == 0
    assert [argv[argv.index("--target") + 1] for argv in launches] == [str(workspace.resolve()), str(other.resolve())]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("event", ["start", "heartbeat"])
def test_hook_without_configured_hub_does_not_launch_publisher(workspace, monkeypatch, capsys, event):
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
    launches = []
    _stub_publisher_process(monkeypatch, lambda argv, **kw: launches.append(argv) or object())
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"conversationId": "conversation-1", "workspacePaths": [str(workspace)]}))
    )
    assert not presence._hub_url_configured()
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", event]) == 0
    assert launches == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def _write_machine_identity():
    path = fleet_client.home_identity_target() / ".brigade" / "node.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'node_id = "22222222-2222-4222-8222-222222222222"\nhostname = "example-node"\nplatform = "test"\nroles = []\n',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("credential", [None, "admin"])
def test_hook_requires_node_or_admin_credential_before_spawning(workspace, monkeypatch, capsys, credential):
    monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN", raising=False)
    monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
    if credential == "admin":
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "example-admin-token")
        _write_machine_identity()
    settings = fleet_client.load_fleet_settings()
    assert settings["hub_url"]
    assert bool(settings["node_token"] or settings["admin_token"]) is (credential is not None)
    launches = []
    _stub_publisher_process(monkeypatch, lambda argv, **kw: launches.append(argv) or object())
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"conversationId": "conversation-1", "workspacePaths": [str(workspace)]}))
    )
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", "start"]) == 0
    assert len(launches) == (0 if credential is None else 1)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("identity", ["absent", "invalid", "valid"])
def test_admin_publisher_requires_claimable_machine_identity(workspace, monkeypatch, capsys, identity):
    monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN", raising=False)
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "example-admin-token")
    if identity != "absent":
        path = _write_machine_identity()
        if identity == "invalid":
            path.write_text("invalid node identity", encoding="utf-8")
    launches = []
    _stub_publisher_process(monkeypatch, lambda argv, **kw: launches.append(argv) or object())
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"conversationId": "conversation-1", "workspacePaths": [str(workspace)]}))
    )
    assert cli.main(["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", "start"]) == 0
    assert len(launches) == (1 if identity == "valid" else 0)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(("prune", "edited"), [(False, False), (True, False), (True, True)])
def test_reconfigure_prunes_only_unedited_antigravity_hook(tmp_path, monkeypatch, prune, edited):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    assert cli.main(["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "antigravity"]) == 0
    path = tmp_path / ".agents" / "hooks.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    foreign = {"Stop": [{"type": "command", "command": "custom-hook"}]}
    doc["foreign-hook"] = foreign
    if edited:
        doc["brigade-presence"]["enabled"] = False
    owned = doc["brigade-presence"]
    path.write_text(json.dumps(doc), encoding="utf-8")
    argv = ["reconfigure", "--target", str(tmp_path), "--harnesses", "codex"]
    if prune:
        argv.append("--prune")
    assert cli.main(argv) == 0
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["foreign-hook"] == foreign
    if prune and not edited:
        assert "brigade-presence" not in after
    else:
        assert after["brigade-presence"] == owned
    first = path.read_bytes()
    assert cli.main(argv) == 0
    assert path.read_bytes() == first


def test_prune_recovers_owned_hook_after_selection_already_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    assert cli.main(["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "antigravity"]) == 0
    argv = ["reconfigure", "--target", str(tmp_path), "--harnesses", "codex"]
    assert cli.main(argv) == 0
    path = tmp_path / ".agents" / "hooks.json"
    assert "brigade-presence" in json.loads(path.read_text(encoding="utf-8"))
    assert cli.main([*argv, "--prune"]) == 0
    assert "brigade-presence" not in json.loads(path.read_text(encoding="utf-8"))
