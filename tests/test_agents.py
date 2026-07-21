import json
import re
from pathlib import Path

import pytest

from brigade import agents


def test_build_argv_for_known_clis():
    assert agents.build_argv("claude", "hi") == ["claude", "-p", "hi"]
    assert agents.build_argv("codex", "hi") == ["codex", "exec", "-"]
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
    assert agents.build_argv("kimi", "hi") == ["kimi", "-p", "hi"]
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
        "-",
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
    kimi_read_only = agents.build_argv("kimi", "hi", read_only=True)
    assert kimi_read_only[:2] == ["kimi", "-p"]
    assert kimi_read_only[-1].startswith("Read-only planning run.")
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
        "/x/agy",
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


def test_build_argv_kimi_prompt_mode_uses_soft_read_only_instruction():
    assert agents.build_argv("kimi", "hi") == ["kimi", "-p", "hi"]
    read_only = agents.build_argv("kimi", "hi", read_only=True)
    assert read_only[:2] == ["kimi", "-p"]
    assert read_only[-1].startswith("Read-only planning run.")
    assert "--yolo" not in read_only


def test_build_argv_can_set_codex_sandbox():
    assert agents.build_argv("codex", "hi", sandbox="danger-full-access") == [
        "codex",
        "exec",
        "--sandbox",
        "danger-full-access",
        "-",
    ]
    assert agents.build_argv("codex", "hi", read_only=True, sandbox="workspace-write") == [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "-",
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
        "-",
    ]
    assert agents.build_argv("codex", "hi", read_only=True, model="gpt-5.5-codex") == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "-m",
        "gpt-5.5-codex",
        "-",
    ]
    assert agents.build_argv("codex", "hi", sandbox="workspace-write", model="gpt-5.5-codex") == [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "-m",
        "gpt-5.5-codex",
        "-",
    ]


def test_build_argv_without_model_is_unchanged():
    assert agents.build_argv("claude", "hi", model=None) == ["claude", "-p", "hi"]
    assert agents.build_argv("codex", "hi", model=None) == ["codex", "exec", "-"]


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
        if len(argv) >= 2 and Path(argv[0]).name == "ollama" and argv[1] == "list":
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
    assert calls == [["/x/ollama", "list"]]


def test_run_agent_ollama_runs_when_model_pulled(monkeypatch):
    listing = agents.proc.Result(0, _OLLAMA_LIST_HEADER + "llama3.3:latest  abc  43 GB  2 days ago\n", "")
    calls = _fake_ollama_env(monkeypatch, listing)
    res = agents.run_agent("ollama:llama3.3", "hi")
    assert res.ok is True
    assert res.text == "answer"
    assert calls[-1] == ["/x/ollama", "run", "llama3.3", "hi"]


def test_run_agent_ollama_matches_exact_tag(monkeypatch):
    listing = agents.proc.Result(0, _OLLAMA_LIST_HEADER + "llama3.2:3b  abc  2.0 GB  2 days ago\n", "")
    calls = _fake_ollama_env(monkeypatch, listing)
    res = agents.run_agent("ollama:llama3.2:3b", "hi")
    assert res.ok is True
    assert calls[-1] == ["/x/ollama", "run", "llama3.2:3b", "hi"]


def test_run_agent_ollama_fails_seat_when_list_fails(monkeypatch):
    listing = agents.proc.Result(1, "", "could not connect to ollama server")
    calls = _fake_ollama_env(monkeypatch, listing)
    res = agents.run_agent("ollama:llama3.2:3b", "hi")
    assert res.ok is False
    assert "could not list local ollama models" in res.detail
    assert calls == [["/x/ollama", "list"]]


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


def test_run_agent_rejects_decode_failure_even_when_exit_zero(monkeypatch):
    decode_error = "child stderr is not valid UTF-8 (utf-8): 'utf-8' codec can't decode byte 0x9d in position 0"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            0,
            "final answer\n",
            decode_error,
            stderr_decode_error=decode_error,
        ),
    )

    result = agents.run_agent("codex", "do it")

    assert result.ok is False
    assert result.text == "final answer"
    assert result.exit_code == 0
    assert result.failure_phase == "harness"
    assert result.failure_kind == "decode-failure"
    assert decode_error in result.detail


def test_run_agent_rejects_structured_grok_decode_failure_before_parsing(monkeypatch):
    decode_error = "child stdout is not valid UTF-8 (utf-8): 'utf-8' codec can't decode byte 0x9d in position 7"
    partial_stdout = "prefix\n\ufffd"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(
            0,
            partial_stdout,
            decode_error,
            stdout_decode_error=decode_error,
        ),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("_parse_grok_final_output must not run when decode_failed")

    monkeypatch.setattr(agents, "_parse_grok_final_output", fail_if_called)

    result = agents.run_agent("grok", "review it", read_only=True, model="grok-4.5")

    assert result.ok is False
    assert result.text == partial_stdout.strip()
    assert result.exit_code == 0
    assert result.failure_phase == "harness"
    assert result.failure_kind == "decode-failure"
    assert decode_error in result.detail


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


@pytest.mark.parametrize(
    "output",
    [
        "Reviewing repository files.",
        "First, I will inspect the repo.",
        "Now I will run the tests.",
        "I'm going to inspect the files first.",
        "I am inspecting the files first.",
    ],
)
def test_run_agent_rejects_bare_progress_only_output(monkeypatch, output):
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(0, output + "\n", ""),
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
        {"type": "function_call_output", "call_id": "call-1", "output": "file contents"},
        {"type": "tool_result", "content": "file contents"},
        {"call_id": "call-1", "output": "file contents"},
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
                    "content": "I will inspect the repository first.",
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


@pytest.mark.parametrize("result_type", ["function_call_output", "tool_call_output"])
def test_run_agent_rejects_call_output_without_final_text(monkeypatch, result_type):
    output = json.dumps(
        {
            "items": [
                {"type": "function_call", "name": "read_file", "arguments": "{}"},
                {"type": result_type, "call_id": "call-1", "output": "file contents"},
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
    assert captured["argv"] == ["/x/claude", "--model", "claude-fable-5", "-p", "hi"]


def test_run_agent_codex_feeds_prompt_on_stdin(monkeypatch):
    """codex exec must take the prompt on stdin (`-`), not as a trailing argv token.

    Codex 0.144+ treats a non-TTY open stdin as optional append input and can
    hang on 'Reading additional input from stdin...' when the prompt is only
    passed as an argument.
    """
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["stdin"] = kw.get("stdin")
        return agents.proc.Result(0, "answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    res = agents.run_agent("codex", "plan this task", model="gpt-5.5", read_only=True)
    assert res.ok is True
    assert captured["argv"] == [
        "/x/codex",
        "exec",
        "--sandbox",
        "read-only",
        "-m",
        "gpt-5.5",
        "-",
    ]
    assert captured["stdin"] == b"plan this task"


def test_run_agent_maps_codex_stdin_hang_banner(monkeypatch):
    def fake_run(argv, **kw):
        return agents.proc.Result(
            124,
            "",
            "Reading additional input from stdin...\nOpenAI Codex v0.144.5\n--------\n",
        )

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    res = agents.run_agent("codex", "hi")
    assert res.ok is False
    assert "Reading additional input from stdin" not in res.detail
    assert "stdin" in res.detail.lower()
    assert "codex" in res.detail.lower()


@pytest.mark.parametrize(
    ("cli_ref", "expected"),
    [
        ("codex", ["codex", "exec", "-c", 'model_reasoning_effort="xhigh"', "-"]),
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
        "/x/agy",
        "--add-dir",
        str(tmp_path),
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]


def test_ollama_model_present_threads_process_registry(monkeypatch):
    registry = agents.proc.ProcessRegistry()
    seen = {}

    def fake_run(argv, timeout=30.0, process_registry=None):
        seen.update(argv=argv, timeout=timeout, process_registry=process_registry)
        return agents.proc.Result(0, "NAME ID SIZE MODIFIED\nllama3.3:latest id size now\n", "")

    executable = agents.proc.ExecutableIdentity(
        command="ollama", path="/x/ollama", kind="native", runnable=True, detail="test"
    )
    monkeypatch.setattr(agents.proc, "run", fake_run)

    present, detail = agents.ollama_model_present("llama3.3", executable, process_registry=registry)

    assert present
    assert detail == ""
    assert seen["process_registry"] is registry


def test_ollama_model_present_preserves_legacy_proc_call_shape(monkeypatch):
    def fake_run(argv, timeout=30.0):
        return agents.proc.Result(0, "NAME ID SIZE MODIFIED\nllama3.3:latest id size now\n", "")

    executable = agents.proc.ExecutableIdentity(
        command="ollama", path="/x/ollama", kind="native", runnable=True, detail="test"
    )
    monkeypatch.setattr(agents.proc, "run", fake_run)

    assert agents.ollama_model_present("llama3.3", executable) == (True, "")


def test_run_agent_threads_process_registry_to_ollama_preflight(monkeypatch):
    registry = agents.proc.ProcessRegistry()
    seen = {}

    def fake_present(model, executable=None, process_registry=None):
        seen["process_registry"] = process_registry
        return False, "stop before dispatch"

    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents, "ollama_model_present", fake_present)

    result = agents.run_agent("ollama:llama3.3", "fix it", process_registry=registry)

    assert not result.ok
    assert seen["process_registry"] is registry


def test_run_agent_preserves_legacy_ollama_preflight_call_shape(monkeypatch):
    def fake_present(model, executable=None):
        return False, "stop before dispatch"

    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)
    monkeypatch.setattr(agents, "ollama_model_present", fake_present)

    assert not agents.run_agent("ollama:llama3.3", "fix it").ok


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
        "/x/agy",
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
    assert result.session_id == "019f0000-0000-7000-8000-000000000001"
    assert result.request_id == "00000000-0000-4000-8000-000000000001"
    assert result.stop_reason == "Cancelled"


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
    assert result.session_id == "019f0000-0000-7000-8000-000000000001"
    assert result.request_id == "00000000-0000-4000-8000-000000000001"
    assert result.stop_reason == "EndTurn"
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


def test_run_agent_resumes_exact_grok_session_with_original_settings(monkeypatch, tmp_path):
    output = _grok_json_output("Recovered final answer.")
    seen = {}
    session_id = "019f0000-0000-7000-8000-000000000001"
    monkeypatch.setattr(agents.proc, "which", lambda command: "/x/" + command)

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return agents.proc.Result(0, output + "\n", "")

    monkeypatch.setattr(agents.proc, "run", fake_run)

    result = agents.run_agent(
        "grok",
        "Return the final answer now.",
        timeout=47,
        cwd=tmp_path,
        read_only=True,
        sandbox="read-only",
        model="grok-4.5",
        reasoning="high",
        resume_session_id=session_id,
    )

    assert result.ok is True
    assert seen["argv"].count("--resume") == 1
    assert seen["argv"][seen["argv"].index("--resume") + 1] == session_id
    assert seen["argv"][seen["argv"].index("-p") + 1] == "Return the final answer now."
    assert seen["argv"][seen["argv"].index("-m") + 1] == "grok-4.5"
    assert seen["argv"][seen["argv"].index("--reasoning-effort") + 1] == "high"
    assert seen["kwargs"]["timeout"] == 47
    assert seen["kwargs"]["cwd"] == tmp_path


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


def test_run_agent_env_overrides_child_environment(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        return agents.proc.Result(0, "answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    monkeypatch.setenv("PRE_EXISTING", "kept")

    result = agents.run_agent(
        "claude",
        "hi",
        env={"ANTHROPIC_BASE_URL": "https://api.example.com/anthropic"},
    )
    assert result.ok
    assert captured["env"] is not None
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
    assert captured["env"]["PRE_EXISTING"] == "kept"


def test_run_agent_env_ref_resolves_from_parent(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        return agents.proc.Result(0, "answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    monkeypatch.setenv("KIMI_API_KEY", "sk-resolved-value")

    result = agents.run_agent("claude", "hi", env={"ANTHROPIC_AUTH_TOKEN_REF": "KIMI_API_KEY"})
    assert result.ok
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-resolved-value"
    assert "ANTHROPIC_AUTH_TOKEN_REF" not in captured["env"]


def test_run_agent_scrubs_resolved_env_values_from_success_output(monkeypatch):
    token = "lane-token-value-for-test"
    endpoint = "https://lane.example.test/anthropic"

    def fake_run(argv, **kw):
        return agents.proc.Result(
            0,
            f"answer used {token} at {endpoint}\n",
            f"debug auth={token}\n",
        )

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    monkeypatch.setenv("LANE_KEY", token)

    result = agents.run_agent(
        "claude",
        "hi",
        env={"ANTHROPIC_BASE_URL": endpoint, "ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY"},
    )

    assert result.ok
    assert token not in result.text
    assert token not in result.stdout
    assert token not in result.stderr
    # ANTHROPIC_BASE_URL is plain roster config, not a *_REF secret: it stays
    # readable in output (#323).
    assert result.text == f"answer used [ANTHROPIC_AUTH_TOKEN] at {endpoint}"
    assert result.stderr == "debug auth=[ANTHROPIC_AUTH_TOKEN]\n"


def test_run_agent_scrubs_resolved_env_value_from_failure_detail(monkeypatch):
    token = "lane-token-value-for-test"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(1, f"request used {token}\n", f"401 bearer {token}\n"),
    )
    monkeypatch.setenv("LANE_KEY", token)

    result = agents.run_agent("claude", "hi", env={"ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY"})

    assert not result.ok
    assert token not in result.text
    assert token not in result.detail
    assert token not in result.stdout
    assert token not in result.stderr
    assert result.detail == "401 bearer [ANTHROPIC_AUTH_TOKEN]"


def test_run_agent_scrubs_longer_overlapping_env_value_first(monkeypatch):
    secret = "https://token.example.test/v1-secret"
    fragment = "token.example.test/v1"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"connected to {secret}\n", ""),
    )
    monkeypatch.setenv("LANE_SECRET", secret)
    monkeypatch.setenv("LANE_FRAGMENT", fragment)

    result = agents.run_agent("claude", "hi", env={"ENDPOINT_REF": "LANE_SECRET", "HOST_FRAGMENT_REF": "LANE_FRAGMENT"})

    assert result.ok
    assert result.text == "connected to [ENDPOINT]"


def test_run_agent_scrubs_equal_env_values_with_stable_target(monkeypatch):
    shared = "shared-override-value"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"configured {shared}\n", ""),
    )
    monkeypatch.setenv("LANE_SHARED", shared)

    result = agents.run_agent("claude", "hi", env={"Z_MODE_REF": "LANE_SHARED", "A_MODE_REF": "LANE_SHARED"})

    assert result.ok
    assert result.text == "configured [A_MODE]"


def test_run_agent_never_scrubs_short_plain_flag_values(monkeypatch):
    """Regression for #323: a proxy seat with a '1'-valued flag corrupted plan JSON."""
    plan = '{"assignments":[{"stage":1,"worker":"coder"},{"stage":2,"worker":"reviewer"}]}'
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, plan + "\n", ""),
    )
    monkeypatch.setenv("CLIPROXY_API_KEY", "proxy-secret-value-long-enough")

    result = agents.run_agent(
        "claude",
        "plan it",
        env={
            "ANTHROPIC_BASE_URL": "https://proxy.local.test/anthropic",
            "ANTHROPIC_AUTH_TOKEN_REF": "CLIPROXY_API_KEY",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
    )

    assert result.ok
    assert result.text == plan


def test_run_agent_skips_short_secret_values(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, "stage 1 of 3 done\n", ""),
    )
    monkeypatch.setenv("LANE_SHORT", "1")

    result = agents.run_agent("claude", "hi", env={"LANE_MODE_REF": "LANE_SHORT"})

    assert result.ok
    assert result.text == "stage 1 of 3 done"


def test_run_agent_scrubs_alnum_secret_only_on_word_boundaries(monkeypatch):
    secret = "abcd1234efgh"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"token {secret} inside receiptabcd1234efghtail\n", ""),
    )
    monkeypatch.setenv("LANE_ALNUM", secret)

    result = agents.run_agent("claude", "hi", env={"LANE_TOKEN_REF": "LANE_ALNUM"})

    assert result.ok
    assert result.text == "token [LANE_TOKEN] inside receiptabcd1234efghtail"


def test_run_agent_skips_alnum_secret_embedded_in_unicode_identifier(monkeypatch):
    secret = "abcd1234efgh"
    embedded = f"é{secret}終"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"token {embedded}\n", ""),
    )
    monkeypatch.setenv("LANE_ALNUM", secret)

    result = agents.run_agent("claude", "hi", env={"LANE_TOKEN_REF": "LANE_ALNUM"})

    assert result.ok
    assert result.text == f"token {embedded}"


def test_scrub_secret_boundary_treats_left_combining_mark_as_identifier_continuation():
    secret = "abcd1234efgh"
    overrides = {"LANE_TOKEN": secret}
    targets = {"LANE_TOKEN"}
    # NFD é (e + combining acute) before the secret: \w-only boundaries treat U+0301 as a
    # delimiter and would scrub the secret inside this decomposed identifier.
    left_embedded = f"token e\u0301{secret} done"
    old_pattern = re.compile(rf"(?<!\w){re.escape(secret)}(?!\w)")
    assert old_pattern.search(left_embedded) is not None

    scrubbed = agents._scrub_env_override_values(left_embedded, overrides, targets)
    assert scrubbed == left_embedded
    assert secret in scrubbed

    standalone = f"token {secret} done"
    assert agents._scrub_env_override_values(standalone, overrides, targets) == "token [LANE_TOKEN] done"


def test_scrub_secret_boundary_treats_right_combining_mark_as_identifier_continuation():
    secret = "abcd1234efgh"
    overrides = {"LANE_TOKEN": secret}
    targets = {"LANE_TOKEN"}
    # Combining acute on the secret's trailing character continues the identifier; \w-only
    # boundaries treat U+0301 as a delimiter and would scrub the secret here.
    right_embedded = f"token {secret}\u0301x done"
    old_pattern = re.compile(rf"(?<!\w){re.escape(secret)}(?!\w)")
    assert old_pattern.search(right_embedded) is not None

    scrubbed = agents._scrub_env_override_values(right_embedded, overrides, targets)
    assert scrubbed == right_embedded
    assert secret in scrubbed

    standalone = f"token {secret} done"
    assert agents._scrub_env_override_values(standalone, overrides, targets) == "token [LANE_TOKEN] done"


def test_run_agent_skips_alnum_secret_embedded_with_decomposed_left_combining_mark(monkeypatch):
    secret = "abcd1234efgh"
    embedded = f"e\u0301{secret}"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"token {embedded}\n", ""),
    )
    monkeypatch.setenv("LANE_ALNUM", secret)

    result = agents.run_agent("claude", "hi", env={"LANE_TOKEN_REF": "LANE_ALNUM"})

    assert result.ok
    assert result.text == f"token {embedded}"


def test_run_agent_skips_alnum_secret_embedded_with_decomposed_right_combining_mark(monkeypatch):
    secret = "abcd1234efgh"
    embedded = f"{secret}\u0301x"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"token {embedded}\n", ""),
    )
    monkeypatch.setenv("LANE_ALNUM", secret)

    result = agents.run_agent("claude", "hi", env={"LANE_TOKEN_REF": "LANE_ALNUM"})

    assert result.ok
    assert result.text == f"token {embedded}"


def test_run_agent_scrubs_alnum_secret_delimited_by_unicode_punctuation(monkeypatch):
    secret = "abcd1234efgh"
    delimited = f"《{secret}》"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"token {delimited}\n", ""),
    )
    monkeypatch.setenv("LANE_ALNUM", secret)

    result = agents.run_agent("claude", "hi", env={"LANE_TOKEN_REF": "LANE_ALNUM"})

    assert result.ok
    assert result.text == "token 《[LANE_TOKEN]》"


@pytest.mark.parametrize(
    ("secret", "embedded", "standalone"),
    [
        ("alpha-beta-gamma", "prefixalpha-beta-gammasuffix", "token alpha-beta-gamma done"),
        ("alpha_beta_gamma", "prefixalpha_beta_gammasuffix", "token alpha_beta_gamma done"),
        ("alpha.beta.gamma", "prefixalpha.beta.gammasuffix", "token alpha.beta.gamma done"),
        ("alpha/beta/gamma", "prefixalpha/beta/gammasuffix", "token alpha/beta/gamma done"),
    ],
)
def test_run_agent_scrubs_secret_only_on_identifier_edges(monkeypatch, secret, embedded, standalone):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"{standalone} inside {embedded}\n", ""),
    )
    monkeypatch.setenv("LANE_SECRET", secret)

    result = agents.run_agent("claude", "hi", env={"LANE_TOKEN_REF": "LANE_SECRET"})

    assert result.ok
    assert result.text == f"token [LANE_TOKEN] done inside {embedded}"


def test_run_agent_scrubs_structured_grok_after_parsing(monkeypatch):
    token = "lane-token-value-for-test"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, _grok_json_output(f"answer used {token}"), ""),
    )
    monkeypatch.setenv("LANE_KEY", token)

    result = agents.run_agent(
        "grok",
        "review it",
        read_only=True,
        env={"GROK_AUTH_TOKEN_REF": "LANE_KEY"},
    )

    assert result.ok
    assert result.text == "answer used [GROK_AUTH_TOKEN]"
    assert token not in result.stdout


def test_run_agent_classifies_output_before_scrubbing_env_values(monkeypatch):
    diagnostic = "rate limit exceeded"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"Error: {diagnostic} for this provider.\n", ""),
    )
    monkeypatch.setenv("LANE_DIAGNOSTIC", diagnostic)

    result = agents.run_agent("claude", "hi", env={"LANE_MODE_REF": "LANE_DIAGNOSTIC"})

    assert not result.ok
    assert result.failure_kind == "rate-limit-error"
    assert diagnostic not in result.text
    assert diagnostic not in result.detail
    assert diagnostic not in result.stdout
    assert result.text == "Error: [LANE_MODE] for this provider."
    assert "Error: [LANE_MODE] for this provider." in result.detail


def test_run_agent_classifies_invalid_grok_before_scrubbing_env_values(monkeypatch):
    diagnostic = "rate limit exceeded"
    stdout = _grok_json_output(f"Error: {diagnostic} for this provider.", structured=False)
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, stdout + "\n", ""),
    )
    monkeypatch.setenv("LANE_DIAGNOSTIC", diagnostic)

    result = agents.run_agent(
        "grok",
        "review it",
        read_only=True,
        env={"GROK_MODE_REF": "LANE_DIAGNOSTIC"},
    )

    assert not result.ok
    assert result.failure_kind == "rate-limit-error"
    assert diagnostic not in result.text
    assert diagnostic not in result.detail
    assert diagnostic not in result.stdout
    assert result.text == "Error: [GROK_MODE] for this provider."
    assert "Error: [GROK_MODE] for this provider." in result.detail


def test_run_agent_scrubs_long_env_value_before_detail_truncation(monkeypatch):
    token = "secret-boundary-" + "x" * 240
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(
            0,
            f"Error: rate limit exceeded while using {token}.\n",
            "",
        ),
    )
    monkeypatch.setenv("LONG_LANE_TOKEN", token)

    result = agents.run_agent("claude", "hi", env={"LANE_TOKEN_REF": "LONG_LANE_TOKEN"})

    assert not result.ok
    assert result.failure_kind == "rate-limit-error"
    assert token[:80] not in result.detail
    assert "[LANE_TOKEN]" in result.detail
    assert len(result.detail) <= 200


def test_run_agent_scrubs_long_grok_error_before_detail_truncation(monkeypatch):
    token = "secret-boundary-" + "x" * 240
    payload = json.loads(_grok_json_output("No findings."))
    payload["structuredOutputError"] = f"schema rejected token {token}"
    stdout = json.dumps(payload)
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, stdout + "\n", ""),
    )
    monkeypatch.setenv("LONG_GROK_TOKEN", token)

    result = agents.run_agent(
        "grok",
        "review it",
        read_only=True,
        env={"GROK_TOKEN_REF": "LONG_GROK_TOKEN"},
    )

    assert not result.ok
    assert result.failure_kind == "malformed-final-output"
    assert token[:80] not in result.detail
    assert "[GROK_TOKEN]" in result.detail
    assert len(result.detail) <= 200


def test_run_agent_does_not_scrub_unrelated_parent_environment(monkeypatch):
    unrelated = "parent-value-not-overridden"
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc,
        "run",
        lambda argv, **kw: agents.proc.Result(0, f"diagnostic {unrelated}\n", ""),
    )
    monkeypatch.setenv("UNRELATED_VALUE", unrelated)

    result = agents.run_agent("claude", "hi", env={"LANE_MODE": "test"})

    assert result.ok
    assert unrelated in result.text


def test_run_agent_env_ref_missing_fails_before_spawn(monkeypatch):
    calls = []
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.delenv("MISSING_LANE_KEY", raising=False)

    result = agents.run_agent("claude", "hi", env={"ANTHROPIC_AUTH_TOKEN_REF": "MISSING_LANE_KEY"})
    assert not result.ok
    assert "MISSING_LANE_KEY" in result.detail
    assert "is not set" in result.detail
    assert calls == []


def test_run_agent_env_default_leaves_child_environment_alone(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        return agents.proc.Result(0, "answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)

    assert agents.run_agent("claude", "hi").ok
    assert captured["env"] is None


def test_run_agent_env_ref_missing_is_typed_failure(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.delenv("MISSING_LANE_KEY", raising=False)
    result = agents.run_agent("claude", "hi", env={"ANTHROPIC_AUTH_TOKEN_REF": "MISSING_LANE_KEY"})
    assert not result.ok
    assert result.failure_kind == "env-ref-missing"


def test_run_agent_env_rejects_bare_ref_suffix(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setenv("HOME_VAR", "value")
    result = agents.run_agent("claude", "hi", env={"_REF": "HOME_VAR"})
    assert not result.ok
    assert "empty" in result.detail


def test_run_agent_env_ref_empty_value_is_typed_failure(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setenv("EMPTY_LANE_KEY", "")
    result = agents.run_agent("claude", "hi", env={"ANTHROPIC_AUTH_TOKEN_REF": "EMPTY_LANE_KEY"})
    assert not result.ok
    assert result.failure_kind == "env-ref-missing"
    assert "is not set or is empty" in result.detail
