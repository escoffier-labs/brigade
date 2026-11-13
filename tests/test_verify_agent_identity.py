"""Identity on real verify receipts and their captured outcomes."""

import json
import sys

import pytest

from brigade import agent_identity, cli, localio, outcome_cmd
from brigade.claude_hooks import runtime
from tests._home import set_home
from tests.test_work_mode import _payload, _wired


@pytest.mark.parametrize("source", ["env", "session", "unknown"])
def test_receipt_and_outcome_identity(tmp_path, monkeypatch, capsys, source):
    set_home(monkeypatch, tmp_path / "home")
    target = _wired(tmp_path)
    for key in (
        "BRIGADE_CONTEXT_MODEL",
        "BRIGADE_CONTEXT_HARNESS",
        "AI_AGENT",
        "CLAUDECODE",
        "CURSOR_TRACE_ID",
        "CODEX_SANDBOX",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "BRIGADE_AGENT_SESSION",
        "BRIGADE_CLAUDE_SESSION",
    ):
        monkeypatch.delenv(key, raising=False)
    if source != "unknown":
        monkeypatch.setenv("CLAUDECODE", "1")
    if source == "env":
        monkeypatch.setenv("BRIGADE_CONTEXT_MODEL", "claude-sonnet-4-6")
    if source == "session":
        monkeypatch.setattr(runtime, "_run_brief", lambda *_a, **_k: "brief")
        runtime.handle_payload("SessionStart", _payload(target, "SessionStart", model="claude-sonnet-4-6"))
        monkeypatch.setenv("BRIGADE_AGENT_SESSION", runtime._session_fingerprint("s1"))
    assert (
        cli.main(
            [
                "work",
                "verify",
                "run",
                "--target",
                str(target),
                "--argv-json",
                json.dumps([sys.executable, "-c", "pass"]),
                "--capture",
                "brigade-work",
            ]
        )
        == 0
    )
    receipt = json.loads(next((target / ".brigade/work/verify-runs").glob("*/receipt.json")).read_text())
    identity = receipt["agent_identity"]
    assert identity["model"] == ("unknown" if source == "unknown" else "claude-sonnet-4-6")
    assert identity["model_source"] == {"env": "env", "session": "hook-session-start", "unknown": "unknown"}[source]
    assert identity["agent_type"] == "unknown"
    assert "harness_session" not in receipt
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    record = json.loads((target / "memory/outcome/records.jsonl").read_text().splitlines()[-1])
    assert record["context"]["agent_identity"] == identity
    plain = {k: v for k, v in record["context"].items() if k != "agent_identity"}
    assert record["capability_fingerprint"] == outcome_cmd.capability_fingerprint(plain)


def test_verify_identity_env_mapping(tmp_path):
    identity = agent_identity.for_verify_run(tmp_path, {"CLAUDECODE": "1", "BRIGADE_CONTEXT_MODEL": "private text"})
    assert identity.model == "unknown"
