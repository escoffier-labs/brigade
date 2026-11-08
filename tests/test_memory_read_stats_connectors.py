"""Remote connector profiles and fleet digests never carry read counts (memory verification slice 2)."""

from tests.dot_support import requires_local_state

import json
from pathlib import Path

from brigade.dot.contracts import TOOL_SCHEMAS
from brigade.dot.profile import DOT, DUET, PROFILES
from tests.test_dot_duet_prompt_scan import Scan

READ_COUNT_TERMS = ("read_stats", "reads_7d", "reads_30d", "last_read_day", "total_reads", "read_counts_since_day")
DOT_SOURCES = Path(__file__).resolve().parents[1] / "src" / "brigade" / "dot"
SNAPSHOT = Path(__file__).parent / "fixtures" / "dot_tools_snapshot.json"


def test_no_connector_tool_or_schema_names_read_counts():
    for name, profile in PROFILES.items():
        for tool in profile.tools:
            assert "stats" not in tool, (name, tool)
    schemas = json.dumps(TOOL_SCHEMAS, sort_keys=True, default=str)
    snapshot = SNAPSHOT.read_text()
    for term in READ_COUNT_TERMS:
        assert term not in schemas and term not in snapshot, term


def test_no_connector_module_asks_the_hub_for_read_counts():
    # `read_stats` is the only hub action that returns counts. Memory tools, hub
    # tools and the fleet digest all live under brigade/dot, and none sends it.
    for source in sorted(DOT_SOURCES.rglob("*.py")):
        assert "read_stats" not in source.read_text(), source.name


class TestSearchDropsReadCounts(Scan):
    @requires_local_state
    def test_memory_search_drops_read_count_fields_a_hub_reply_carries(self):
        counts = {"reads_7d": 4, "reads_30d": 9, "last_read_day": 20400, "total_reads": 12}
        self.search_reply(
            self.match(
                "clean-card",
                summary="Ship on Tuesdays.",
                source="hub",
                scope="fleet",
                revision=3,
                trust="agent-unreviewed",
                **counts,
            ),
            retrieval={"mode": "lexical", "read_counts_since_day": 20300, **counts},
        )
        for profile in (DOT, DUET):
            payload = self.memory(profile).memory_search("release")
            assert payload["matches"], profile.pack_id
            serialized = json.dumps(payload)
            for term in READ_COUNT_TERMS:
                assert term not in serialized, (profile.pack_id, term)
