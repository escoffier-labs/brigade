import json
import subprocess
import sys

from brigade import tools_cmd
from brigade import work_cmd


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _init_git_repo(path):
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def _plan_task_id(tmp_path, capsys, **add_kwargs):
    add_kwargs.setdefault("text", "Build the planner")
    add_kwargs.setdefault("task_type", "feature")
    add_kwargs.setdefault("acceptance", ["Plan is written", "Plan is reviewed"])
    assert work_cmd.task_add(target=tmp_path, **add_kwargs) == 0
    out = capsys.readouterr().out
    return out.split("task: ", 1)[1].splitlines()[0]


def _make_research_run(tmp_path, run_id="r1", question="q"):
    from brigade.research import registry

    registry.create_run(tmp_path, question=question, run_id=run_id, caps={})
    registry.finish_run(
        tmp_path,
        run_id,
        status="done",
        stats={},
        artifacts={"report_md": "report.md"},
    )
    (registry.run_dir(tmp_path, run_id) / "report.md").write_text("# report\n")
    return run_id


def _accepted_plan_task_id(tmp_path, capsys, **add_kwargs):
    task_id = _plan_task_id(tmp_path, capsys, **add_kwargs)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 0
    capsys.readouterr()
    return task_id


def _assert_no_install_dirs(tmp_path, task_id):
    # Brigade must never write into install locations during promote.
    for rel in ("rules", "templates", "memory", "skills", ".claude/skills"):
        assert not (tmp_path / rel).exists(), f"unexpected install dir created: {rel}"
    proposals = work_cmd._plan_proposals_dir(tmp_path)
    written = sorted(p.name for p in proposals.glob("*.md")) if proposals.is_dir() else []
    assert written, "expected a proposal file under plan-proposals/"


def _write_script_tool_config(tmp_path, *, script: str, timeout: float = 5.0) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / "runner.py").write_text(script)
    (tools_dir / "input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": True,
            }
        )
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        f"""
[[tool]]
id = "runner"
name = "Runner"
family = "script"
enabled = true
description = "Run local script."
command = "{sys.executable} tools/runner.py"
input_schema_path = "tools/input.schema.json"
timeout = {timeout}
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
argument_template = {{ path = "{{path}}" }}
supported_harnesses = []
"""
    )


def _write_runtime_config(
    tmp_path,
    *,
    runtime_id="helper",
    command=None,
    health_command=None,
    health_path=None,
    cwd=".",
    port=None,
):
    command = command or f'{sys.executable} -c "import time; time.sleep(30)"'
    lines = [
        "[[runtime]]",
        f'id = "{runtime_id}"',
        'name = "Helper"',
        "enabled = true",
        f"command = {json.dumps(command)}",
        f"cwd = {json.dumps(cwd)}",
        f'pid_path = ".brigade/tools/runtime/{runtime_id}.pid"',
        f'log_path = ".brigade/tools/runtime/{runtime_id}.log"',
        "timeout = 2",
    ]
    if health_command is not None:
        lines.append(f"health_command = {json.dumps(health_command)}")
    if health_path is not None:
        lines.append(f"health_path = {json.dumps(health_path)}")
    if port is not None:
        lines.append(f"port = {port}")
    config = tmp_path / ".brigade" / "tools" / "runtimes.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines) + "\n")


def _write_policy_config(
    tmp_path,
    *,
    allowed_families=None,
    allowed_effects=None,
    denied_effects=None,
    required_approval_modes=None,
    max_timeout=10,
    allowed_runtimes=None,
    env_bindings=None,
):
    allowed_families = ["script"] if allowed_families is None else allowed_families
    allowed_effects = ["local-read"] if allowed_effects is None else allowed_effects
    denied_effects = [] if denied_effects is None else denied_effects
    required_approval_modes = ["on-request", "always"] if required_approval_modes is None else required_approval_modes
    allowed_runtimes = [] if allowed_runtimes is None else allowed_runtimes
    env_bindings = {} if env_bindings is None else env_bindings
    config = tmp_path / ".brigade" / "tools" / "policy.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "\n".join(
            [
                "allowed_families = " + json.dumps(allowed_families),
                "allowed_effects = " + json.dumps(allowed_effects),
                "denied_effects = " + json.dumps(denied_effects),
                "required_approval_modes = " + json.dumps(required_approval_modes),
                f"max_timeout = {max_timeout}",
                "allowed_runtimes = " + json.dumps(allowed_runtimes),
                "env_bindings = { "
                + ", ".join(f"{key} = {json.dumps(value)}" for key, value in env_bindings.items())
                + " }",
                "",
            ]
        )
    )


def _queue_and_approve_runner(tmp_path, capsys, args='{"path":"README.md"}'):
    assert tools_cmd.call_queue(target=tmp_path, tool_id="runner", args=args, json_output=True) == 0
    call = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_approve(target=tmp_path, call_id=call["id"], json_output=True) == 0
    return json.loads(capsys.readouterr().out)["call"]


def _checkpoint_script(*, fail_on_resume: bool = False) -> str:
    resume_failure = "sys.exit(5)" if fail_on_resume else ""
    return f"""
import json
import os
import sys
from pathlib import Path

checkpoint_dir = Path(os.environ["BRIGADE_TOOL_CHECKPOINT_DIR"])
checkpoint_dir.mkdir(parents=True, exist_ok=True)
if os.environ.get("BRIGADE_TOOL_RESUME_CHECKPOINT_ID"):
    print("resumed choice=" + os.environ.get("BRIGADE_TOOL_RESUME_CHOICE", ""))
    Path("resumed.txt").write_text(os.environ.get("BRIGADE_TOOL_RESUME_CHOICE", ""))
    {resume_failure}
else:
    (checkpoint_dir / "request.json").write_text(json.dumps({{
        "reason": "needs operator review",
        "requested_action": "choose next step",
        "prompt": "Continue with token=prompt-secret?",
        "context": {{"api_token": "argument-secret", "note": "secret=private-value"}},
        "choices": ["continue", "abort"]
    }}))
    print("checkpoint requested")
"""


def _create_waiting_checkpoint(tmp_path, capsys, *, script: str | None = None, args='{"path":"README.md"}'):
    _write_script_tool_config(tmp_path, script=script or _checkpoint_script())
    call = _queue_and_approve_runner(tmp_path, capsys, args=args)
    assert tools_cmd.call_run(target=tmp_path, call_id=call["id"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    checkpoint_id = payload["receipt"]["checkpoint_id"]
    return payload["call"], checkpoint_id, payload["receipt"]


def _write_mcp_tool_config(tmp_path, *, server_script: str, timeout: float = 5.0) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / "fake_mcp.py").write_text(server_script)
    (tools_dir / "mcp-input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "api_token": {"type": "string"}},
                "additionalProperties": True,
            }
        )
    )
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        f"""
[[tool]]
id = "mcp-runner"
name = "MCP Runner"
family = "mcp"
enabled = true
description = "Run local MCP tool."
command = "{sys.executable} tools/fake_mcp.py"
input_schema_path = "tools/mcp-input.schema.json"
timeout = {timeout}
permissions = ["read-files"]
effects = ["local-read"]
approval_mode = "on-request"
runtime_id = "helper"
requires_runtime = true
mcp_server_id = "helper"
mcp_tool_name = "echo"
supported_harnesses = []
"""
    )


def _fake_mcp_server_script(*, malformed: bool = False, sleep_seconds: float = 0.0, copy_env: bool = False) -> str:
    if malformed:
        return 'print("not-json", flush=True)\n'
    env_line = '" env=" + os.environ.get("SAFE_LABEL", "")' if copy_env else '""'
    return f"""
import json
import os
import sys
import time
from pathlib import Path

time.sleep({sleep_seconds})
methods = []
for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    methods.append(request.get("method", ""))
    method = request.get("method")
    if method == "initialize":
        response = {{"jsonrpc": "2.0", "id": request.get("id"), "result": {{"protocolVersion": "2024-11-05", "capabilities": {{}}}}}}
    elif method == "tools/list":
        response = {{"jsonrpc": "2.0", "id": request.get("id"), "result": {{"tools": [{{"name": "echo", "inputSchema": {{"type": "object"}}}}]}}}}
    elif method == "tools/call":
        arguments = request.get("params", {{}}).get("arguments", {{}})
        text = "echo " + str(arguments.get("path", "")) + " api_token=server-secret" + ({env_line})
        response = {{"jsonrpc": "2.0", "id": request.get("id"), "result": {{"content": [{{"type": "text", "text": text}}]}}}}
    else:
        response = {{"jsonrpc": "2.0", "id": request.get("id"), "error": {{"code": -32601, "message": "unknown"}}}}
    print(json.dumps(response), flush=True)
Path("mcp-methods.json").write_text(json.dumps(methods))
"""


def _queue_and_approve_mcp(tmp_path, capsys, args='{"path":"README.md"}'):
    assert tools_cmd.call_queue(target=tmp_path, tool_id="mcp-runner", args=args, json_output=True) == 0
    call = json.loads(capsys.readouterr().out)["call"]
    assert tools_cmd.call_approve(target=tmp_path, call_id=call["id"], json_output=True) == 0
    return json.loads(capsys.readouterr().out)["call"]


def _write_chat_surfaces_config(tmp_path, surfaces):
    lines = []
    for surface in surfaces:
        lines.append("[[surface]]")
        for key, value in surface.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                rendered = json.dumps(value)
            lines.append(f"{key} = {rendered}")
        lines.append("")
    path = tmp_path / ".brigade" / "chat-surfaces.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


def _chat_finding(provider, surface_id, issue_id="issue-1", **extra):
    item = {
        "sweep_id": "nightly-chat",
        "provider": provider,
        "surface_id": surface_id,
        "issue_id": issue_id,
        "issue_type": "task",
        "priority": "high",
        "confidence": "high",
        "safe_summary": "Actionable local chat export finding.",
        "evidence_summary": "Several local export messages refer to the same follow-up.",
        "suggested_task_text": "Review chat export follow-up",
        "acceptance_criteria": ["The follow-up is reviewed without copying raw chat text."],
        "source_fingerprint": f"fp-{surface_id}-{issue_id}",
        "channel_label": "triage",
        "message_range_label": "messages 10-12",
    }
    item.update(extra)
    return item
