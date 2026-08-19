"""#1029 mutation tests: scanner-forged trust-clean capability is refused."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import authority_broker, trust_gate


def test_notify_miseledger_trust_writes_handoff_to_stdin_not_env(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        captured["input"] = kwargs.get("input")
        captured["stdin"] = kwargs.get("stdin")

        class Result:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""

        return Result()

    from brigade import component_bins

    monkeypatch.setattr(trust_gate.subprocess, "run", fake_run)
    monkeypatch.setattr(component_bins, "resolve", lambda name: "/tmp/fake-miseledger")
    trust_gate.notify_miseledger_trust(
        tmp_path,
        "item-1",
        "a" * 64,
        to_label="verified",
        operator_command="operator:brigade evidence trust review",
        mark_injection_clean=True,
    )
    assert captured["stdin"] is None
    assert isinstance(captured["input"], str)
    secret, capability = authority_broker.decode_handoff(captured["input"])
    assert len(secret) == 32
    assert capability["transition"]["mark_injection_clean"] is True
    env = captured["env"]
    assert isinstance(env, dict)
    assert env.get("BRIGADE_REQUIRE_TRUST_CAPABILITY") == "1"
    dumped = json.dumps(env)
    assert secret.hex() not in dumped
    assert "BRIGADE_CAPABILITY_SECRET" not in env
    assert "--capability" not in captured["command"]


def test_scanner_child_env_cannot_carry_store_key_override():
    from brigade.work_cmd import scanners as scanners_mod

    assert "BRIGADE_AUTHORITY_KEY_FILE" not in scanners_mod._SCANNER_CHILD_ENV_ALLOWLIST
    source = Path(scanners_mod.__file__).read_text(encoding="utf-8")
    assert "BRIGADE_AUTHORITY_KEY_FILE" not in scanners_mod._SCANNER_CHILD_ENV_ALLOWLIST
    assert "_SCANNER_CHILD_ENV_ALLOWLIST" in source
