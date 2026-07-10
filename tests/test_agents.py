import pytest

from brigade import agents


def test_build_argv_for_known_clis():
    assert agents.build_argv("claude", "hi") == ["claude", "-p", "hi"]
    assert agents.build_argv("codex", "hi") == ["codex", "exec", "hi"]
    assert agents.build_argv("opencode", "hi") == ["opencode", "run", "hi"]
    assert agents.build_argv("antigravity", "hi") == [
        "agy",
        "--dangerously-skip-permissions",
        "--print",
        "hi",
    ]
    assert agents.build_argv("pi", "hi") == ["pi", "-p", "hi"]
    assert agents.build_argv("cursor", "hi") == ["cursor-agent", "-p", "--output-format", "text", "hi"]
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


def test_run_agent_nonzero_is_not_ok(monkeypatch):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(1, "", "boom"))
    res = agents.run_agent("claude", "x")
    assert res.ok is False
    assert "boom" in res.detail


class _StubThread:
    def __init__(self, result):
        self._result = result
        self.prompts = []

    def run_turn(self, prompt, *, timeout, on_event=None):
        self.prompts.append((prompt, timeout))
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


def test_run_codex_appserver_empty_text_not_ok():
    from brigade import codex_appserver

    turn = codex_appserver.TurnResult(text="", ok=True, status="complete", thread_id="t-1")
    server = _StubServer(turn)
    res = agents.run_codex_appserver(server, "p", timeout=5.0, cwd=None)
    assert not res.ok and res.detail == "empty output"


def test_run_codex_appserver_server_error_is_failed():
    server = _StubServer(None, fail=True)
    res = agents.run_codex_appserver(server, "p", timeout=5.0, cwd=None)
    assert not res.ok and res.status == "failed" and "boom" in res.detail


def test_agent_result_defaults_keep_exec_contract():
    res = agents.AgentResult(text="t", ok=True)
    assert res.thread_id is None and res.status == ""
