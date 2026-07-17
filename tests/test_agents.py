import json
from pathlib import Path

import pytest

from brigade import agents


def test_build_argv_for_known_clis():
    assert agents.build_argv("claude", "hi") == ["claude", "-p", "hi"]
    assert agents.build_argv("codex", "hi") == ["codex", "exec", "hi"]
    assert agents.build_argv("opencode", "hi") == ["opencode", "run", "hi"]
    assert agents.build_argv("antigravity", "hi") == [
        "agy",
        "--add-dir",
        str(Path.cwd().resolve()),
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]
    assert agents.build_argv("pi", "hi") == ["pi", "-p", "hi"]
    assert agents.build_argv("cursor", "hi") == [
        "cursor-agent",
        "-p",
        "--output-format",
        "text",
        "-f",
        "hi",
    ]
    assert agents.build_argv("aider", "hi") == ["aider", "--yes", "--no-auto-commits", "--message", "hi"]
    assert agents.build_argv("goose", "hi") == ["goose", "run", "--no-session", "-t", "hi"]
    assert agents.build_argv("continue", "hi") == ["cn", "-p", "hi"]
    assert agents.build_argv("copilot", "hi") == ["copilot", "-p", "hi"]
    assert agents.build_argv("qwen", "hi") == ["qwen", "-p", "hi", "--approval-mode", "yolo"]
    assert agents.build_argv("kimi", "hi") == [
        "kimi",
        "--yolo",
        "--print",
        "-p",
        "hi",
        "--final-message-only",
    ]
    assert agents.build_argv("adal", "hi") == ["adal", "-q", "hi"]
    assert agents.build_argv("openhands", "hi") == ["openhands", "--headless", "-t", "hi"]
    assert agents.build_argv("grok", "hi") == ["grok", "-p", "hi", "--always-approve"]
    assert agents.build_argv("amp", "hi") == ["amp", "-x", "hi"]
    assert agents.build_argv("crush", "hi") == ["crush", "run", "hi"]
    assert agents.build_argv("ollama:llama3.3", "hi") == ["ollama", "run", "llama3.3", "hi"]


def test_build_argv_for_read_only_codex():
    assert agents.build_argv("codex", "hi", read_only=True) == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "hi",
    ]
    assert agents.build_argv("claude", "hi", read_only=True) == ["claude", "-p", "hi"]
    assert agents.build_argv("opencode", "hi", read_only=True) == ["opencode", "run", "hi"]
    assert agents.build_argv("antigravity", "hi", read_only=True) == ["agy", "--sandbox", "--print", "hi"]
    assert agents.build_argv("pi", "hi", read_only=True) == ["pi", "--tools", "read,grep,find,ls", "-p", "hi"]
    assert agents.build_argv("cursor", "hi", read_only=True) == [
        "cursor-agent",
        "-p",
        "--mode",
        "plan",
        "--output-format",
        "text",
        "--trust",
        "hi",
    ]
    assert agents.build_argv("aider", "hi", read_only=True) == [
        "aider",
        "--no-auto-commits",
        "--dry-run",
        "--message",
        "hi",
    ]
    assert agents.build_argv("continue", "hi", read_only=True) == ["cn", "-p", "hi", "--readonly"]
    assert agents.build_argv("qwen", "hi", read_only=True) == ["qwen", "-p", "hi", "--approval-mode", "plan"]
    assert agents.build_argv("kimi", "hi", read_only=True) == [
        "kimi",
        "--plan",
        "--print",
        "-p",
        "hi",
        "--final-message-only",
    ]
    assert agents.build_argv("goose", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("copilot", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("adal", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("openhands", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("grok", "hi", read_only=True) == [
        "grok",
        "-p",
        "hi",
        "--permission-mode",
        "plan",
    ]
    assert agents.build_argv("amp", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("crush", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("ollama:llama3.3", "hi", read_only=True) == [
        "ollama",
        "run",
        "llama3.3",
        "hi",
    ]


def test_build_argv_antigravity_writable_uses_cwd_write_approval(tmp_path):
    assert agents.build_argv("antigravity", "hi", cwd=tmp_path) == [
        "agy",
        "--add-dir",
        str(tmp_path),
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]


def test_build_argv_antigravity_writable_preserves_explicit_cwd():
    cwd = Path("workspace")

    assert agents.build_argv("antigravity", "hi", cwd=cwd) == [
        "agy",
        "--add-dir",
        "workspace",
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]


def test_build_argv_antigravity_writable_uses_current_cwd_when_cwd_omitted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert agents.build_argv("antigravity", "hi") == [
        "agy",
        "--add-dir",
        str(tmp_path.resolve()),
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]


def test_research_antigravity_cli_uses_current_cwd_when_cwd_omitted(tmp_path, monkeypatch):
    from brigade.research import llm

    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw["cwd"]
        return agents.proc.Result(0, "answer", "")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)

    assert llm._run_cli("antigravity", "hi", 10) == "answer"
    assert captured["cwd"] is None
    assert captured["argv"] == [
        "agy",
        "--add-dir",
        str(tmp_path.resolve()),
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]


def test_build_argv_antigravity_read_only_keeps_sandbox_without_write_flags(tmp_path):
    assert agents.build_argv("antigravity", "hi", read_only=True, cwd=tmp_path) == [
        "agy",
        "--sandbox",
        "--print",
        "hi",
    ]
    argv = agents.build_argv("antigravity", "hi", sandbox="read-only", cwd=tmp_path)
    assert argv == ["agy", "--sandbox", "--print", "hi"]
    assert "--add-dir" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_build_argv_cursor_sandbox_read_only_uses_plan_mode():
    assert agents.build_argv("cursor", "hi", sandbox="read-only") == [
        "cursor-agent",
        "-p",
        "--mode",
        "plan",
        "--output-format",
        "text",
        "--trust",
        "hi",
    ]


def test_build_argv_kimi_writable_uses_yolo_and_read_only_keeps_plan():
    assert agents.build_argv("kimi", "hi") == [
        "kimi",
        "--yolo",
        "--print",
        "-p",
        "hi",
        "--final-message-only",
    ]
    read_only = agents.build_argv("kimi", "hi", read_only=True)
    assert read_only == [
        "kimi",
        "--plan",
        "--print",
        "-p",
        "hi",
        "--final-message-only",
    ]
    assert "--yolo" not in read_only


def test_build_argv_can_set_codex_sandbox():
    assert agents.build_argv("codex", "hi", sandbox="danger-full-access") == [
        "codex",
        "exec",
        "--sandbox",
        "danger-full-access",
        "hi",
    ]
    assert agents.build_argv("codex", "hi", read_only=True, sandbox="workspace-write") == [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "hi",
    ]


def test_build_argv_unknown_raises():
    with pytest.raises(ValueError):
        agents.build_argv("nope", "hi")


def test_build_argv_pins_model_for_claude_and_codex():
    assert agents.build_argv("claude", "hi", model="claude-fable-5") == [
        "claude",
        "--model",
        "claude-fable-5",
        "-p",
        "hi",
    ]
    assert agents.build_argv("codex", "hi", model="gpt-5.5-codex") == [
        "codex",
        "exec",
        "-m",
        "gpt-5.5-codex",
        "hi",
    ]
    assert agents.build_argv("codex", "hi", read_only=True, model="gpt-5.5-codex") == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "-m",
        "gpt-5.5-codex",
        "hi",
    ]
    assert agents.build_argv("codex", "hi", sandbox="workspace-write", model="gpt-5.5-codex") == [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "-m",
        "gpt-5.5-codex",
        "hi",
    ]


def test_build_argv_without_model_is_unchanged():
    assert agents.build_argv("claude", "hi", model=None) == ["claude", "-p", "hi"]
    assert agents.build_argv("codex", "hi", model=None) == ["codex", "exec", "hi"]


def test_build_argv_model_on_unsupported_cli_raises():
    with pytest.raises(ValueError, match="model"):
        agents.build_argv("goose", "hi", model="anything")


def test_build_argv_model_on_ollama_ref_raises():
    with pytest.raises(ValueError, match="model"):
        agents.build_argv("ollama:llama3.3", "hi", model="mistral")


def test_command_for_returns_binary():
    assert agents.command_for("claude") == "claude"
    assert agents.command_for("codex") == "codex"
    assert agents.command_for("opencode") == "opencode"
    assert agents.command_for("antigravity") == "agy"
    assert agents.command_for("pi") == "pi"
    assert agents.command_for("cursor") == "cursor-agent"
    assert agents.command_for("aider") == "aider"
    assert agents.command_for("goose") == "goose"
    assert agents.command_for("continue") == "cn"
    assert agents.command_for("copilot") == "copilot"
    assert agents.command_for("qwen") == "qwen"
    assert agents.command_for("kimi") == "kimi"
    assert agents.command_for("adal") == "adal"
    assert agents.command_for("openhands") == "openhands"
    assert agents.command_for("grok") == "grok"
    assert agents.command_for("amp") == "amp"
    assert agents.command_for("crush") == "crush"
    assert agents.command_for("ollama:llama3.3") == "ollama"


def test_is_known():
    assert agents.is_known("claude")
    assert agents.is_known("codex")
    assert agents.is_known("opencode")
    assert agents.is_known("antigravity")
    assert agents.is_known("pi")
    assert agents.is_known("cursor")
    assert agents.is_known("aider")
    assert agents.is_known("goose")
    assert agents.is_known("continue")
    assert agents.is_known("copilot")
    assert agents.is_known("qwen")
    assert agents.is_known("kimi")
    assert agents.is_known("adal")
    assert agents.is_known("openhands")
    assert agents.is_known("grok")
    assert agents.is_known("amp")
    assert agents.is_known("crush")
    assert agents.is_known("ollama:anything")
    assert not agents.is_known("bogus")


def test_run_agent_reports_missing(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: None)
    res = agents.run_agent("claude", "hi")
    assert res.ok is False
    assert "not installed" in res.detail


_OLLAMA_LIST_HEADER = "NAME                ID              SIZE      MODIFIED\n"


def _fake_ollama_env(monkeypatch, list_result, run_result=None):
    """Route proc.run so `ollama list` returns list_result and record any other argv."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["ollama", "list"]:
            return list_result
        return run_result if run_result is not None else agents.proc.Result(0, "answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    return calls


def test_run_agent_ollama_refuses_model_not_pulled(monkeypatch):
    # `ollama run` on a missing model silently auto-pulls it (43GB for
    # llama3.3, once enough to fill a root disk); dispatch must refuse instead.
    listing = agents.proc.Result(0, _OLLAMA_LIST_HEADER + "other:latest  abc  2.0 GB  2 days ago\n", "")
    calls = _fake_ollama_env(monkeypatch, listing)
    res = agents.run_agent("ollama:llama3.3", "hi")
    assert res.ok is False
    assert "not pulled locally" in res.detail
    assert "never auto-pulls" in res.detail
    assert calls == [["ollama", "list"]]


def test_run_agent_ollama_runs_when_model_pulled(monkeypatch):
    listing = agents.proc.Result(0, _OLLAMA_LIST_HEADER + "llama3.3:latest  abc  43 GB  2 days ago\n", "")
    calls = _fake_ollama_env(monkeypatch, listing)
    res = agents.run_agent("ollama:llama3.3", "hi")
    assert res.ok is True
    assert res.text == "answer"
    assert calls[-1] == ["ollama", "run", "llama3.3", "hi"]


def test_run_agent_ollama_matches_exact_tag(monkeypatch):
    listing = agents.proc.Result(0, _OLLAMA_LIST_HEADER + "llama3.2:3b  abc  2.0 GB  2 days ago\n", "")
    calls = _fake_ollama_env(monkeypatch, listing)
    res = agents.run_agent("ollama:llama3.2:3b", "hi")
    assert res.ok is True
    assert calls[-1] == ["ollama", "run", "llama3.2:3b", "hi"]


def test_run_agent_ollama_fails_seat_when_list_fails(monkeypatch):
    listing = agents.proc.Result(1, "", "could not connect to ollama server")
    calls = _fake_ollama_env(monkeypatch, listing)
    res = agents.run_agent("ollama:llama3.2:3b", "hi")
    assert res.ok is False
    assert "could not list local ollama models" in res.detail
    assert calls == [["ollama", "list"]]


def test_run_agent_captures_output(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(0, "  answer  ", ""))
    res = agents.run_agent("codex", "do it")
    assert res.ok is True
    assert res.text == "answer"
    assert res.stdout == "  answer  "
    assert res.stderr == ""
    assert res.exit_code == 0
    assert res.timed_out is False


def test_run_agent_rejects_intent_only_antigravity_output(monkeypatch):
    output = "\n".join(
        [
            "I will locate the relevant files in the repository.",
            "I will list the repository contents to understand its structure.",
            "I will run a search for the provider dispatch path.",
            "I will inspect the matching source files next.",
        ]
    )
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, output + "\n", ""))

    result = agents.run_agent("antigravity", "trace it", model="Gemini 3.5 Flash (High)")

    assert result.ok is False
    assert result.text == output
    assert result.exit_code == 0
    assert result.failure_phase == "output-validation"
    assert result.failure_kind == "non-final-output"
    assert result.detail == "provider returned progress or intent without a final result"


def test_run_agent_rejects_bare_progress_only_output(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, "Reviewing repository files.\n", ""),
    )

    result = agents.run_agent("antigravity", "review it")

    assert result.ok is False
    assert result.failure_kind == "non-final-output"


def test_run_agent_rejects_progress_over_changed_files(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, "Reviewing changed files.\n", ""),
    )

    result = agents.run_agent("antigravity", "review it")

    assert result.ok is False
    assert result.failure_kind == "non-final-output"


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {"tool_calls": [{"name": "write_file", "arguments": {"text": "content"}}]},
        {"type": "tool_call", "name": "read_file", "arguments": {"path": "README.md"}},
        {"type": "tool_call", "name": "write_file", "arguments": {"text": "content"}},
        {"name": "read_file", "arguments": {"path": "README.md"}},
        {"name": "write_file", "arguments": {"text": "content"}},
    ],
)
def test_run_agent_rejects_tool_call_only_output(monkeypatch, payload):
    output = json.dumps(payload)
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, output, ""))

    result = agents.run_agent("antigravity", "inspect it")

    assert result.ok is False
    assert result.failure_phase == "output-validation"
    assert result.failure_kind == "tool-only-output"
    assert result.detail == "provider returned tool-call data without a final result"


@pytest.mark.parametrize(
    "output",
    [
        '<tool_use>{"name":"read_file","path":"README.md"}</tool_use>',
        '<tool_use name="read_file">{"path":"README.md"}</tool_use>',
        '<tool_call name="read_file"/><function_call>{"name":"inspect"}</function_call>',
    ],
)
def test_run_agent_rejects_tool_use_markup_without_final_text(monkeypatch, output):
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, output, ""))

    result = agents.run_agent("antigravity", "inspect it")

    assert result.ok is False
    assert result.failure_kind == "tool-only-output"


def test_run_agent_rejects_tool_call_and_tool_result_transcript(monkeypatch):
    output = json.dumps(
        {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}],
                },
                {"role": "tool", "type": "tool_result", "content": "file contents"},
            ]
        }
    )
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, output, ""))

    result = agents.run_agent("antigravity", "inspect it")

    assert result.ok is False
    assert result.failure_kind == "tool-only-output"


@pytest.mark.parametrize(
    ("output", "failure_kind"),
    [
        ("Error: authentication required. Run provider login.", "authentication-error"),
        ("Error: failed to connect to the model provider.", "network-error"),
        ("Error: model gemini-example is not available.", "provider-setting-error"),
        ("Error: rate limit exceeded for this provider.", "rate-limit-error"),
    ],
)
def test_run_agent_rejects_in_band_operational_diagnostics(monkeypatch, output, failure_kind):
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, output, ""))

    result = agents.run_agent("antigravity", "answer directly")

    assert result.ok is False
    assert result.failure_phase == "output-validation"
    assert result.failure_kind == failure_kind
    assert result.detail.startswith("provider returned an operational error instead of a final result:")


@pytest.mark.parametrize(
    "output",
    [
        "No findings.",
        "OK",
        "I will inspect the repository first. No findings.",
        "Running the targeted tests passed.",
        "Checking the implementation, I do not see any regressions.",
        "```text\nError: NonRetriableError: Provider Error\n```\nThis is the requested fixture.",
    ],
)
def test_run_agent_accepts_short_or_quoted_substantive_output(monkeypatch, output):
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, output, ""))

    result = agents.run_agent("antigravity", "answer directly")

    assert result.ok is True
    assert result.text == output


def test_run_agent_forwards_model_to_argv(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return agents.proc.Result(0, "answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    res = agents.run_agent("claude", "hi", model="claude-fable-5")
    assert res.ok is True
    assert captured["argv"] == ["claude", "--model", "claude-fable-5", "-p", "hi"]


@pytest.mark.parametrize(
    ("cli_ref", "expected"),
    [
        ("codex", ["codex", "exec", "-c", 'model_reasoning_effort="xhigh"', "hi"]),
        ("opencode", ["opencode", "run", "--variant", "xhigh", "hi"]),
        ("pi", ["pi", "--thinking", "xhigh", "-p", "hi"]),
        ("grok", ["grok", "--reasoning-effort", "xhigh", "-p", "hi", "--always-approve"]),
    ],
)
def test_build_argv_applies_reasoning(cli_ref, expected):
    assert agents.build_argv(cli_ref, "hi", reasoning="xhigh") == expected


def test_build_argv_rejects_reasoning_for_unsupported_adapter():
    with pytest.raises(ValueError, match="does not support reasoning"):
        agents.build_argv("claude", "hi", reasoning="high")


def test_run_agent_threads_cwd_into_argv_builder(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw["cwd"]
        return agents.proc.Result(0, "answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    res = agents.run_agent("antigravity", "hi", cwd=tmp_path)
    assert res.ok is True
    assert captured["cwd"] == tmp_path
    assert captured["argv"] == [
        "agy",
        "--add-dir",
        str(tmp_path),
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]


def test_run_agent_antigravity_with_no_cwd_allows_current_cwd(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw["cwd"]
        return agents.proc.Result(0, "answer", "")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    res = agents.run_agent("antigravity", "hi")

    assert res.ok is True
    assert captured["cwd"] is None
    assert captured["argv"] == [
        "agy",
        "--add-dir",
        str(tmp_path.resolve()),
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]


def test_run_agent_nonzero_is_not_ok(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(1, "", "boom"))
    res = agents.run_agent("claude", "x")
    assert res.ok is False
    assert "boom" in res.detail
    assert res.exit_code == 1
    assert res.stderr == "boom"


@pytest.mark.parametrize("cli_ref", ["cursor", "grok"])
def test_run_agent_classifies_silent_adapter_exit(monkeypatch, cli_ref):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(0, "", ""))

    result = agents.run_agent(cli_ref, "do it")

    assert result.ok is False
    assert result.detail == f"{cli_ref} exited 0 without output; check trust, permissions, and model availability"
    assert result.exit_code == 0


def _grok_json_output(answer: str, *, structured: bool = True, stop_reason: str = "EndTurn") -> str:
    result = {"kind": "answer", "answer": answer}
    return json.dumps(
        {
            "text": json.dumps(result),
            "stopReason": stop_reason,
            "sessionId": "019f0000-0000-7000-8000-000000000001",
            "requestId": "00000000-0000-4000-8000-000000000001",
            "structuredOutput": result if structured else None,
            "structuredOutputError": None if structured else "model did not produce structured output",
        }
    )


def test_run_agent_rejects_grok_progress_without_structured_final_output(monkeypatch):
    output = (
        "Reviewing the README.md and tools/brigade.md diffs against Brigade 0.22.0. "
        "Gathering the git diffs and current file content first."
    )
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, output + "\n", ""))

    result = agents.run_agent("grok", "review it", read_only=True, model="grok-4.5")

    assert result.ok is False
    assert result.detail == "grok exited 0 without a structured final response"
    assert result.failure_phase == "output-validation"
    assert result.failure_kind == "malformed-final-output"
    assert result.text == output
    assert result.stdout == output + "\n"
    assert result.exit_code == 0


def test_run_agent_rejects_grok_json_without_structured_final_output(monkeypatch):
    output = "Reviewing the diff and gathering the relevant files first."
    stdout = _grok_json_output(output, structured=False, stop_reason="Cancelled")
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, stdout + "\n", ""))

    result = agents.run_agent("grok", "review it", read_only=True, model="grok-4.5")

    assert result.ok is False
    assert result.detail == (
        "grok exited 0 without a structured final response "
        "(stopReason=Cancelled; model did not produce structured output)"
    )
    assert result.text == output
    assert result.stdout == stdout + "\n"


@pytest.mark.parametrize("case", ["cancelled", "extra-property", "structured-error"])
def test_run_agent_rejects_invalid_grok_structured_final_output(monkeypatch, case):
    payload = json.loads(_grok_json_output("No actionable findings."))
    if case == "cancelled":
        payload["stopReason"] = "Cancelled"
    elif case == "extra-property":
        payload["structuredOutput"]["extra"] = True
    else:
        payload["structuredOutputError"] = "model reported an invalid structured result"
    stdout = json.dumps(payload)
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, stdout + "\n", ""))

    result = agents.run_agent("grok", "review it", read_only=True, model="grok-4.5")

    assert result.ok is False
    assert "grok exited 0 without a structured final response" in result.detail
    assert result.text == "No actionable findings."
    assert result.stdout == stdout + "\n"


@pytest.mark.parametrize("error_value", ["", False, 0, {}])
def test_run_agent_rejects_falsey_present_grok_structured_error(monkeypatch, error_value):
    payload = json.loads(_grok_json_output("No actionable findings."))
    payload["structuredOutputError"] = error_value
    stdout = json.dumps(payload)
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, stdout + "\n", ""))

    result = agents.run_agent("grok", "review it", read_only=True, model="grok-4.5")

    assert result.ok is False
    assert "structuredOutputError was present" in result.detail
    assert result.text == "No actionable findings."
    assert result.stdout == stdout + "\n"
    assert result.exit_code == 0


def test_run_agent_accepts_structured_grok_finding_with_progress_opening(monkeypatch):
    output = "Reviewing the diff: missing bounds check in foo."
    stdout = _grok_json_output(output)
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, stdout + "\n", ""))

    result = agents.run_agent("grok", "review it", read_only=True, model="grok-4.5")

    assert result.ok is True
    assert result.text == output
    assert result.stdout == stdout + "\n"


def test_run_agent_accepts_concise_grok_no_findings(monkeypatch):
    output = _grok_json_output("No actionable findings.")
    seen = {}
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return agents.proc.Result(0, output + "\n", "")

    monkeypatch.setattr(agents.proc, "run", fake_run)

    result = agents.run_agent("grok", "review it", read_only=True, model="grok-4.5")

    assert result.ok is True
    assert result.text == "No actionable findings."
    assert result.exit_code == 0
    assert seen["argv"][-2:] == [
        "--json-schema",
        (
            '{"type":"object","properties":{"kind":{"type":"string","enum":["answer"]},'
            '"answer":{"type":"string","minLength":1}},"required":["kind","answer"],'
            '"additionalProperties":false}'
        ),
    ]
    assert "--permission-mode" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("--sandbox") :] == [
        "--sandbox",
        "read-only",
        "--always-approve",
        "--json-schema",
        seen["argv"][-1],
    ]


def test_run_agent_keeps_permission_mode_prompt_separate_from_grok_flags(monkeypatch):
    output = _grok_json_output("No actionable findings.")
    seen = {}
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return agents.proc.Result(0, output + "\n", "")

    monkeypatch.setattr(agents.proc, "run", fake_run)

    result = agents.run_agent("grok", "--permission-mode", read_only=True, model="grok-4.5")

    assert result.ok is True
    assert result.text == "No actionable findings."
    assert seen["argv"][seen["argv"].index("-p") + 1] == "--permission-mode"
    assert seen["argv"].count("--permission-mode") == 1
    assert seen["argv"][seen["argv"].index("--sandbox") : seen["argv"].index("--json-schema")] == [
        "--sandbox",
        "read-only",
        "--always-approve",
    ]


def test_run_agent_keeps_writable_grok_plain_output(monkeypatch):
    output = "Implemented the requested change."
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: agents.proc.Result(0, output + "\n", ""))

    result = agents.run_agent("grok", "implement it", read_only=False, model="grok-4.5")

    assert result.ok is True
    assert result.text == output


def test_run_agent_reports_invalid_internal_grok_read_only_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents, "build_argv", lambda *args, **kwargs: ["grok", "-p", "review it"])
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kwargs: calls.append(argv))

    result = agents.run_agent("grok", "review it", read_only=True, model="grok-4.5")

    assert result.ok is False
    assert result.detail == "internal error: grok read-only argv missing --permission-mode plan"
    assert result.requested_model == "grok-4.5"
    assert calls == []


def test_run_agent_rejects_direct_read_only_cursor_composer_before_spawn(monkeypatch):
    calls = []
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: calls.append(argv))

    result = agents.run_agent("cursor", "inspect", read_only=True, model="composer-2.5-fast")

    assert result.ok is False
    assert result.exit_code is None
    assert result.requested_model == "composer-2.5-fast"
    assert "direct Cursor plan mode does not return Composer findings" in result.detail
    assert 'transport = "acpx"' in result.detail
    assert calls == []


def test_run_agent_classifies_grok_cursor_empty_output_with_process_evidence(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(0, "\n", "provider note"))

    result = agents.run_agent("cursor", "inspect", read_only=True, model="grok-4.5-xhigh")

    assert result.ok is False
    assert result.exit_code == 0
    assert result.stdout == "\n"
    assert result.stderr == "provider note"
    assert result.requested_model == "grok-4.5-xhigh"
    assert "direct Cursor plan mode returned no assistant text" in result.detail
    assert 'transport = "acpx"' in result.detail


class _StubThread:
    def __init__(self, result):
        self._result = result
        self.prompts = []
        self.efforts = []

    def run_turn(self, prompt, *, timeout, on_event=None, effort=None):
        self.prompts.append((prompt, timeout))
        self.efforts.append(effort)
        return self._result


class _StubServer:
    def __init__(self, result, fail=False):
        self.thread = _StubThread(result)
        self.calls = []
        self.fail = fail

    def start_thread(self, *, cwd, model=None, sandbox=None):
        if self.fail:
            from brigade import codex_appserver

            raise codex_appserver.AppServerError("boom")
        self.calls.append({"cwd": cwd, "model": model, "sandbox": sandbox})
        return self.thread


def test_run_codex_appserver_maps_turn_result():
    from brigade import codex_appserver

    turn = codex_appserver.TurnResult(text=" hi ", ok=True, status="complete", thread_id="t-1")
    server = _StubServer(turn)
    res = agents.run_codex_appserver(server, "do it", timeout=5.0, cwd=None, model="gpt-5.1", sandbox="workspace-write")
    assert res.ok and res.text == "hi"
    assert res.thread_id == "t-1" and res.status == "complete"
    assert server.calls == [{"cwd": None, "model": "gpt-5.1", "sandbox": "workspace-write"}]


def test_run_codex_appserver_read_only_maps_to_sandbox():
    from brigade import codex_appserver

    turn = codex_appserver.TurnResult(text="x", ok=True, status="complete", thread_id="t-1")
    server = _StubServer(turn)
    agents.run_codex_appserver(server, "p", timeout=5.0, cwd=None, read_only=True)
    assert server.calls[0]["sandbox"] == "read-only"


def test_run_codex_appserver_passes_reasoning_to_turn():
    from brigade import codex_appserver

    turn = codex_appserver.TurnResult(text="x", ok=True, status="complete", thread_id="t-1")
    server = _StubServer(turn)
    agents.run_codex_appserver(server, "p", timeout=5.0, cwd=None, reasoning="xhigh")
    assert server.thread.efforts == ["xhigh"]


def test_run_codex_appserver_empty_text_not_ok():
    from brigade import codex_appserver

    turn = codex_appserver.TurnResult(text="", ok=True, status="complete", thread_id="t-1")
    server = _StubServer(turn)
    res = agents.run_codex_appserver(server, "p", timeout=5.0, cwd=None)
    assert not res.ok and res.detail == "empty output"


def test_run_codex_appserver_rejects_intent_only_final():
    from brigade import codex_appserver

    turn = codex_appserver.TurnResult(
        text="I will inspect the repository first. I will report the result next.",
        ok=True,
        status="complete",
        thread_id="t-1",
    )
    server = _StubServer(turn)

    result = agents.run_codex_appserver(server, "p", timeout=5.0, cwd=None)

    assert result.ok is False
    assert result.failure_phase == "output-validation"
    assert result.failure_kind == "non-final-output"


def test_run_codex_appserver_server_error_is_failed():
    server = _StubServer(None, fail=True)
    res = agents.run_codex_appserver(server, "p", timeout=5.0, cwd=None)
    assert not res.ok and res.status == "failed" and "boom" in res.detail


def test_agent_result_defaults_keep_exec_contract():
    res = agents.AgentResult(text="t", ok=True)
    assert res.thread_id is None and res.status == ""
