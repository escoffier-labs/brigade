"""Fail-open context coverage helpers for code graph briefs and deltas."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]+(?::\d+(?::\d+)?)?)"
)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_CODE_EXTENSIONS = {
    ".bash",
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".fish",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".php",
    ".py",
    ".pyi",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
    ".zsh",
}


def extract_brief_files(brief_text: str) -> list[str]:
    """Extract conservative repo-relative code paths from GraphTrail markdown."""
    try:
        if not isinstance(brief_text, str) or not brief_text:
            return []
        files: set[str] = set()
        for match in _BACKTICK_RE.finditer(brief_text):
            candidate = _clean_path(match.group(1))
            if candidate is not None:
                files.add(candidate)
        for match in _PATH_TOKEN_RE.finditer(brief_text):
            candidate = _clean_path(match.group(1))
            if candidate is not None:
                files.add(candidate)
        return sorted(files)
    except BaseException:
        return []


def extract_delta_files(delta_sidecar_path_or_dict: str | Path | dict[str, Any]) -> list[str]:
    """Extract changed file paths from a GraphTrail delta sidecar or payload."""
    try:
        if isinstance(delta_sidecar_path_or_dict, dict):
            payload = delta_sidecar_path_or_dict
        else:
            path = Path(delta_sidecar_path_or_dict)
            payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            return []

        files: set[str] = set()
        for key in ("added_nodes", "removed_nodes", "changed_nodes"):
            nodes = payload.get(key)
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                candidate = _clean_path(node.get("file_path"))
                if candidate is not None:
                    files.add(candidate)
        return sorted(files)
    except BaseException:
        return []


def evaluate(brief_files: list[str], delta_files: list[str]) -> dict[str, object]:
    """Compare brief coverage against delta files."""
    brief = {_cleaned for item in brief_files if (_cleaned := _clean_path(item)) is not None}
    delta = {_cleaned for item in delta_files if (_cleaned := _clean_path(item)) is not None}
    hits = sorted(brief & delta)
    missed = sorted(delta - brief)
    return {
        "counts": {
            "brief_files": len(brief),
            "delta_files": len(delta),
            "hits": len(hits),
            "missed": len(missed),
        },
        "hits": hits,
        "missed": missed,
        "brief_hit_rate": round(len(hits) / len(delta), 3) if delta else None,
    }


def _clean_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().strip("`'\"()[]{}<>,.;")
    if not raw or "://" in raw or raw.startswith(("/", "~")):
        return None
    raw = re.sub(r":\d+(?::\d+)?$", "", raw)
    if "\\" in raw:
        return None
    if any(char.isspace() for char in raw):
        return None
    path = Path(raw)
    if path.is_absolute():
        return None
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in _CODE_EXTENSIONS:
        return None
    return "/".join(parts)
