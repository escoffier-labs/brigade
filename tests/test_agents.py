import pytest

from brigade import agents


def test_build_argv_for_known_clis():
    assert agents.build_argv("claude", "hi") == ["claude", "-p", "hi"]
    assert agents.build_argv("codex", "hi") == ["codex", "exec", "hi"]
    assert agents.build_argv("opencode", "hi") == ["opencode", "run", "hi"]
    assert agents.build_argv("antigravity", "hi") == ["agy", "--print", "hi"]
    assert agents.build_argv("pi", "hi") == ["pi", "-p", "hi"]
    assert agents.build_argv("cursor", "hi") == ["cursor-agent", "-p", "--output-format", "text", "hi"]
    assert agents.build_argv("aider", "hi") == ["aider", "--yes", "--no-auto-commits", "--message", "hi"]
    assert agents.build_argv("goose", "hi") == ["goose", "run", "--no-session", "-t", "hi"]
    assert agents.build_argv("continue", "hi") == ["cn", "-p", "hi"]
    assert agents.build_argv("copilot", "hi") == ["copilot", "-p", "hi"]
    assert agents.build_argv("qwen", "hi") == ["qwen", "-p", "hi", "--approval-mode", "yolo"]
    assert agents.build_argv("kimi", "hi") == ["kimi", "--print", "-p", "hi", "--final-message-only"]
    assert agents.build_argv("adal", "hi") == ["adal", "-q", "hi"]
    assert agents.build_argv("openhands", "hi") == ["openhands", "--headless", "-t", "hi"]
    assert agents.build_argv("grok", "hi") == ["grok", "-p", "hi"]
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
    assert agents.build_argv("grok", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("amp", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("crush", "hi", read_only=True)[-1].startswith("Read-only planning run.")
    assert agents.build_argv("ollama:llama3.3", "hi", read_only=True) == [
        "ollama",
        "run",
        "llama3.3",
        "hi",
    ]


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


def test_run_agent_captures_output(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(0, "  answer  ", ""))
    res = agents.run_agent("codex", "do it")
    assert res.ok is True
    assert res.text == "answer"


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


def test_run_agent_nonzero_is_not_ok(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(1, "", "boom"))
    res = agents.run_agent("claude", "x")
    assert res.ok is False
    assert "boom" in res.detail
