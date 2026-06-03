"""Tests for brigade openclaw-fragments / hermes-fragments."""
from __future__ import annotations

import json
from pathlib import Path

from brigade import fragments as frag_mod
from brigade.templates import (
    load_depth_manifest,
    load_harness_manifest,
    load_include_manifest,
    template_root,
)


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
    assert data.get("_brigade_status") == "experimental"


def test_unknown_harness_errors(tmp_path: Path):
    rc = frag_mod.write_fragments(tmp_path, harness="bogus")
    assert rc == 2


def test_load_depth_repo():
    m = load_depth_manifest("repo")
    assert m["id"] == "repo"
    dsts = [f["dst"] for f in m["files"]]
    assert "AGENTS.md" in dsts
    assert "SAFETY_RULES.md" in dsts
    assert "INSTALL_FOR_AGENTS.md" in dsts
    assert "hooks/pre-push" in dsts
    assert ".brigade/policies/public-repo.json" in dsts
    assert ".brigade/policies/personal.json" in dsts
    # depth baseline does NOT install harness-specific bridge files
    assert "CLAUDE.md" not in dsts


def test_load_depth_workspace():
    m = load_depth_manifest("workspace")
    assert m["id"] == "workspace"
    dsts = [f["dst"] for f in m["files"]]
    assert "AGENTS.md" in dsts
    assert "MEMORY.md" in dsts
    assert "TOOLS.md" in dsts
    assert "USER.md" in dsts
    assert "SOUL.md" in dsts
    assert "IDENTITY.md" in dsts
    assert "HEARTBEAT.md" in dsts
    assert "CLAUDE.md" not in dsts


def test_depth_repo_files_exist():
    m = load_depth_manifest("repo")
    root = template_root()
    for entry in m["files"]:
        assert (root / entry["src"]).is_file(), entry["src"]


def test_load_harness_claude():
    m = load_harness_manifest("claude")
    assert m["id"] == "claude"
    assert m.get("role") == "writer"
    dsts = [f["dst"] for f in m["files"]]
    assert "CLAUDE.md" in dsts
    assert ".claude/memory-handoffs/TEMPLATE.md" in dsts
    assert ".claude/memory-handoffs/processed" in m.get("dirs", [])


def test_load_harness_codex():
    m = load_harness_manifest("codex")
    assert m["id"] == "codex"
    assert m.get("role") == "writer"
    dsts = [f["dst"] for f in m["files"]]
    # Codex has no bridge file today (reads AGENTS.md from depth baseline)
    assert "CODEX.md" not in dsts
    assert ".codex/memory-handoffs/TEMPLATE.md" in dsts
    assert ".codex/memory-handoffs/processed" in m.get("dirs", [])


def test_load_harness_openclaw():
    m = load_harness_manifest("openclaw")
    assert m["id"] == "openclaw"
    assert m.get("role") == "reader"
    dsts = [f["dst"] for f in m["files"]]
    # Reader fragments live under .brigade/openclaw/
    assert any(d.startswith(".brigade/openclaw/") for d in dsts)
    # No inbox for readers
    assert not any("/memory-handoffs/" in d for d in dsts)


def test_load_harness_hermes():
    m = load_harness_manifest("hermes")
    assert m["id"] == "hermes"
    assert m.get("role") == "reader"


def test_codex_template_file_exists():
    from brigade.templates import template_root
    assert (template_root() / "codex" / "memory-handoffs" / "TEMPLATE.md").is_file()


def test_all_harness_files_exist():
    from brigade.templates import template_root
    for h in ("claude", "codex", "openclaw", "hermes"):
        m = load_harness_manifest(h)
        for entry in m["files"]:
            assert (template_root() / entry["src"]).is_file(), f"{h}: {entry['src']}"


def test_load_include_publisher():
    m = load_include_manifest("publisher")
    assert m["id"] == "publisher"
    dsts = [f["dst"] for f in m["files"]]
    assert ".brigade/policies/public-content.json" in dsts
    assert ".brigade/policies/personal.json" in dsts
    assert "memory/cards/content-safety.md" in dsts
