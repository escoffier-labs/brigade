"""Fixed stdio Excalidraw MCP launcher for Obsidian Operator."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from brigade.grokbot_obsidian.adapters import ENV_ALLOWLIST
from brigade.grokbot_obsidian.contracts import ObsidianError
from brigade.grokbot_obsidian.excalidraw_client import (
    DEADLINE_SECONDS,
    EXCALIDRAW_MCP_TOOLS,
    MAX_OUTPUT_BYTES,
    StdioExcalidrawMcpClient,
    create_excalidraw_stdio_client,
)

SESSION = "session01"
SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys

with open(os.path.join(os.getcwd(), "record.json"), "w", encoding="utf-8") as handle:
    json.dump({"cwd": os.getcwd(), "argv": sys.argv, "env": dict(os.environ)}, handle)

def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        emit(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "excalidraw", "version": "1"},
                },
            }
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/call":
        name = request["params"]["name"]
        session = request["params"]["arguments"].get("sessionId", "")
        emit(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": f"Session ID: {session}\\n{name}"}]},
            }
        )
    else:
        emit({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": method}})
"""


def _script(path: Path, source: str = SCRIPT) -> Path:
    path.write_text(source, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def test_stdio_client_uses_absolute_executable_empty_argv_and_fixed_tools(tmp_path: Path, monkeypatch):
    script = _script(tmp_path / "excalidraw")
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    launched: dict[str, object] = {}
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        launched["argv"] = args[0]
        launched["cwd"] = kwargs.get("cwd")
        launched["shell"] = kwargs.get("shell")
        launched["env"] = dict(kwargs.get("env") or {})
        launched["pass_fds"] = kwargs.get("pass_fds")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("brigade.grokbot_obsidian.excalidraw_client.subprocess.Popen", spy)
    client = create_excalidraw_stdio_client(
        executable=str(script),
        staging_dir=str(staging),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "HOME": str(tmp_path),
            "SECRET": "nope",
            "BROWSER": "/bin/chrome",
        },
    )
    try:
        result = client.call_tool("start_session", {"sessionId": SESSION})
        assert result["content"][0]["text"].startswith(f"Session ID: {SESSION}")
        client.call_tool("create_diagram", {"sessionId": SESSION})
        client.call_tool("add_elements", {"sessionId": SESSION, "elements": []})
        client.call_tool("export_diagram", {"sessionId": SESSION, "path": str(staging / "out"), "format": "json"})
        with pytest.raises(ObsidianError) as caught:
            client.call_tool("command_execute", {})
        assert caught.value.code == "protocol_error"
    finally:
        client.close()
        client.close()
    payload = json.loads((staging / "record.json").read_text(encoding="utf-8"))
    assert payload["cwd"] == str(staging)
    assert len(payload["argv"]) == 1
    assert payload["argv"][0].startswith("/proc/self/fd/")
    assert launched["argv"] == payload["argv"]
    assert launched["pass_fds"]
    assert launched["cwd"] == str(staging)
    assert launched["shell"] is False
    assert "SECRET" not in launched["env"]
    assert launched["env"]["BROWSER"] == "/usr/bin/true"
    assert set(launched["env"]) <= set(ENV_ALLOWLIST) | {"BROWSER"}
    assert EXCALIDRAW_MCP_TOOLS == {"start_session", "create_diagram", "add_elements", "export_diagram"}
    assert DEADLINE_SECONDS == 45
    assert MAX_OUTPUT_BYTES == 262_144
    with pytest.raises(ObsidianError) as closed:
        client.call_tool("start_session", {"sessionId": SESSION})
    assert closed.value.code == "unavailable"


def test_stdio_factory_ignores_arbitrary_command_args_and_kills_on_close(tmp_path: Path):
    script = _script(tmp_path / "excalidraw")
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    client = create_excalidraw_stdio_client(
        executable=str(script),
        staging_dir=str(staging),
        env={"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(tmp_path)},
    )
    client.call_tool("start_session", {"sessionId": SESSION})
    pid = client.pid
    assert pid is not None
    client.close()
    with pytest.raises(ObsidianError):
        client.call_tool("start_session", {"sessionId": SESSION})
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("child still running")
    assert "args" not in StdioExcalidrawMcpClient.__init__.__code__.co_varnames


def test_stdio_client_enforces_deadline_and_output_cap(tmp_path: Path, monkeypatch):
    hanging = _script(
        tmp_path / "hang.py",
        "#!/usr/bin/env python3\nimport sys, time\ntime.sleep(2)\nsys.stdin.read()\n",
    )
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    monkeypatch.setattr("brigade.grokbot_obsidian.excalidraw_client.DEADLINE_SECONDS", 0.05)
    client = create_excalidraw_stdio_client(
        executable=str(hanging),
        staging_dir=str(staging),
        env={"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(tmp_path)},
    )
    try:
        with pytest.raises(ObsidianError) as timed:
            client.call_tool("start_session", {"sessionId": SESSION})
        assert timed.value.code in {"timeout", "unavailable"}
    finally:
        client.close()
    noisy = _script(
        tmp_path / "noisy.py",
        "#!/usr/bin/env python3\nimport sys\nsys.stdout.write('x' * 300000)\nsys.stdout.flush()\nsys.stdin.read()\n",
    )
    client = create_excalidraw_stdio_client(
        executable=str(noisy),
        staging_dir=str(staging),
        env={"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(tmp_path)},
    )
    try:
        with pytest.raises(ObsidianError) as capped:
            client.call_tool("start_session", {"sessionId": SESSION})
        assert capped.value.code == "unavailable"
        assert "x" * 16 not in str(capped.value)
    finally:
        client.close()


def test_replaced_executable_after_validation_is_never_executed(tmp_path: Path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    marker = tmp_path / "ran.txt"
    original = _script(
        tmp_path / "helper",
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('original', encoding='utf-8')\n"
        + SCRIPT.split("#!/usr/bin/env python3\n", 1)[1],
    )
    replacement = _script(
        tmp_path / "replacement",
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('replacement', encoding='utf-8')\n"
        "raise SystemExit(0)\n",
    )
    launched: dict[str, object] = {}
    real_popen = subprocess.Popen

    def swap_then_popen(*args, **kwargs):
        launched["argv"] = args[0]
        launched["pass_fds"] = kwargs.get("pass_fds")
        os.replace(replacement, original)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("brigade.grokbot_obsidian.excalidraw_client.subprocess.Popen", swap_then_popen)
    client = create_excalidraw_stdio_client(
        executable=str(original),
        staging_dir=str(staging),
        env={"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(tmp_path)},
    )
    try:
        client.call_tool("start_session", {"sessionId": SESSION})
    finally:
        client.close()
    assert marker.read_text(encoding="utf-8") == "original"
    argv = launched["argv"]
    assert isinstance(argv, list) and len(argv) == 1
    assert argv[0].startswith("/proc/self/fd/")
    assert argv[0] != str(original)
    assert launched["pass_fds"]


def test_helper_must_be_regular_executable(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    missing = tmp_path / "missing"
    with pytest.raises(ObsidianError) as caught:
        create_excalidraw_stdio_client(executable=str(missing), staging_dir=str(staging), env={})
    assert caught.value.code == "unavailable"
    assert str(missing) not in str(caught.value)
    link = tmp_path / "link"
    target = _script(tmp_path / "real")
    link.symlink_to(target)
    with pytest.raises(ObsidianError):
        create_excalidraw_stdio_client(executable=str(link), staging_dir=str(staging), env={})
    info = target.stat()
    assert stat.S_ISREG(info.st_mode)
