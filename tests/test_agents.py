import pytest

from brigade import agents


def test_build_argv_for_known_clis():
    assert agents.build_argv("claude", "hi") == ["claude", "-p", "hi"]
    assert agents.build_argv("codex", "hi") == ["codex", "exec", "hi"]
    assert agents.build_argv("gemini", "hi") == ["gemini", "--prompt", "hi"]
    assert agents.build_argv("opencode", "hi") == ["opencode", "run", "hi"]
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
    assert agents.build_argv("gemini", "hi", read_only=True) == ["gemini", "--prompt", "hi"]
    assert agents.build_argv("opencode", "hi", read_only=True) == ["opencode", "run", "hi"]
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


def test_command_for_returns_binary():
    assert agents.command_for("claude") == "claude"
    assert agents.command_for("codex") == "codex"
    assert agents.command_for("gemini") == "gemini"
    assert agents.command_for("opencode") == "opencode"
    assert agents.command_for("ollama:llama3.3") == "ollama"


def test_is_known():
    assert agents.is_known("claude")
    assert agents.is_known("codex")
    assert agents.is_known("gemini")
    assert agents.is_known("opencode")
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


def test_run_agent_nonzero_is_not_ok(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(1, "", "boom"))
    res = agents.run_agent("claude", "x")
    assert res.ok is False
    assert "boom" in res.detail
