"""Generic public capability projection."""

from __future__ import annotations

import pytest

from brigade.grokbot_obsidian.capabilities import (
    hash_capabilities_fingerprint,
    project_phase1,
    reconcile_capabilities,
)
from brigade.grokbot_obsidian.contracts import ObsidianError


def test_projection_is_generic_and_sorted():
    projected = project_phase1(
        [
            {"id": "templater", "version": "1.2.0", "supported_action_ids": ["apply_template"]},
            {"id": "core", "version": "1.0.0", "supported_action_ids": ["create_note", "patch_note"]},
        ]
    )
    assert projected["phase"] == "phase1"
    assert projected["search_backend"] == "native_bounded_search"
    assert [plugin["id"] for plugin in projected["plugins"]] == ["core", "templater"]
    assert "patch_canvas" not in projected["supported_action_ids"]
    dumped = str(projected)
    assert "/home/" not in dumped
    assert "vault" not in dumped.lower() or "untrusted" in dumped


def test_fingerprint_mismatch_fails_closed():
    runtime = {
        "plugin_inventory": [{"id": "core", "version": "1.0.0", "supported_action_ids": ["create_note"]}],
        "command_fingerprint": "sha256:" + ("0" * 64),
    }
    with pytest.raises(ObsidianError) as caught:
        reconcile_capabilities(runtime, lambda: [{"id": "app:reload", "name": "Reload"}])
    assert caught.value.code == "unavailable"


def test_matching_fingerprint_projects_phase1():
    plugins = [{"id": "core", "version": "1.0.0", "supported_action_ids": ["create_note"]}]
    commands = [{"id": "app:reload", "name": "Reload"}]
    fingerprint = hash_capabilities_fingerprint({"version": 1, "plugins": plugins, "commands": commands})
    result = reconcile_capabilities(
        {"plugin_inventory": plugins, "command_fingerprint": fingerprint},
        lambda: commands,
    )
    assert result["status"] == "ok"
    assert result["capabilities"]["supported_action_ids"] == ["create_note"]
