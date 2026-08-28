"""Phase 1 action catalog and canonical content hashing."""

from __future__ import annotations

import hashlib
import json
from typing import NoReturn

from .contracts import (
    ERROR_MESSAGES,
    ObsidianError,
    parse_phase1_action,
    require_request_id,
)
from .utf8 import utf8_byte_length

HASH_MAX_DEPTH = 16
HASH_MAX_ENTRIES = 16_384
HASH_MAX_BYTES = 262_144
INDEX_KEY = __import__("re").compile(r"^(0|[1-9]\d*)$")
HEX64 = __import__("re").compile(r"^[0-9a-f]{64}$")

_PHASE1_CATALOG: tuple[dict[str, str], ...] = (
    {
        "kind": "create_note",
        "phase": "1",
        "blast_radius": "one new markdown file",
        "summary": "Create one markdown note",
    },
    {
        "kind": "patch_note",
        "phase": "1",
        "blast_radius": "one existing markdown file; no whole-file Markdown replacement",
        "summary": "Patch one markdown note",
    },
    {
        "kind": "copy_note",
        "phase": "1",
        "blast_radius": "one new file; overwrite forbidden",
        "summary": "Copy one markdown note",
    },
    {
        "kind": "move_note",
        "phase": "1",
        "blast_radius": "one file; overwrite forbidden",
        "summary": "Move one markdown note",
    },
    {
        "kind": "trash_note",
        "phase": "1",
        "blast_radius": "one file to Obsidian trash",
        "summary": "Trash one markdown note",
    },
    {
        "kind": "create_canvas",
        "phase": "1",
        "blast_radius": "one `.canvas`",
        "summary": "Create one canvas",
    },
    {
        "kind": "create_base",
        "phase": "1",
        "blast_radius": "one `.base`",
        "summary": "Create one base",
    },
    {
        "kind": "append_flashcard",
        "phase": "1",
        "blast_radius": "one heading in the configured flashcard note",
        "summary": "Append one flashcard",
    },
    {
        "kind": "create_excalidraw",
        "phase": "1",
        "blast_radius": "one verified Excalidraw artifact under `03 - Resources/Excalidraw/` plus optional embed patch",
        "summary": "Create one Excalidraw scene",
    },
    {
        "kind": "apply_template",
        "phase": "1",
        "blast_radius": "one Markdown create or structured patch after server-side expansion",
        "summary": "Apply one template",
    },
)
_CATALOG_BY_KIND = {row["kind"]: row for row in _PHASE1_CATALOG}


def _reject() -> NoReturn:
    raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])


def _not_found() -> NoReturn:
    raise ObsidianError("not_found", ERROR_MESSAGES["not_found"])


def list_catalog() -> tuple[dict[str, str], ...]:
    return tuple(dict(row) for row in _PHASE1_CATALOG)


def get_catalog_row(kind: str) -> dict[str, str]:
    row = _CATALOG_BY_KIND.get(kind)
    if row is None:
        _not_found()
    return dict(row)


def _canonical_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if utf8_byte_length(encoded) > HASH_MAX_BYTES:
        _reject()
    return encoded


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonicalize(value: object, *, depth: int, budget: list[int], kind_first: bool = False) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value or value in {float("inf"), float("-inf")}:
            _reject()
        if isinstance(value, float) and value == 0.0 and str(value).startswith("-"):
            _reject()
        return value
    if not isinstance(value, (list, dict)):
        _reject()
    if depth >= HASH_MAX_DEPTH:
        _reject()
    if isinstance(value, list):
        budget[0] += len(value)
        if budget[0] > HASH_MAX_ENTRIES:
            _reject()
        return [_canonicalize(item, depth=depth + 1, budget=budget) for item in value]
    keys = list(value)
    budget[0] += len(keys)
    if budget[0] > HASH_MAX_ENTRIES:
        _reject()
    ordered = (["kind"] if kind_first and "kind" in keys else []) + sorted(
        key for key in keys if not (kind_first and key == "kind")
    )
    return {key: _canonicalize(value[key], depth=depth + 1, budget=budget) for key in ordered}


def hash_template_definition(raw: object) -> str:
    if not isinstance(raw, dict):
        _reject()
    canonical = _canonicalize(raw, depth=0, budget=[0])
    if not isinstance(canonical, dict):
        _reject()
    required = {"id", "body", "variables"}
    if set(canonical) != required:
        _reject()
    if not isinstance(canonical["id"], str) or not 1 <= len(canonical["id"]) <= 64:
        _reject()
    if not isinstance(canonical["body"], str) or not 1 <= utf8_byte_length(canonical["body"]) <= 12_288:
        _reject()
    variables = canonical["variables"]
    if not isinstance(variables, dict):
        _reject()
    for key, kind in variables.items():
        if not isinstance(key, str) or kind not in {"string", "string_array", "number", "boolean"}:
            _reject()
    return _digest(_canonicalize(canonical, depth=0, budget=[0]))


def hash_action_content(raw: object) -> str:
    if not isinstance(raw, dict):
        _reject()
    canonical = _canonicalize(raw, depth=0, budget=[0])
    if not isinstance(canonical, dict):
        _reject()
    allowed = {"request_id", "action"}
    if "template_digest" in canonical:
        allowed = allowed | {"template_digest"}
    if set(canonical) != allowed:
        _reject()
    request_id = require_request_id(canonical.get("request_id"))
    action = parse_phase1_action(canonical.get("action"))
    digest = canonical.get("template_digest")
    if (action["kind"] == "apply_template") != (digest is not None):
        _reject()
    if digest is not None and (not isinstance(digest, str) or not HEX64.fullmatch(digest)):
        _reject()
    payload: dict[str, object] = {
        "action": _canonicalize(action, depth=0, budget=[0], kind_first=True),
        "request_id": request_id,
    }
    if digest is not None:
        payload["template_digest"] = digest
    return _digest(_canonicalize(payload, depth=0, budget=[0]))
