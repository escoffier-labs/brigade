"""Vault path and content policy for Phase 1 Obsidian Operator."""

from __future__ import annotations

import pytest

from brigade.grokbot_obsidian.content_policy import assert_safe_markdown, contains_executable_markdown
from brigade.grokbot_obsidian.contracts import ObsidianError
from brigade.grokbot_obsidian.path_policy import assert_static_readable, assert_static_writable, normalize_vault_path

POLICY = {
    "dailyNotesFolder": "00 - Inbox/Daily",
    "sensitivePathPrefixes": ("05 - Sensitive",),
    "sensitiveTags": ("private",),
    "dashboardRoot": "01 - Projects/Dashboard.base",
    "excalidrawSuffix": ".excalidraw.md",
    "tags": (),
}


def test_normalize_accepts_vault_relative_posix_and_rejects_escapes():
    assert normalize_vault_path("01 - Projects/demo.md") == "01 - Projects/demo.md"
    for path in (
        "/etc/passwd",
        "../outside.md",
        "01 - Projects/./demo.md",
        "01 - Projects\\demo.md",
        "file:demo.md",
        ".hidden/note.md",
    ):
        with pytest.raises(ObsidianError) as caught:
            normalize_vault_path(path)
        assert caught.value.code == "denied"
        assert path not in str(caught.value)


def test_static_readable_denies_attachments_memory_daily_and_sensitive():
    assert assert_static_readable("01 - Projects/demo.md", POLICY) == "01 - Projects/demo.md"
    for path in (
        "01 - Projects/Attachments/photo.png",
        "Brigade Memory/card.md",
        "00 - Inbox/Daily/2026-08-28.md",
        "05 - Sensitive/secret.md",
        "04 - Archive/old.md",
    ):
        with pytest.raises(ObsidianError) as caught:
            assert_static_readable(path, POLICY)
        assert caught.value.code == "denied"


def test_static_writable_roots_and_phase1_kinds():
    assert (
        assert_static_writable("create_note", "00 - Inbox/Agent Notes/demo.md", POLICY)
        == "00 - Inbox/Agent Notes/demo.md"
    )
    assert (
        assert_static_writable("create_excalidraw", "03 - Resources/Excalidraw/scene.excalidraw.md", POLICY)
        == "03 - Resources/Excalidraw/scene.excalidraw.md"
    )
    with pytest.raises(ObsidianError):
        assert_static_writable("create_note", "04 - Archive/old.md", POLICY)
    with pytest.raises(ObsidianError):
        assert_static_writable("create_excalidraw", "01 - Projects/scene.excalidraw.md", POLICY)


def test_content_policy_rejects_executable_markdown():
    assert contains_executable_markdown("Hello world") is False
    assert contains_executable_markdown("```dataviewjs\n1\n```") is True
    assert contains_executable_markdown("<% tp.file.title %>") is True
    assert contains_executable_markdown("$= dv.pages()") is True
    assert contains_executable_markdown("<script>alert(1)</script>") is True
    assert contains_executable_markdown("javascript:alert(1)") is True
    with pytest.raises(ObsidianError) as caught:
        assert_safe_markdown({"body": "onclick=alert(1)"})
    assert caught.value.code == "denied"
    assert contains_executable_markdown("Use `<%` in a code span") is False
