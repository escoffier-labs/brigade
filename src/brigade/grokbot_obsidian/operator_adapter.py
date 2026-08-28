"""Pinned private adapter inventory and fingerprint for Phase 2 CAS tools."""

from __future__ import annotations

import hashlib
import json
from typing import Any

ADAPTER_MANIFEST_ID = "grokbot-operator-adapter"
ADAPTER_MANIFEST_VERSION = "0.1.0"
OPERATOR_ADAPTER_TOOLS = frozenset(
    {
        "grokbot_replace_canvas_v1",
        "grokbot_replace_base_v1",
        "grokbot_replace_excalidraw_v1",
    }
)
PRIVATE_TOOL_DESCRIPTIONS = {
    "grokbot_replace_canvas_v1": f"Private canvas compare-and-swap v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_replace_base_v1": f"Private base compare-and-swap v{ADAPTER_MANIFEST_VERSION}",
    "grokbot_replace_excalidraw_v1": f"Private excalidraw compare-and-swap v{ADAPTER_MANIFEST_VERSION}",
}
PRIVATE_TOOL_INPUT_KEYS = ("expected_sha256", "path", "replacement_utf8")
PRIVATE_TOOL_RESULT_KEYS = ("previous_sha256", "resulting_sha256")


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
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValueError("adapter inventory")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("adapter inventory")
        name = item.get("name")
        if not isinstance(name, str) or name not in OPERATOR_ADAPTER_TOOLS or name in seen:
            raise ValueError("adapter inventory")
        keys = _tool_input_keys(item)
        if tuple(keys) != PRIVATE_TOOL_INPUT_KEYS:
            raise ValueError("adapter inventory")
        description = item.get("description")
        if description != PRIVATE_TOOL_DESCRIPTIONS[name]:
            raise ValueError("adapter inventory")
        seen.add(name)
        parsed.append(
            {
                "description": description,
                "input": list(PRIVATE_TOOL_INPUT_KEYS),
                "name": name,
            }
        )
    if seen != set(OPERATOR_ADAPTER_TOOLS):
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
                "input": list(PRIVATE_TOOL_INPUT_KEYS),
            }
            for name in sorted(OPERATOR_ADAPTER_TOOLS)
        ]
    )
