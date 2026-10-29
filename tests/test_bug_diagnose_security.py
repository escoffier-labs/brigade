"""Adversarial public-CLI contracts for bounded selected-project diagnosis."""

from __future__ import annotations

import builtins
import io
import json
import os
import socket
import stat
import subprocess

import pytest

from brigade import cli
from tests._home import set_home

CATALOG = ".brigade/mcp.json"
STATE = ".brigade/mcp/state.json"
NATIVE = ".codex/config.toml"
SECRET = "ghp_" + "FixtureOnlySecret" * 3


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Use only fake configuration and deny every runtime transport/launch."""
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    monkeypatch.setenv("BRIGADE_USER_DIR", str(home / ".brigade"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    target = tmp_path / "project"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    # Same canonical v1 shape used in the existing MCP CLI fixtures.
    for relative, content in {
        CATALOG: json.dumps({"version": 1, "servers": {"demo": {"command": "example-server", "timeout": 60}}}),
        STATE: json.dumps({"version": 1, "ownership": {}}),
        NATIVE: "# No selected native entry yet.\n",
    }.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.chdir(target)

    def denied(*args, **kwargs):
        pytest.fail("diagnosis attempted a network connection or subprocess launch")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    # Windows asyncio subclasses Popen during lazy imports. Keep its class
    # identity while refusing every attempted process construction.
    monkeypatch.setattr(subprocess.Popen, "__init__", denied)
    return target


def invoke(target, capsys, *extra, harness="codex", structured=True):
    code = cli.main(
        [
            "bug",
            "diagnose",
            "--target",
            str(target),
            "--harness",
            harness,
            "--summary",
            "Selected server unavailable",
            *(["--json"] if structured else []),
            *extra,
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def snapshot(root):
    """Include empty directories and symlinks, without following linked files."""
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        if path.is_symlink():
            content = os.readlink(path)
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, content)
    return result


def assert_safe(output, target):
    # Decode JSON too: escaped terminal bytes must not survive in structured fields.
    rendered = json.dumps(json.loads(output), ensure_ascii=False) if output.startswith("{") else output
    decoded = json.loads(output) if output.startswith("{") else output
    values = json.dumps(decoded, ensure_ascii=False) if isinstance(decoded, dict) else decoded
    assert SECRET not in rendered
    assert str(target) not in rendered
    assert "\x1b" not in values
    assert "\\u001b" not in values.lower()
    assert "\u202e" not in values


@pytest.mark.parametrize("parent", [".brigade", ".brigade/mcp", ".codex"])
def test_linked_parent_cannot_escape_selected_project(project, tmp_path, capsys, parent):
    original = project / parent
    outside = tmp_path / "outside"
    original.rename(outside)
    try:
        original.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    before = snapshot(tmp_path)
    code, output = invoke(project, capsys)
    assert code != 0, output
    assert_safe(output, project)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("relative", "kind"),
    [(CATALOG, "symlink"), (STATE, "hardlink"), (NATIVE, "symlink"), (NATIVE, "fifo"), (CATALOG, "directory")],
)
def test_unsafe_final_inputs_fail_closed(project, tmp_path, capsys, relative, kind):
    path = project / relative
    outside = tmp_path / "outside-file"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        if kind == "symlink":
            path.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, path)
        elif kind == "fifo":
            if not hasattr(os, "mkfifo"):
                pytest.skip("FIFOs unavailable")
            os.mkfifo(path)
        else:
            path.mkdir()
    except (OSError, NotImplementedError):
        pytest.skip(f"{kind} unavailable")
    before = snapshot(tmp_path)
    code, output = invoke(project, capsys)
    assert code != 0, output
    assert_safe(output, project)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("relative", [CATALOG, STATE, NATIVE])
@pytest.mark.parametrize("oversized", [False, True], ids=["malformed", "oversized-malformed"])
def test_parser_errors_never_disclose_input(project, tmp_path, capsys, relative, oversized):
    # Invalid UTF-8 and syntax can put attacker-controlled bytes in parser exceptions.
    payload = (SECRET + "\x1b[31m\u202e").encode() + b"\xff"
    if oversized:
        payload += b"x" * (64 * 1024)
    (project / relative).write_bytes(payload)
    before = snapshot(tmp_path)
    code, output = invoke(project, capsys)
    assert code != 0, output
    assert_safe(output, project)
    assert snapshot(tmp_path) == before


def test_user_scope_rejected_before_any_config_read(project, capsys, monkeypatch):
    original_io_open = io.open
    original_open = builtins.open

    def denied_read(*args, **kwargs):
        pytest.fail("user-scope diagnosis attempted a filesystem read")

    def check_python_open(original):
        def checked(path, mode="r", *args, **kwargs):
            if "r" in mode or "+" in mode:
                pytest.fail("user-scope diagnosis attempted a filesystem read")
            return original(path, mode, *args, **kwargs)

        return checked

    # Guard actual reads, not private helpers. Separate snapshots enforce no writes.
    with monkeypatch.context() as guard:
        guard.setattr(os, "read", denied_read)
        guard.setattr(io, "open", check_python_open(original_io_open))
        guard.setattr(builtins, "open", check_python_open(original_open))
        code, output = invoke(project, capsys, harness="codex-user")
    assert code != 0, output
    assert_safe(output, project)


@pytest.mark.parametrize("source", ["catalog-label", "sidecar-label", "native-label"])
@pytest.mark.parametrize("structured", [False, True], ids=["terminal", "json"])
def test_sensitive_labels_and_controls_never_escape(project, capsys, source, structured):
    label = SECRET + "\x1b[31m\u202e"
    if source == "catalog-label":
        # Valid entry reaches label rendering, rather than only exercising parse errors.
        data = {"version": 1, "servers": {label: {"command": "example-server", "timeout": 60}}}
        (project / CATALOG).write_text(json.dumps(data))
    elif source == "sidecar-label":
        data = {"version": 1, "ownership": {"codex": {NATIVE: {label: "invalid-record"}}}}
        (project / STATE).write_text(json.dumps(data))
    else:
        # Valid TOML with an invalid MCP entry forces native validation to handle the label.
        (project / NATIVE).write_text("[mcp_servers]\n" + json.dumps(label) + " = 7\n")
    code, output = invoke(project, capsys, structured=structured)
    if source != "catalog-label":
        assert code != 0, output
    assert_safe(output, project)


@pytest.mark.parametrize("kind", ["existing", "symlink", "dangling-symlink"])
def test_export_refuses_existing_or_linked_destination(project, tmp_path, capsys, kind):
    destination = project / "finding.jsonl"
    victim = tmp_path / "victim.jsonl"
    if kind == "existing":
        destination.write_text("preserve existing finding\n")
    else:
        if kind == "symlink":
            victim.write_text("preserve symlink target\n")
        try:
            destination.symlink_to(victim)
        except (OSError, NotImplementedError):
            pytest.skip("file symlinks unavailable")
    before = snapshot(tmp_path)
    code, output = invoke(project, capsys, "--save-finding", str(destination))
    assert code != 0, output
    assert_safe(output, project)
    assert snapshot(tmp_path) == before


def test_default_creates_no_project_or_user_state(project, tmp_path, capsys):
    # Cover the whole sandbox, including empty dirs and synthetic user roots.
    before = snapshot(tmp_path)
    code, output = invoke(project, capsys)
    assert code == 0, output
    assert json.loads(output)["verification"] == "not-run"
    assert json.loads(output)["runtime"] == "unknown"
    assert snapshot(tmp_path) == before
