"""Pinned private adapter inventory and fingerprint for Phase 2 private tools."""

from __future__ import annotations

import hashlib
import json
from typing import Any

ADAPTER_MANIFEST_ID = "grokbot-operator-adapter"
ADAPTER_MANIFEST_VERSION = "0.1.0"
CAS_ADAPTER_TOOLS = frozenset(
    {
        "grokbot_replace_canvas_v1",
        "grokbot_replace_base_v1",
        "grokbot_replace_excalidraw_v1",
    }
)
WORKFLOW_ADAPTER_TOOLS = frozenset(
    {
        "grokbot_lint_note_v1",
        "grokbot_auto_move_note_v1",
        "grokbot_sr_open_review_v1",
        "grokbot_homepage_open_v1",
        "grokbot_omnisearch_v1",
        "grokbot_excalidraw_open_v1",
        "grokbot_excalidraw_export_v1",
    }
)
OPERATOR_ADAPTER_TOOLS = CAS_ADAPTER_TOOLS | WORKFLOW_ADAPTER_TOOLS
# Only these tools are callable before the Task 20 canary and capability gate.
# OPERATOR_ADAPTER_TOOLS remains the documented schema inventory.
CALLABLE_OPERATOR_ADAPTER_TOOLS = CAS_ADAPTER_TOOLS | frozenset(
    {
        "grokbot_lint_note_v1",
        "grokbot_sr_open_review_v1",
        "grokbot_omnisearch_v1",
        "grokbot_excalidraw_open_v1",
    }
)
PRIVATE_TOOL_DESCRIPTIONS = {
    "grokbot_replace_canvas_v1": f"Private canvas compare-and-swap v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_replace_base_v1": f"Private base compare-and-swap v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_replace_excalidraw_v1": f"Private excalidraw compare-and-swap v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_lint_note_v1": f"Private fixed Linter note workflow v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_auto_move_note_v1": f"Private fixed Auto Note Mover workflow v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_sr_open_review_v1": f"Private fixed Spaced Repetition review workflow v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_homepage_open_v1": f"Private fixed Homepage workflow v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_omnisearch_v1": f"Private Omnisearch 1.30.1 runtime index v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_excalidraw_open_v1": f"Private fixed Excalidraw open workflow v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_excalidraw_export_v1": f"Private fixed Excalidraw export workflow v{ADAPTER_MANIFEST_VERSION}",
}
PRIVATE_TOOL_INPUT_KEYS = ("expected_sha256", "path", "replacement_utf8")
PRIVATE_TOOL_RESULT_KEYS = ("previous_sha256", "resulting_sha256")
PRIVATE_TOOL_INPUT_SCHEMAS = {
    "grokbot_replace_canvas_v1": PRIVATE_TOOL_INPUT_KEYS,
    "grokbot_replace_base_v1": PRIVATE_TOOL_INPUT_KEYS,
    "grokbot_replace_excalidraw_v1": PRIVATE_TOOL_INPUT_KEYS,
    "grokbot_lint_note_v1": ("expected_sha256", "path"),
    "grokbot_auto_move_note_v1": ("expected_sha256", "path"),
    "grokbot_sr_open_review_v1": (),
    "grokbot_homepage_open_v1": (),
    "grokbot_omnisearch_v1": ("limit", "query"),
    "grokbot_excalidraw_open_v1": ("path",),
    "grokbot_excalidraw_export_v1": ("format", "path"),
}
PRIVATE_TOOL_RESULT_SCHEMAS = {
    "grokbot_replace_canvas_v1": PRIVATE_TOOL_RESULT_KEYS,
    "grokbot_replace_base_v1": PRIVATE_TOOL_RESULT_KEYS,
    "grokbot_replace_excalidraw_v1": PRIVATE_TOOL_RESULT_KEYS,
    "grokbot_lint_note_v1": ("after_sha256", "before_sha256", "path"),
    "grokbot_auto_move_note_v1": ("content_sha256", "destination_path", "source_path"),
    "grokbot_sr_open_review_v1": ("opened", "view_id"),
    "grokbot_homepage_open_v1": ("opened", "path"),
    "grokbot_omnisearch_v1": ("hits",),
    "grokbot_excalidraw_open_v1": ("path", "view_type"),
    "grokbot_excalidraw_export_v1": ("artifact_ref", "content_sha256"),
}


def _tool_input_keys(tool: dict[str, Any]) -> list[str]:
    schema = tool.get("inputSchema")
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        return sorted(str(key) for key in schema["properties"])
    raw = tool.get("input")
    if isinstance(raw, list):
        return sorted(str(key) for key in raw)
    if isinstance(raw, dict):
        return sorted(str(key) for key in raw)
    return []


def canonicalize_private_tools(raw: object) -> list[dict[str, Any]]:
    """Canonicalize the mandatory CAS inventory used by Phase 2 readiness."""
    if not isinstance(raw, list) or len(raw) != len(CAS_ADAPTER_TOOLS):
        raise ValueError("adapter inventory")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("adapter inventory")
        name = item.get("name")
        if not isinstance(name, str) or name not in CAS_ADAPTER_TOOLS or name in seen:
            raise ValueError("adapter inventory")
        keys = _tool_input_keys(item)
        if tuple(keys) != PRIVATE_TOOL_INPUT_SCHEMAS[name]:
            raise ValueError("adapter inventory")
        description = item.get("description")
        if description != PRIVATE_TOOL_DESCRIPTIONS[name]:
            raise ValueError("adapter inventory")
        seen.add(name)
        parsed.append(
            {
                "description": description,
                "input": list(PRIVATE_TOOL_INPUT_SCHEMAS[name]),
                "name": name,
            }
        )
    if seen != set(CAS_ADAPTER_TOOLS):
        raise ValueError("adapter inventory")
    parsed.sort(key=lambda tool: tool["name"])
    return parsed


def hash_private_tool_fingerprint(raw: object) -> str:
    canonical = canonicalize_private_tools(raw)
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def expected_private_tool_fingerprint() -> str:
    return hash_private_tool_fingerprint(
        [
            {
                "name": name,
                "description": PRIVATE_TOOL_DESCRIPTIONS[name],
                "input": list(PRIVATE_TOOL_INPUT_SCHEMAS[name]),
            }
            for name in sorted(CAS_ADAPTER_TOOLS)
        ]
    )
