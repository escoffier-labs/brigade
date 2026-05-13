"""Tests for solo-mise openclaw-fragments / hermes-fragments."""
from __future__ import annotations

import json
from pathlib import Path

from solo_mise import fragments as frag_mod


def test_openclaw_fragments(tmp_path: Path):
    out = tmp_path / "openclaw-fragments"
    rc = frag_mod.write_fragments(out, harness="openclaw")
    assert rc == 0
    for name in (
        "model-aliases.openclaw.json",
        "ollama-memory-search.openclaw.json",
        "acp-escalation.openclaw.json",
        "README.md",
    ):
        assert (out / name).is_file()
    # JSON fragments parse
    data = json.loads((out / "model-aliases.openclaw.json").read_text())
    assert "agents" in data


def test_hermes_fragments(tmp_path: Path):
    out = tmp_path / "hermes-fragments"
    rc = frag_mod.write_fragments(out, harness="hermes")
    assert rc == 0
    for name in (
        "workspace.harness.json",
        "memory-handoff.harness.json",
        "model-lanes.harness.json",
        "README.md",
    ):
        assert (out / name).is_file()
    data = json.loads((out / "workspace.harness.json").read_text())
    assert data.get("_solo_mise_status") == "experimental"


def test_unknown_harness_errors(tmp_path: Path):
    rc = frag_mod.write_fragments(tmp_path, harness="bogus")
    assert rc == 2
