"""The paired MCP server exposes exactly five tools and logs every call, including rejected ones."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import paired_mcp
from brigade.model_trial_paired import TOOLS

FIXTURES = Path(__file__).parents[1] / "src/brigade/fixtures/paired-brigade-work"


def _serve(tmp_path, requests, case="passing-verify"):
    target, state = tmp_path / "target", tmp_path / "state"
    target.mkdir()
    state.mkdir()
    env = {
        # Windows resolves a home from USERPROFILE and cannot start Python without SYSTEMROOT, so the
        # server env must carry both, exactly as the live adapter's child env does.
        **{k: os.environ[k] for k in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT") if k in os.environ},
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path(paired_mcp.__file__).resolve().parents[1]),
        "CLAUDE_CODE_OAUTH_TOKEN": "sentinel-token",
        "ANTHROPIC_API_KEY": "sentinel-key",
    }
    argv = [
        sys.executable,
        "-m",
        "brigade",
        "model",
        "trial",
        "paired-mcp",
        "--case",
        case,
        "--target",
        str(target),
        "--package-dir",
        str(FIXTURES / "baseline"),
        "--state-dir",
        str(state),
    ]
    stdin = "".join(json.dumps({"jsonrpc": "2.0", "id": i, **r}) + "\n" for i, r in enumerate(requests, start=1))
    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    replies = [json.loads(line) for line in done.stdout.splitlines()]  # stdout is JSON-RPC only
    return replies, state


def test_server_lists_exactly_the_five_tools_and_logs_a_rejected_canary(tmp_path):
    calls = [
        {"method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"method": "tools/list"},
        {"method": "tools/call", "params": {"name": "brief", "arguments": {}}},
        {"method": "tools/call", "params": {"name": "canary", "arguments": {}}},
        {"method": "tools/call", "params": {"name": "receipt", "arguments": {"path": "/etc/passwd"}}},
    ]
    replies, state = _serve(tmp_path, calls)
    assert [t["name"] for t in replies[1]["result"]["tools"]] == list(TOOLS)
    assert replies[2]["result"]["isError"] is False
    assert replies[3]["result"]["isError"] is True
    assert replies[4]["result"]["isError"] is True
    rows = [json.loads(line) for line in (state / "trace.jsonl").read_text().splitlines()]
    assert [(r["seq"], r["tool"], r["allowlisted"]) for r in rows] == [
        (1, "brief", True),
        (2, "canary", False),
        (3, "receipt", True),
    ]
    assert rows[1]["response"] == {"error": "unsupported tool or arguments"}
    assert rows[2]["response"] == {"error": "unsupported tool or arguments"}
    assert not (state / "receipt.json").exists()


def test_server_enforces_the_sixteen_call_cap_and_writes_the_receipt(tmp_path):
    calls = [{"method": "tools/call", "params": {"name": "verify", "arguments": {}}}]
    calls += [{"method": "tools/call", "params": {"name": "receipt", "arguments": {}}}] * 16
    replies, state = _serve(tmp_path, calls)
    rows = [json.loads(line) for line in (state / "trace.jsonl").read_text().splitlines()]
    assert len(rows) == 17
    assert rows[-1]["response"] == {"error": "unsupported tool or arguments"}
    assert json.loads((state / "receipt.json").read_text())["run_id"] == rows[0]["response"]["run_id"]


def test_scrub_environment_drops_auth_before_boundary_subprocesses(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setenv("UNRELATED_SETTING", "z")
    paired_mcp.scrub_environment()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ and "ANTHROPIC_API_KEY" not in os.environ
    assert os.environ["UNRELATED_SETTING"] == "z"


def test_apparatus_failure_is_a_logged_row_not_a_crash(tmp_path):
    class Exploding:
        receipt_bytes = None

        def call(self, tool, arguments):
            raise RuntimeError("fixture broke")

    server = paired_mcp.PairedServer(Exploding(), tmp_path)  # type: ignore[arg-type]
    payload, failed = server.call_tool("verify", {})
    assert failed and json.loads(payload) == {"error": "apparatus_error:RuntimeError"}
    row = json.loads((tmp_path / "trace.jsonl").read_text())
    assert row["apparatus_error"] is True


@pytest.mark.parametrize("tool", TOOLS)
def test_tool_schemas_take_no_arguments(tool):
    definition = next(t for t in paired_mcp.tool_definitions() if t["name"] == tool)
    assert definition["inputSchema"] == {"type": "object", "properties": {}, "additionalProperties": False}


def test_grade_reasons_carry_the_failed_subprocess_stderr_tail():
    from brigade.model_trial_paired import grade

    crash = {"exit_code": 1, "stdout": "", "stderr": "Traceback\n" + "x" * 500 + "\nFileNotFoundError: git\n"}
    attempt = {
        "failure": None,
        "cell": {"case": "passing-verify"},
        "trace": [{"tool": "brief", "response": crash}, {"tool": "capture", "response": crash}],
        "receipt": None,
        "final": None,
    }
    reasons = grade(attempt)["reasons"]
    assert "unexpected brief exit code (stderr: FileNotFoundError: git)" in reasons
    assert "unexpected capture exit code (stderr: FileNotFoundError: git)" in reasons
