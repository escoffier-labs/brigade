"""Real presence entry points with vendor-shaped hook input."""

import io
import json
import subprocess
import time
from unittest.mock import Mock

import pytest

from brigade import fleet_session_presence as presence, observed_checks
from tests._home import set_home
from tests._posix import requires_dirfd
from tests.test_work_mode import _wired, _set_mode


@pytest.fixture
def target(tmp_path, monkeypatch, capsys):
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: 0.0)
    target = _wired(tmp_path, harnesses=("codex",))
    _set_mode(target, "observe", capsys)
    return target


def payload(target, harness, **extra):
    return {
        "hook_event_name": "PostToolUse",
        "session_id": "private-session",
        "workspace_roots": [target.as_posix()],
        "cwd": target.as_posix(),
        "model": "gpt-6.1-sol",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
        "tool_output": '{"exitCode":0}',
        "duration": 5432,
        **extra,
    }


def run(monkeypatch, harness, data, **kwargs):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    return presence.run_presence_hook(
        harness=harness,
        stdin=True,
        context="codex-observe",
        **kwargs,
    )


@pytest.mark.parametrize("harness", ["codex"])
@pytest.mark.parametrize("mode", ["strict", "observe", "off"])
def test_capture_mode_and_privacy(target, monkeypatch, capsys, harness, mode):
    _set_mode(target, mode, capsys)
    assert run(monkeypatch, harness, payload(target, harness)) == 0
    assert capsys.readouterr() == ("{}\n", "")
    records = observed_checks.read_checks(target)[0]
    assert len(records) == (mode == "observe")
    if records:
        record = records[0]
        assert (record.harness, record.model, record.agent_type) == (harness, "gpt-6.1-sol", "unknown")
        assert record.status == "unknown"
        assert record.duration_ms is None
        assert record.tree_fingerprint is None
        text = json.dumps(record.to_dict())
        assert "private-session" not in text and str(target) not in text and "pytest -q" not in text


@pytest.mark.parametrize("harness", ["codex"])
def test_no_process_or_snapshot(target, monkeypatch, harness):
    from brigade import localio
    from brigade import observe_harness_hooks as capture

    original = capture._capture
    mocks = [Mock(side_effect=AssertionError("capture spawned")) for _ in range(4)]

    def bounded(*args, **kwargs):
        with monkeypatch.context() as patch:
            for name, mock in zip(("run", "Popen", "call"), mocks, strict=False):
                patch.setattr(subprocess, name, mock)
            patch.setattr(localio, "tree_fingerprint", mocks[3])
            original(*args, **kwargs)

    monkeypatch.setattr(capture, "_capture", bounded)
    run(monkeypatch, harness, payload(target, harness))
    assert len(observed_checks.read_checks(target)[0]) == 1
    for mock in mocks:
        mock.assert_not_called()


@pytest.mark.parametrize("harness", ["codex"])
@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"tool_input": []},
        {"session_id": []},
        {"tool_input": {"command": "x" * 70000}},
    ],
)
def test_malformed(target, monkeypatch, capsys, harness, bad):
    data = payload(target, harness)
    data = {**data, **bad} if isinstance(bad, dict) else bad
    run(monkeypatch, harness, data)
    assert capsys.readouterr() == ("{}\n", "")
    assert not observed_checks.read_checks(target)[0]


@pytest.mark.parametrize("harness", ["codex"])
@pytest.mark.parametrize("stage", ["identity", "append"])
def test_deadline(target, monkeypatch, harness, stage):
    from brigade import observe_harness_hooks as capture

    now = [0.0]
    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: now[0])
    module, name = (capture, "codex_identity") if stage == "identity" else (observed_checks, "_digest_key")
    original = getattr(module, name)

    def expire(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 2.0
        return result

    monkeypatch.setattr(module, name, expire)
    run(monkeypatch, harness, payload(target, harness))
    assert not observed_checks.read_checks(target)[0]


@pytest.mark.parametrize("harness", ["codex"])
def test_pin_and_home_each_destination(target, tmp_path, monkeypatch, capsys, harness):
    other = _wired(tmp_path, name="other", harnesses=(harness,))
    _set_mode(other, "observe", capsys)
    data = payload(target, harness, tool_input={"command": f"cd {other.as_posix()} && pytest"})
    run(monkeypatch, harness, data, target=target)
    assert not observed_checks.read_checks(other)[0]
    run(monkeypatch, harness, data)
    assert len(observed_checks.read_checks(other)[0]) == 1
    set_home(monkeypatch, other)
    run(monkeypatch, harness, data)
    # Read with the original private storage location restored.
    set_home(monkeypatch, tmp_path / "home")
    assert len(observed_checks.read_checks(other)[0]) == 1


@pytest.mark.parametrize("mode", ["strict", "observe", "off"])
def test_codex_write_marker(target, monkeypatch, capsys, mode):
    from brigade.codex_observe_hooks import observe_session_path

    _set_mode(target, mode, capsys)
    run(monkeypatch, "codex", payload(target, "codex", tool_name="apply_patch"))
    key = observed_checks.digest_key_path(target)
    paths = list(key.parent.glob("sessions/*.json")) if key else []
    assert len(paths) == (mode == "observe")
    if paths:
        assert paths == [observe_session_path(target, "private-session")]
        assert set(json.loads(paths[0].read_text())) == {"last_write_at"}


def test_codex_install_preserves_and_refuses(tmp_path):
    from brigade.codex_observe_hooks import install_codex_observe_hooks, codex_observe_hooks_installed

    directory = tmp_path / ".codex"
    directory.mkdir()
    path = directory / "hooks.json"
    foreign = {"hooks": {"Stop": [{"hooks": [{"command": "foreign"}]}]}}
    path.write_text(json.dumps(foreign))
    assert install_codex_observe_hooks(tmp_path) is None
    first = path.read_text()
    assert install_codex_observe_hooks(tmp_path) is None
    assert path.read_text() == first
    assert codex_observe_hooks_installed(tmp_path)
    assert json.loads(first)["hooks"]["Stop"][0] == foreign["hooks"]["Stop"][0]
    for raw in ["{", "[]", '{"hooks": []}', '{"hooks": {"Stop": 3}}']:
        path.write_text(raw)
        assert install_codex_observe_hooks(tmp_path)
        assert path.read_text() == raw


@pytest.mark.parametrize("harness", ["codex"])
@pytest.mark.parametrize("windows", [False, True])
def test_append_preserves_other_writer(target, monkeypatch, harness, windows):
    from brigade import dirfd

    monkeypatch.setattr("brigade.localio.utc_now_iso", lambda: "2026-11-13T00:00:00+00:00")
    run(monkeypatch, harness, payload(target, harness))
    path = observed_checks.digest_key_path(target).with_name("checks.jsonl")
    first = path.read_bytes()
    original = dirfd.open_child_file

    def existing(parent, name, flags, *args, **kwargs):
        if name == "checks.jsonl" and windows:
            assert not (flags & __import__("os").O_CREAT and not flags & __import__("os").O_EXCL)
            flags &= ~__import__("os").O_APPEND
        return original(parent, name, flags, *args, **kwargs)

    monkeypatch.setattr(dirfd, "open_child_file", existing)
    run(monkeypatch, harness, payload(target, harness))
    assert path.read_bytes() == first * 2


@pytest.mark.parametrize("harness", ["codex"])
def test_hostile_bounded_input(target, monkeypatch, capsys, harness):
    for raw in [
        "{",
        "[]",
        "x" * 1_048_577,
        "[" * 1100,
        json.dumps(
            {
                **payload(target, harness),
                "extra": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]],
            }
        ),
    ]:
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        presence.run_presence_hook(harness=harness, stdin=True, context="codex-observe")
        assert capsys.readouterr() == ("{}\n", "")
    assert not observed_checks.read_checks(target)[0]


@pytest.mark.parametrize("slow", [False, True])
def test_codex_response_precedes_capture(target, monkeypatch, capsys, slow):
    from brigade import observe_harness_hooks as capture

    def broken(*args, **kwargs):
        assert capsys.readouterr() == ("{}\n", "")
        if slow:
            time.sleep(0.3)
        raise RuntimeError("capture failure")

    monkeypatch.setattr(capture, "_capture", broken)
    assert run(monkeypatch, "codex", payload(target, "codex")) == 0
    assert capsys.readouterr() == ("", "")


def test_ordinary_codex_write_preserves_cached_identity(target, monkeypatch):
    from brigade.codex_observe_hooks import observe_session_path

    run(monkeypatch, "codex", payload(target, "codex", tool_name="apply_patch"))
    path = observe_session_path(target, "private-session")
    state = {"model": "gpt-6.1-sol", "model_source": "hook-event"}
    path.write_text(json.dumps(state))
    run(monkeypatch, "codex", payload(target, "codex", tool_name="apply_patch", model="gpt-stale"))
    current = json.loads(path.read_text())
    assert current["model"] == state["model"] and current["model_source"] == state["model_source"]


@pytest.mark.parametrize(
    "harness,mode,installed",
    [("codex", "observe", True), ("codex", "strict", False), ("codex", "off", False)],
)
def test_mode_installs_only_for_codex(tmp_path, monkeypatch, capsys, harness, mode, installed):
    from brigade import cli

    set_home(monkeypatch, tmp_path / "home")
    target = _wired(tmp_path, harnesses=(harness,))
    _set_mode(target, mode, capsys)
    assert (target / ".codex/hooks.json").exists() is installed
    assert cli.main(["work", "mode", "show", "--target", str(target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["observe_hooks"] == {"codex": installed}


@pytest.mark.parametrize("tool", ["Bash", "apply_patch"])
def test_installed_command_pins_target(target, tmp_path, monkeypatch, capsys, tool):
    import shlex
    from brigade import cli
    from brigade.claude_hooks.package import COMMAND_PREFIX
    from brigade.codex_observe_hooks import observe_session_path

    other = _wired(tmp_path, name="other", harnesses=("codex",))
    _set_mode(other, "observe", capsys)
    document = json.loads((target / ".codex/hooks.json").read_bytes())
    handler = document["hooks"]["PostToolUse"][0]["hooks"][0]
    command = handler["command"]
    assert shlex.split(command)[:1] == shlex.split(COMMAND_PREFIX)[:1]
    assert shlex.split(command)[-2:] == ["--target", str(target.resolve())]
    assert handler["async"] is True
    data = payload(other, "codex", tool_name=tool)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    assert cli.main(shlex.split(command)[1:]) == 0
    assert not observed_checks.read_checks(other)[0]
    assert not observe_session_path(other, "private-session").exists()
    # A command launched in the pinned workspace cannot redirect a verifier either.
    data = payload(target, "codex", tool_input={"command": f"cd {other.as_posix()} && pytest"})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    assert cli.main(shlex.split(command)[1:]) == 0
    assert not observed_checks.read_checks(other)[0]


@pytest.mark.parametrize("action", ["strict", "off", "uninstall"])
def test_codex_cleanup_preserves_user_entries(target, capsys, action):
    from brigade import cli

    path = target / ".codex/hooks.json"
    doc = json.loads(path.read_bytes())
    foreign = {"hooks": [{"command": "user-hook"}], "matcher": "user"}
    for groups in doc["hooks"].values():
        groups.insert(0, foreign)
    path.write_bytes(json.dumps(doc).encode("utf-8"))
    if action == "uninstall":
        assert cli.main(["work", "hooks", "uninstall", "--target", str(target)]) == 0
    else:
        _set_mode(target, action, capsys)
    assert json.loads(path.read_bytes())["hooks"] == {"Stop": [foreign], "PostToolUse": [foreign]}


def test_codex_utf8_crlf_surgical_install_and_remove(tmp_path):
    from brigade.codex_observe_hooks import install_codex_observe_hooks, remove_codex_observe_hooks

    folder = tmp_path / ".codex"
    folder.mkdir()
    path = folder / "hooks.json"
    entry = '{ "hooks" : [ { "command" : "echo ō" } ], "matcher" : "user" }'
    original = (
        '{\r\n  "z": "ō",\r\n  "hooks" : { "Stop" : [\r\n    ' + entry + '\r\n  ] },\r\n  "a": 1\r\n}\r\n'
    ).encode("utf-8")
    path.write_bytes(original)
    path.chmod(0o640)
    mode = path.stat().st_mode
    assert install_codex_observe_hooks(tmp_path) is None
    installed = path.read_bytes()
    assert entry.encode("utf-8") in installed
    assert installed.startswith(b'{\r\n  "z":')
    assert installed.endswith(b',\r\n  "a": 1\r\n}\r\n')
    assert path.stat().st_mode == mode
    assert install_codex_observe_hooks(tmp_path) is None
    assert path.read_bytes() == installed
    assert remove_codex_observe_hooks(tmp_path) is None
    cleaned = path.read_bytes()
    assert entry.encode("utf-8") in cleaned
    assert cleaned.startswith(b'{\r\n  "z":')
    assert cleaned.endswith(b',\r\n  "a": 1\r\n}\r\n')
    assert path.stat().st_mode == mode


@requires_dirfd
@pytest.mark.parametrize("parent", [False, True])
def test_codex_refuses_symlink(tmp_path, parent):
    from brigade.codex_observe_hooks import install_codex_observe_hooks

    destination = tmp_path / "foreign"
    destination.mkdir()
    file = destination / "hooks.json"
    file.write_bytes(b'{"hooks":{}}')
    directory = tmp_path / ".codex"
    if parent:
        directory.symlink_to(destination, target_is_directory=True)
    else:
        directory.mkdir()
        (directory / "hooks.json").symlink_to(file)
    assert install_codex_observe_hooks(tmp_path)
    assert file.read_bytes() == b'{"hooks":{}}'


def test_reinstall_preserves_interleaved_user_entries(target):
    from brigade.codex_observe_hooks import install_codex_observe_hooks, remove_codex_observe_hooks

    path = target / ".codex/hooks.json"
    doc = json.loads(path.read_bytes())
    user = {"hooks": [{"command": "user-hook"}]}
    for entries in doc["hooks"].values():
        entries.append(user)
    raw = json.dumps(doc, ensure_ascii=False, indent=3).replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(raw)
    assert install_codex_observe_hooks(target) is None
    assert path.read_bytes() == raw
    # A user can put their handler alongside Brigade's within a group.
    doc["hooks"]["Stop"][0]["hooks"].append({"command": "mixed-user"})
    path.write_bytes(json.dumps(doc).encode("utf-8"))
    assert remove_codex_observe_hooks(target) is None
    assert json.loads(path.read_bytes())["hooks"]["Stop"] == [
        {"hooks": [{"command": "mixed-user"}]},
        user,
    ]
