# content-guard: allow bearer-token file
"""Private atomic Wazuh triage state: mode, bounds, and public projections."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade.grokbot_wazuh.contracts import WazuhError, parse_ingest_input
from brigade.grokbot_wazuh.normalize import normalize_alert
from brigade.grokbot_wazuh.policy import classify
from brigade.grokbot_wazuh.store import WazuhStore

NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
SECRET_DETAIL = "Bearer ghp_exampleNotARealToken0000000000000001 path=/etc/shadow"


def _record(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "rule_id": "504",
        "rule_level": 12,
        "rule_description": "Agent disconnected",
        "rule_groups": ["agent_disconnected"],
        "agent_id": "001",
        "decoder": "agent-buffer",
        "timestamp": "2026-08-28T16:00:00Z",
        "detail": SECRET_DETAIL,
    }
    payload.update(overrides)
    return normalize_alert(parse_ingest_input({"alerts": [payload]})["alerts"][0])


def test_store_upserts_one_current_alert_and_writes_mode_0600(tmp_path: Path):
    path = tmp_path / "state" / "wazuh.json"
    store = WazuhStore(str(path))
    store.ready()
    record = _record()
    decision = classify(record, now=NOW)
    first = store.upsert_alert(record, decision, now=NOW)
    second = store.upsert_alert(record, decision, now=NOW)
    assert first["created"] is True
    assert second["created"] is False
    assert store.current_count() == 1
    stored = store.get_alert(record["fingerprint"])
    assert stored is not None
    assert stored["count"] == 2
    assert stored["fingerprint"] == record["fingerprint"]
    public = store.public_alert(record["fingerprint"])
    assert public is not None
    assert "body" not in public
    assert "title" not in public
    assert "source_ref" not in public
    leaked = json.dumps(public)
    assert "ghp_exampleNotARealToken0000000000000001" not in leaked
    assert "/etc/shadow" not in leaked
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_store_rejects_insecure_mode_and_keeps_suppressions_bounded(tmp_path: Path):
    path = tmp_path / "state" / "wazuh.json"
    path.parent.mkdir(mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    store = WazuhStore(str(path))
    with pytest.raises(WazuhError) as caught:
        store.ready()
    assert caught.value.code in {"unavailable", "protocol_error"}
    assert str(path) not in str(caught.value)


def test_store_reads_through_nofollow_descriptor_not_path_read_text(tmp_path: Path, monkeypatch):
    path = tmp_path / "state" / "wazuh.json"
    store = WazuhStore(str(path))
    store.ready()
    record = _record()
    store.upsert_alert(record, classify(record, now=NOW), now=NOW)
    original = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "wazuh.json":
            raise AssertionError("TOCTOU Path.read_text")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    loaded = store.get_alert(record["fingerprint"])
    assert loaded is not None
    assert loaded["finding_id"] == record["finding_id"]
    linked = tmp_path / "linked-state" / "wazuh.json"
    linked.parent.mkdir(mode=0o700)
    os.chmod(linked.parent, 0o700)
    real = tmp_path / "linked-state" / "real.json"
    real.write_bytes(path.read_bytes())
    os.chmod(real, 0o600)
    linked.symlink_to(real)
    with pytest.raises(WazuhError) as caught:
        WazuhStore(str(linked)).ready()
    assert caught.value.code in {"unavailable", "protocol_error"}
    assert str(linked) not in str(caught.value)


def test_store_updates_semantic_fields_on_same_fingerprint(tmp_path: Path):
    path = tmp_path / "state" / "wazuh.json"
    store = WazuhStore(str(path))
    store.ready()
    first = _record(rule_level=8, rule_description="Agent disconnected", detail="first body")
    second = _record(rule_level=15, rule_description="Agent disconnected critical", detail="escalated body")
    assert first["fingerprint"] == second["fingerprint"]
    store.upsert_alert(first, classify(first, now=NOW), now=NOW)
    store.upsert_alert(second, classify(second, now=NOW), now=NOW)
    stored = store.get_alert(first["fingerprint"])
    assert stored is not None
    assert stored["count"] == 2
    assert stored["severity"] == "critical"
    assert stored["title"] == second["title"]
    assert stored["body"] == second["body"]
    assert stored["revision"] == second["revision"]
    assert stored["content_digest"] == second["content_digest"]
    assert stored["source_digest"] == second["source_digest"]
