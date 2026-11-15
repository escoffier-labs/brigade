"""Daybreak round-three regressions for authenticated use and coverage."""

from datetime import timedelta
import json
import os

import pytest

from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority

from brigade import memory_card_reads as reads, memory_lifecycle as life, memory_review, memory_hooks
from brigade.claude_hooks import hub_recall
from tests.memory_read_fixtures import signing_key
from tests.test_memory_lifecycle_security import ID, OLD, TODAY, old_card, cover


@pytest.mark.parametrize(
    "context",
    [{"agent_id": "worker"}, {"agent_type": "subagent"}, {"session_kind": "automated"}],
)
def test_automated_read_cannot_keep_card_alive(tmp_path, monkeypatch, context):
    path = old_card(tmp_path)
    cover(tmp_path)
    monkeypatch.setattr(memory_hooks, "resolve_memory_recall_target", lambda _: (tmp_path, "active"))
    assert hub_recall.observe_read(
        tmp_path, {"session_id": "worker", "tool_input": {"file_path": str(path)}, **context}
    )
    assert not reads.load(tmp_path).last_read
    assert not memory_review._opens(tmp_path)
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"


def test_forged_interactive_open_is_not_use(tmp_path):
    path = old_card(tmp_path)
    cover(tmp_path)
    ledger = hub_recall.ledger_path(tmp_path, "forged")
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"v": 1, "opened": [ID], "interactive_open_days": {ID: [TODAY.isoformat()]}}))
    assert not memory_review._opens(tmp_path)
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"
    assert not path.exists()


def test_unsigned_coverage_cannot_authorize_archiving(tmp_path):
    path = old_card(tmp_path)
    (tmp_path / reads.READS_REL).write_text(
        json.dumps(
            {
                "tracking_since": OLD.isoformat(),
                "healthy_since": OLD.isoformat(),
                "heartbeat": TODAY.isoformat(),
                "last_read": {},
            }
        )
    )
    result = life.run(tmp_path, today=TODAY)
    assert path.exists()
    assert result["blocked"] == "local read coverage incomplete"


@pytest.mark.parametrize("reference", [f"See {ID}.", "[details](notes.md)"])
def test_active_card_reference_preserves_dependency(tmp_path, reference):
    path = old_card(tmp_path)
    cover(tmp_path)
    recent = path.with_name("recent.md")
    recent.write_text(f"---\nid: recent\ntrust: agent-unreviewed\n---\n{reference}\n")
    result = life.run(tmp_path, today=TODAY)
    assert path.exists(), result
    assert {row["reason"] for row in result["skipped"]} == {"referenced"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink instruction probe")
def test_symlinked_instructions_do_not_crash_reference_scan(tmp_path):
    path = old_card(tmp_path)
    cover(tmp_path)
    (tmp_path / "AGENTS.md").write_text("unrelated instructions")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"
    assert not path.exists()


def test_edited_authenticated_use_cannot_keep_card_alive(tmp_path):
    old_card(tmp_path)
    cover(tmp_path)
    ledger = tmp_path / reads.READS_REL
    value = json.loads(ledger.read_text())
    value["last_read"] = {ID: TODAY.isoformat()}
    ledger.write_text(json.dumps(value))
    assert reads.last_read(reads.load(tmp_path), [ID]) is None
    assert not reads.covered(reads.load(tmp_path), today=TODAY, days=90)


def test_edited_coverage_cannot_extend_a_signed_window(tmp_path):
    signing_key(tmp_path)
    reads.record(tmp_path, [], today=TODAY)
    ledger = tmp_path / reads.READS_REL
    value = json.loads(ledger.read_text())
    value["healthy_since"] = (TODAY - timedelta(days=318)).isoformat()
    ledger.write_text(json.dumps(value))
    assert not reads.covered(reads.load(tmp_path), today=TODAY, days=90)


@pytest.mark.parametrize("environment", ["BRIGADE_RUN_ID", "BRIGADE_SCANNER_RUN_ID"])
def test_automated_environment_does_not_record_use(tmp_path, monkeypatch, environment):
    path = old_card(tmp_path)
    cover(tmp_path)
    monkeypatch.setattr(memory_hooks, "resolve_memory_recall_target", lambda _: (tmp_path, "active"))
    monkeypatch.setenv(environment, "example-run")
    assert hub_recall.observe_read(tmp_path, {"session_id": "worker", "tool_input": {"file_path": str(path)}})
    assert not reads.load(tmp_path).last_read
    assert not memory_review._opens(tmp_path)


@pytest.mark.parametrize("malformed_mac", [None, "x" * 64, "\u00e9" * 64])
def test_forged_hook_days_are_not_laundered_by_real_open(tmp_path, monkeypatch, malformed_mac):
    path = old_card(tmp_path)
    ledger_path = hub_recall.ledger_path(tmp_path, "session")
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "v": 1,
                "opened": ["forged"],
                "interactive_open_days": {"forged": [TODAY.isoformat()]},
                "interactive_open_auth": malformed_mac,
            }
        )
    )
    assert not memory_review._opens(tmp_path)
    monkeypatch.setattr(memory_hooks, "resolve_memory_recall_target", lambda _: (tmp_path, "active"))
    assert hub_recall.observe_read(tmp_path, {"session_id": "session", "tool_input": {"file_path": str(path)}})
    assert set(memory_review._opens(tmp_path)) == {ID}


@pytest.mark.parametrize("destination", ["notes.md#details", "../cards/notes.md", "<notes.md>"])
def test_relative_link_variants_protect(tmp_path, destination):
    path = old_card(tmp_path)
    cover(tmp_path)
    path.with_name("recent.md").write_text(f"[details]({destination})")
    assert not life.run(tmp_path, today=TODAY)["transitions"]


@pytest.mark.parametrize(
    "reference",
    [
        f"See {ID}.suffix",
        f"prefix-{ID}",
        "[other](other/notes.md)",
        "[external](https://example.com/notes.md)",
        "some/memory/cards/notes.md",
    ],
)
def test_partial_identity_and_unrelated_paths_do_not_protect(tmp_path, reference):
    path = old_card(tmp_path)
    cover(tmp_path)
    path.with_name("recent.md").write_text(reference)
    life.run(tmp_path, today=TODAY)
    assert not path.exists()


def test_signed_history_with_a_missing_day_is_incomplete(tmp_path):
    old_card(tmp_path)
    cover(tmp_path)
    value = reads._read_json(tmp_path, reads.READS_REL)
    value.pop("mac")
    del value["coverage_days"][10]
    value["mac"] = reads.authenticate(tmp_path, reads.READS_REL, value)
    reads._write_json(tmp_path, reads.READS_REL, value)
    assert not reads.covered(reads.load(tmp_path), today=TODAY, days=90)


def test_workspace_and_session_replay_are_rejected(tmp_path):
    from tests.test_memory_review import opened

    signing_key(tmp_path)
    opened(tmp_path, "original", ID, [TODAY.isoformat()])
    original = hub_recall.ledger_path(tmp_path, "original")
    replay = hub_recall.ledger_path(tmp_path, "another")
    replay.write_bytes(original.read_bytes())
    assert memory_review._opens(tmp_path)[ID][0] == {original.stem}
    other = tmp_path / "other-workspace"
    signing_key(other)
    other_path = hub_recall.ledger_path(other, "original")
    other_path.parent.mkdir(parents=True)
    other_path.write_bytes(original.read_bytes())
    assert not memory_review._opens(other)
