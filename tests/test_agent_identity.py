from brigade import agent_identity, outcome_cmd
from brigade.claude_hooks import runtime


def test_detect_harness_matches_previous_outcome_detection(monkeypatch):
    for var in (
        "BRIGADE_CONTEXT_HARNESS",
        "CLAUDECODE",
        "CURSOR_TRACE_ID",
        "CODEX_SANDBOX",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "AI_AGENT",
    ):
        monkeypatch.delenv(var, raising=False)
    assert agent_identity.detect_harness() == ("unknown", "unknown")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert agent_identity.detect_harness() == ("claude-code", "auto")
    monkeypatch.setenv("BRIGADE_CONTEXT_HARNESS", "My Harness")
    assert agent_identity.detect_harness() == ("my-harness", "env")
    assert outcome_cmd.context_manifest()["harness"] == "my-harness"


def test_explicit_environ_and_model():
    assert agent_identity.detect_harness({}) == ("unknown", "unknown")
    for signal, harness in [
        ("CURSOR_TRACE_ID", "cursor"),
        ("CODEX_SANDBOX", "codex"),
        ("CODEX_SANDBOX_NETWORK_DISABLED", "codex"),
        ("AI_AGENT", "custom"),
    ]:
        assert agent_identity.detect_harness({signal: "custom"}) == (harness, "auto")
    assert agent_identity.env_model({}) == ("unknown", "unknown")
    assert agent_identity.env_model({"BRIGADE_CONTEXT_MODEL": "  "}) == ("unknown", "unknown")
    assert agent_identity.env_model({"BRIGADE_CONTEXT_MODEL": " model-x "}) == ("model-x", "env")


def test_session_fingerprint_matches_claude_runtime():
    assert agent_identity.session_fingerprint("claude-code", "abc") == runtime._session_fingerprint("abc")
    assert agent_identity.session_fingerprint("codex", "abc") != runtime._session_fingerprint("abc")


def test_identity_as_dict_keys():
    identity = agent_identity.AgentIdentity("codex", "hook-event", "model-x", "hook-event")
    assert identity.as_dict() == {
        "harness": "codex",
        "harness_source": "hook-event",
        "model": "model-x",
        "model_source": "hook-event",
        "agent_type": "unknown",
    }
