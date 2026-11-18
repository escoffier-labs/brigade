"""A fake `claude` for the live paired adapter tests: recorded-style stream-json, no network.

The fake speaks real MCP stdio to the real paired server named in --mcp-config, so server
traces, receipts and rejections are genuine. It never contacts a model.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

SERVER = "brigade-paired"
TOOLS = ("brief", "verify", "receipt", "scoreability", "capture")
GOOD_STEPS = [{"call": tool} for tool in TOOLS]

_SCRIPT = r"""#!__PYTHON__
import json, os, subprocess, sys, uuid

SC = json.load(open("__SCENARIO__"))
if "--version" in sys.argv:
    if SC.get("version_hang"):
        import time
        time.sleep(600)
    print(SC.get("version", "2.1.327") + " (Claude Code)")
    sys.exit(0)
argv = sys.argv[1:]
opt = lambda name: argv[argv.index(name) + 1]
sid = opt("--session-id")
cfg = json.load(open(opt("--mcp-config")))
prompt = open(opt("--system-prompt-file"), "rb").read().decode()
stdin = sys.stdin.read()
with open(SC["record"], "a") as handle:
    handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "system_prompt": prompt,
                             "stdin": stdin, "session_id": sid, "config": cfg}) + "\n")
QUAL = "mcp__brigade-paired__"
TOOLS = ["brief", "verify", "receipt", "scoreability", "capture"]


def emit(event):
    print(json.dumps(event), flush=True)


proc = None
counter = [0]


def rpc(method, params=None):
    counter[0] += 1
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": counter[0], "method": method, "params": params or {}}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


if SC.get("leak"):  # A misbehaving CLI echoing its credential to the stream and to stderr.
    sys.stderr.write("auth debug " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"] + "\n")
for event in SC.get("pre_events", []):
    emit(event)
steps = SC.get("steps", [])
if steps:
    server = cfg["mcpServers"]["brigade-paired"]
    proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env={**os.environ, **server.get("env", {})})
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "0"}})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    listed = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
init = {
    "type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": sid,
    "tools": [QUAL + t for t in TOOLS] + (["EndConversation"] if SC.get("end_tool_listed") else []),
    "mcp_servers": [{"name": "brigade-paired", "status": "connected", "source": "dynamic"}],
    "model": "claude-opus-5-5", "permissionMode": "dontAsk", "slash_commands": [], "apiKeySource": "none",
    "claude_code_version": SC.get("version", "2.1.327"), "skills": [], "plugins": [],
    "agents": ["general-purpose", "Explore", "Plan"], "additional_directories": [],
    "uuid": str(uuid.uuid4()),
}
init.update(SC.get("init", {}))
emit(init)
if SC.get("leak"):
    emit({"type": "system", "subtype": "debug", "note": "token=" + os.environ["CLAUDE_CODE_OAUTH_TOKEN"]})
for event in SC.get("post_init_events", []):
    emit(event)
if SC.get("hang"):
    import time
    time.sleep(600)
receipt = None


def assistant(name, tool_input):
    tid = "toolu_" + uuid.uuid4().hex[:12]
    emit({"type": "assistant", "uuid": str(uuid.uuid4()), "session_id": sid,
          "message": {"id": "msg_" + uuid.uuid4().hex[:8], "role": "assistant",
                      "content": [{"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}})
    return tid


def result(tid, text):
    emit({"type": "user", "uuid": str(uuid.uuid4()), "session_id": sid,
          "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}})


for step in steps:
    if "call" in step or "server_only" in step or "emit_only" in step:
        tool = step.get("call") or step.get("server_only") or step.get("emit_only")
        shown = step.get("stream_input", step.get("input", {}))  # What the stream says may differ from the server.
        tid = None if "server_only" in step else assistant(QUAL + step.get("stream_as", tool), shown)
        if "emit_only" in step:
            continue
        reply = rpc("tools/call", {"name": tool, "arguments": step.get("input", {})})["result"]
        text = reply["content"][0]["text"]
        if tool == "receipt":
            receipt = json.loads(text).get("receipt")
        if tid:
            result(tid, text)
    elif "tool_use" in step:
        result(assistant(step["tool_use"], step.get("input", {})), "No such tool available")
    elif "event" in step:
        emit(step["event"])
    elif "block" in step:
        emit({"type": "assistant", "uuid": str(uuid.uuid4()), "session_id": sid,
              "message": {"id": "msg_x", "role": "assistant", "content": [step["block"]]}})
if SC.get("end_conversation"):
    assistant("EndConversation", {})
final = SC.get("final", "receipt")
if final == "receipt" and receipt:
    final = {"run_id": receipt["run_id"], "status": receipt["status"], "eligible": False}
if proc is not None:
    proc.stdin.close()
    proc.wait()
emit({"type": "result", "subtype": SC.get("result_subtype", "success"), "is_error": bool(SC.get("is_error")),
      "session_id": sid, "result": json.dumps(final) if isinstance(final, dict) else (final or "")})
"""


def make_fake(directory: Path, **scenario: Any) -> Path:
    """Write the fake executable and its scenario next to it. Returns the executable path."""
    directory.mkdir(parents=True, exist_ok=True)
    scenario.setdefault("record", str(directory / "record.jsonl"))
    scenario_path = directory / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    exe = directory / "claude"
    exe.write_text(_SCRIPT.replace("__PYTHON__", sys.executable).replace("__SCENARIO__", str(scenario_path)))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def records(directory: Path) -> list[dict[str, Any]]:
    path = directory / "record.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
