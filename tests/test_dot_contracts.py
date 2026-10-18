import json
from pathlib import Path

from brigade.dot import contracts as c

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "dot_tools_snapshot.json").read_text())


def test_tool_names_match_snapshot() -> None:
    assert c.TOOLS == frozenset(SNAPSHOT["tools"])


def test_read_only_flags_match_snapshot() -> None:
    for name, entry in SNAPSHOT["tools"].items():
        assert c.TOOL_SCHEMAS[name].read_only == entry["annotations"].get("readOnlyHint", False), name


def test_dropped_tools_absent() -> None:
    assert not {"dot_t3_submit_review", "dot_t3_job_status", "dot_t3_job_result"} & c.TOOLS


def test_gated_tools() -> None:
    assert c.GATED_TOOLS == frozenset(
        {"fleet_submit", "fleet_followup", "fleet_answer", "fleet_approve", "fleet_interrupt", "message_send"}
    )


def test_pack_constants() -> None:
    assert (c.PACK_ID, c.SERVICE_NAME, c.SERVER_VERSION, c.DEFAULT_BIND) == (
        "dot",
        "dot-t3",
        "0.28.0",
        "127.0.0.1:8779",
    )
