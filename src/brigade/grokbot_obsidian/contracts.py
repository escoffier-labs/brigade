"""Closed Obsidian Operator Phase 1 public contracts and stable errors."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, NoReturn

from .utf8 import utf8_byte_length, utf8_in_range

PACK_ID = "obsidian-operator"
DEFAULT_BIND = "127.0.0.1:8773"
PUBLIC_ROUTE = "/mcp"
TOOLS = frozenset(
    {
        "obsidian_capabilities",
        "obsidian_search",
        "obsidian_read",
        "obsidian_action_status",
        "obsidian_propose_action",
        "obsidian_execute_action",
    }
)
UNTRUSTED_VAULT_CONTENT = "untrusted_vault_content"
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "denied": "Obsidian request was denied",
    "not_found": "Obsidian resource was not found",
    "unavailable": "Obsidian observation is unavailable",
    "timeout": "Obsidian observation timed out",
    "protocol_error": "Obsidian observation failed",
    "conflict": "Obsidian request conflicted",
}
PHASE1_ACTION_IDS = frozenset(
    {
        "create_note",
        "patch_note",
        "copy_note",
        "move_note",
        "trash_note",
        "create_canvas",
        "create_base",
        "append_flashcard",
        "create_excalidraw",
        "apply_template",
    }
)
PHASE2_ONLY_ACTION_IDS = frozenset({"patch_canvas", "patch_base", "update_excalidraw"})
FORBIDDEN_ACTION_IDS = frozenset(
    {
        "delete_permanent",
        "command_execute",
        "vault_write",
        "run_plugin_command",
        "open_file",
        "delete",
        "write",
        "plugin",
        "installed_plugin",
        "generic_command",
    }
)
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OPAQUE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REVISION_TOKEN_RE = re.compile(r"^[0-9a-f]{6}$")
EXCALIDRAW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
FLASHCARD_RE = re.compile(r"^.{1,200}::.{1,400}$", re.UNICODE)
URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
CANVAS_FILE_ROOTS = (
    "00 - Inbox/Agent Notes",
    "01 - Projects",
    "02 - Areas/07 - Agent Work Log",
    "03 - Resources",
)
PATH_REF_KEYS = frozenset({"file", "url", "path", "toFile", "fromFile"})
JSON_MAX_DEPTH = 8
JSON_MAX_ENTRIES = 256
JSON_MAX_BYTES = 12_288
BODY_BYTES = 12_288
PUBLIC_RESULT_BYTES = 131_072
READ_TEXT_BYTES = 65_536
PROPOSAL_STATES = frozenset({"created", "expired"})
APPROVAL_STATES = frozenset({"missing", "approved", "expired", "consumed"})
EXECUTION_STATES = frozenset({"not_started", "claimed", "completed", "failed"})
VERIFICATION_STATES = frozenset({"not_started", "unknown_after_claim", "verified", "unverified"})


class ObsidianError(Exception):
    """Stable public Obsidian failure. Messages never include paths or child output."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message if message is not None else ERROR_MESSAGES[code]
        super().__init__(self.message)

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": ERROR_MESSAGES[self.code]}}

    def __repr__(self) -> str:
        return f"ObsidianError({self.code!r})"


def _invalid() -> NoReturn:
    raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])


def _require_mapping(raw: object, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _invalid()
    if keys is not None and set(raw) != keys:
        _invalid()
    return raw


def require_utf8(value: object, minimum: int, maximum: int) -> str:
    if not utf8_in_range(value, minimum, maximum):
        _invalid()
    assert isinstance(value, str)
    return value


def require_finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _is_finite(value):
        _invalid()
    return float(value)


def _is_finite(value: float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}


def require_int(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _invalid()
    return value


def require_request_id(value: object) -> str:
    text = require_utf8(value, 8, 64)
    if not REQUEST_ID_RE.fullmatch(text):
        _invalid()
    return text


def require_opaque_id(value: object) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_RE.fullmatch(value):
        _invalid()
    return value


def require_revision(value: object) -> str:
    if not isinstance(value, str) or not REVISION_TOKEN_RE.fullmatch(value):
        _invalid()
    return value


def require_vault_path(value: object) -> str:
    return require_utf8(value, 1, 512)


def require_heading_path(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        _invalid()
    return [require_utf8(item, 1, 256) for item in value]


def require_selector(value: object) -> str:
    return require_utf8(value, 1, 256)


def json_value_depth(value: object) -> int:
    if isinstance(value, list):
        return 1 if not value else 1 + max(json_value_depth(item) for item in value)
    if isinstance(value, dict):
        return 1 if not value else 1 + max(json_value_depth(item) for item in value.values())
    return 0


def json_value_entries(value: object) -> int:
    if isinstance(value, list):
        return len(value) + sum(json_value_entries(item) for item in value)
    if isinstance(value, dict):
        return len(value) + sum(json_value_entries(item) for item in value.values())
    return 0


def is_bounded_json_value(value: object) -> bool:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return (
        json_value_depth(value) <= JSON_MAX_DEPTH
        and json_value_entries(value) <= JSON_MAX_ENTRIES
        and len(encoded.encode("utf-8")) <= JSON_MAX_BYTES
    )


def parse_json_value(value: object, *, depth: int = JSON_MAX_DEPTH) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return require_utf8(value, 0, BODY_BYTES)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return require_finite_number(value)
    if depth <= 0:
        _invalid()
    parsed: Any
    if isinstance(value, list):
        if len(value) > JSON_MAX_ENTRIES:
            _invalid()
        parsed = [parse_json_value(item, depth=depth - 1) for item in value]
    elif isinstance(value, dict):
        parsed = {str(key): parse_json_value(item, depth=depth - 1) for key, item in value.items()}
    else:
        _invalid()
    if not is_bounded_json_value(parsed):
        _invalid()
    return parsed


def _is_allowed_canvas_file_ref(path: str) -> bool:
    if (
        "\0" in path
        or "\\" in path
        or "\r" in path
        or "\n" in path
        or URI_SCHEME_RE.search(path)
        or path.startswith("/")
    ):
        return False
    segments = path.split("/")
    for segment in segments:
        if (
            segment == ""
            or segment in {".", ".."}
            or segment.startswith(".")
            or segment.startswith("-")
            or "%" in segment
            or segment == "Attachments"
        ):
            return False
    normalized = "/".join(segments)
    return any(normalized == root or normalized.startswith(f"{root}/") for root in CANVAS_FILE_ROOTS)


def _canvas_has_forbidden_ref(value: object) -> bool:
    if isinstance(value, list):
        return any(_canvas_has_forbidden_ref(item) for item in value)
    if not isinstance(value, dict):
        return False
    for key in PATH_REF_KEYS:
        if key not in value:
            continue
        field = value[key]
        if not isinstance(field, str):
            return True
        if key == "url" or not _is_allowed_canvas_file_ref(field):
            return True
    return any(_canvas_has_forbidden_ref(item) for item in value.values())


def _parse_canvas_node(raw: object) -> dict[str, Any]:
    node = _require_mapping(raw)
    node_type = node.get("type")
    common = {"id", "type", "x", "y", "width", "height"}
    node_id = require_utf8(node.get("id"), 1, 64)
    x = require_int(node.get("x"), -(2**31), 2**31 - 1)
    y = require_int(node.get("y"), -(2**31), 2**31 - 1)
    width = require_int(node.get("width"), 1, 2**31 - 1)
    height = require_int(node.get("height"), 1, 2**31 - 1)
    if node_type == "text":
        if set(node) != common | {"text"}:
            _invalid()
        return {
            "id": node_id,
            "type": "text",
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "text": require_utf8(node.get("text"), 0, BODY_BYTES),
        }
    if node_type == "file":
        if set(node) != common | {"file"}:
            _invalid()
        file_ref = require_utf8(node.get("file"), 1, 512)
        if not _is_allowed_canvas_file_ref(file_ref):
            _invalid()
        return {
            "id": node_id,
            "type": "file",
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "file": file_ref,
        }
    if node_type == "group":
        allowed = common | ({"label"} if "label" in node else set())
        if set(node) != allowed:
            _invalid()
        parsed = {
            "id": node_id,
            "type": "group",
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        }
        if "label" in node:
            parsed["label"] = require_utf8(node.get("label"), 0, BODY_BYTES)
        return parsed
    _invalid()
    raise AssertionError("unreachable")


def _parse_canvas_edge(raw: object) -> dict[str, Any]:
    edge = _require_mapping(raw)
    required = {"id", "fromNode", "toNode"}
    optional = {"fromSide", "toSide", "fromEnd", "toEnd", "color", "label"}
    if not required <= set(edge) or not set(edge) <= required | optional:
        _invalid()
    parsed = {
        "id": require_utf8(edge.get("id"), 1, 64),
        "fromNode": require_utf8(edge.get("fromNode"), 1, 64),
        "toNode": require_utf8(edge.get("toNode"), 1, 64),
    }
    for key, allowed in (
        ("fromSide", {"top", "right", "bottom", "left"}),
        ("toSide", {"top", "right", "bottom", "left"}),
        ("fromEnd", {"none", "arrow"}),
        ("toEnd", {"none", "arrow"}),
    ):
        if key in edge:
            value = edge[key]
            if value not in allowed:
                _invalid()
            parsed[key] = value
    if "color" in edge:
        parsed["color"] = require_utf8(edge.get("color"), 1, 64)
    if "label" in edge:
        parsed["label"] = require_utf8(edge.get("label"), 1, BODY_BYTES)
    return parsed


def parse_canvas(raw: object) -> dict[str, Any]:
    canvas = _require_mapping(raw, {"nodes", "edges"})
    nodes_raw = canvas.get("nodes")
    edges_raw = canvas.get("edges")
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
        _invalid()
    if len(nodes_raw) > 64 or len(edges_raw) > 128:
        _invalid()
    nodes = [_parse_canvas_node(node) for node in nodes_raw]
    edges = [_parse_canvas_edge(edge) for edge in edges_raw]
    node_ids: set[str] = set()
    for node in nodes:
        if node["id"] in node_ids:
            _invalid()
        node_ids.add(node["id"])
    edge_ids: set[str] = set()
    for edge in edges:
        if edge["id"] in edge_ids or edge["fromNode"] not in node_ids or edge["toNode"] not in node_ids:
            _invalid()
        edge_ids.add(edge["id"])
    parsed = {"nodes": nodes, "edges": edges}
    if _canvas_has_forbidden_ref(parsed):
        _invalid()
    encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PUBLIC_RESULT_BYTES:
        _invalid()
    return parsed


def parse_base_yaml(raw: object) -> dict[str, Any]:
    payload = _require_mapping(raw)
    allowed = {"filters", "views", "formulas"}
    if not set(payload) <= allowed:
        _invalid()
    parsed: dict[str, Any] = {}
    if "filters" in payload:
        filters = payload["filters"]
        if not isinstance(filters, dict):
            _invalid()
        parsed["filters"] = {str(key): parse_json_value(item) for key, item in filters.items()}
    if "views" in payload:
        views = payload["views"]
        if not isinstance(views, list):
            _invalid()
        parsed["views"] = [parse_json_value(item) for item in views]
    if "formulas" in payload:
        formulas = payload["formulas"]
        if not isinstance(formulas, dict):
            _invalid()
        parsed["formulas"] = {str(key): parse_json_value(item) for key, item in formulas.items()}
    encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > BODY_BYTES:
        _invalid()
    return parsed


def parse_scene_elements(raw: object) -> list[Any]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 256:
        _invalid()
    elements = [parse_json_value(item) for item in raw]
    encoded = json.dumps(elements, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PUBLIC_RESULT_BYTES:
        _invalid()
    return elements


def parse_note_patch(raw: object) -> dict[str, Any]:
    patch = _require_mapping(raw)
    target = patch.get("target")
    if target == "heading":
        if set(patch) != {"target", "heading", "body"}:
            _invalid()
        return {
            "target": "heading",
            "heading": require_heading_path(patch.get("heading")),
            "body": require_utf8(patch.get("body"), 1, BODY_BYTES),
        }
    if target == "block":
        if set(patch) != {"target", "selector", "body"}:
            _invalid()
        return {
            "target": "block",
            "selector": require_selector(patch.get("selector")),
            "body": require_utf8(patch.get("body"), 1, BODY_BYTES),
        }
    if target == "frontmatter":
        if set(patch) != {"target", "selector", "value"}:
            _invalid()
        return {
            "target": "frontmatter",
            "selector": require_selector(patch.get("selector")),
            "value": parse_json_value(patch.get("value")),
        }
    _invalid()
    raise AssertionError("unreachable")


def parse_template_variable(raw: object) -> Any:
    if isinstance(raw, list):
        if len(raw) > 16:
            _invalid()
        return [require_utf8(item, 0, 512) for item in raw]
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return require_finite_number(raw)
    return require_utf8(raw, 0, BODY_BYTES)


def parse_phase1_action(raw: object) -> dict[str, Any]:
    action = _require_mapping(raw)
    kind = action.get("kind")
    if kind in PHASE2_ONLY_ACTION_IDS or kind in FORBIDDEN_ACTION_IDS:
        _invalid()
    if kind == "create_note":
        if set(action) != {"kind", "path", "type", "para", "agent", "tags", "body"}:
            _invalid()
        tags = action.get("tags")
        if not isinstance(tags, list) or len(tags) > 16:
            _invalid()
        return {
            "kind": "create_note",
            "path": require_vault_path(action.get("path")),
            "type": require_utf8(action.get("type"), 1, 64),
            "para": require_utf8(action.get("para"), 1, 64),
            "agent": require_utf8(action.get("agent"), 1, 64),
            "tags": [require_utf8(tag, 1, 64) for tag in tags],
            "body": require_utf8(action.get("body"), 1, BODY_BYTES),
        }
    if kind == "patch_note":
        if set(action) != {"kind", "path", "ifMatch", "patch"}:
            _invalid()
        return {
            "kind": "patch_note",
            "path": require_vault_path(action.get("path")),
            "ifMatch": require_revision(action.get("ifMatch")),
            "patch": parse_note_patch(action.get("patch")),
        }
    if kind == "copy_note":
        if set(action) != {"kind", "from", "to"}:
            _invalid()
        return {
            "kind": "copy_note",
            "from": require_vault_path(action.get("from")),
            "to": require_vault_path(action.get("to")),
        }
    if kind == "move_note":
        if set(action) != {"kind", "from", "to"}:
            _invalid()
        return {
            "kind": "move_note",
            "from": require_vault_path(action.get("from")),
            "to": require_vault_path(action.get("to")),
        }
    if kind == "trash_note":
        if set(action) != {"kind", "path"}:
            _invalid()
        return {"kind": "trash_note", "path": require_vault_path(action.get("path"))}
    if kind == "create_canvas":
        if set(action) != {"kind", "path", "canvas"}:
            _invalid()
        return {
            "kind": "create_canvas",
            "path": require_vault_path(action.get("path")),
            "canvas": parse_canvas(action.get("canvas")),
        }
    if kind == "create_base":
        if set(action) != {"kind", "path", "yaml"}:
            _invalid()
        return {
            "kind": "create_base",
            "path": require_vault_path(action.get("path")),
            "yaml": parse_base_yaml(action.get("yaml")),
        }
    if kind == "append_flashcard":
        if set(action) != {"kind", "card", "ifMatch"}:
            _invalid()
        card = action.get("card")
        if not isinstance(card, str) or not FLASHCARD_RE.fullmatch(card):
            _invalid()
        return {"kind": "append_flashcard", "card": card, "ifMatch": require_revision(action.get("ifMatch"))}
    if kind == "create_excalidraw":
        allowed = {"kind", "name", "elements"}
        if "embed_path" in action:
            allowed = allowed | {"embed_path"}
        if set(action) != allowed:
            _invalid()
        name = action.get("name")
        if not isinstance(name, str) or not EXCALIDRAW_NAME_RE.fullmatch(name):
            _invalid()
        parsed = {
            "kind": "create_excalidraw",
            "name": name,
            "elements": parse_scene_elements(action.get("elements")),
        }
        if "embed_path" in action:
            parsed["embed_path"] = require_vault_path(action.get("embed_path"))
        return parsed
    if kind == "apply_template":
        allowed = {"kind", "template_id", "path", "variables"}
        if "ifMatch" in action:
            allowed = allowed | {"ifMatch"}
        if set(action) != allowed:
            _invalid()
        template_id = action.get("template_id")
        variables = action.get("variables")
        if not isinstance(template_id, str) or not 1 <= len(template_id) <= 64:
            _invalid()
        if not isinstance(variables, dict):
            _invalid()
        parsed_vars: dict[str, Any] = {}
        for key, item in variables.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 64:
                _invalid()
            parsed_vars[key] = parse_template_variable(item)
        parsed_template: dict[str, Any] = {
            "kind": "apply_template",
            "template_id": template_id,
            "path": require_vault_path(action.get("path")),
            "variables": parsed_vars,
        }
        if "ifMatch" in action:
            parsed_template["ifMatch"] = require_revision(action.get("ifMatch"))
        return parsed_template
    _invalid()
    raise AssertionError("unreachable")


def parse_read_target(raw: object) -> dict[str, Any]:
    target = _require_mapping(raw)
    kind = target.get("kind")
    if kind == "heading":
        if set(target) != {"kind", "heading"}:
            _invalid()
        return {"kind": "heading", "heading": require_heading_path(target.get("heading"))}
    if kind == "block":
        if set(target) != {"kind", "selector"}:
            _invalid()
        return {"kind": "block", "selector": require_selector(target.get("selector"))}
    if kind == "frontmatter":
        if set(target) != {"kind", "selector"}:
            _invalid()
        return {"kind": "frontmatter", "selector": require_selector(target.get("selector"))}
    if kind in {"map", "tags", "links", "backlinks"}:
        if set(target) != {"kind"}:
            _invalid()
        return {"kind": kind}
    _invalid()
    raise AssertionError("unreachable")


def parse_capabilities_input(raw: object) -> dict[str, Any]:
    _require_mapping(raw, set())
    return {}


def parse_search_input(raw: object) -> dict[str, Any]:
    payload = _require_mapping(raw)
    allowed = {"query"}
    if "limit" in payload:
        allowed = allowed | {"limit"}
    if set(payload) != allowed:
        _invalid()
    parsed = {"query": require_utf8(payload.get("query"), 1, 512), "limit": 5}
    if "limit" in payload:
        parsed["limit"] = require_int(payload.get("limit"), 1, 10)
    return parsed


def parse_read_input(raw: object) -> dict[str, Any]:
    payload = _require_mapping(raw)
    allowed = {"path"}
    if "target" in payload:
        allowed = allowed | {"target"}
    if set(payload) != allowed:
        _invalid()
    parsed: dict[str, Any] = {"path": require_vault_path(payload.get("path"))}
    if "target" in payload:
        parsed["target"] = parse_read_target(payload.get("target"))
    return parsed


def parse_action_status_input(raw: object) -> dict[str, str]:
    payload = _require_mapping(raw, {"action_id"})
    return {"action_id": require_opaque_id(payload.get("action_id"))}


def parse_propose_input(raw: object) -> dict[str, Any]:
    payload = _require_mapping(raw, {"request_id", "action"})
    return {
        "request_id": require_request_id(payload.get("request_id")),
        "action": parse_phase1_action(payload.get("action")),
    }


def parse_execute_input(raw: object) -> dict[str, str]:
    payload = _require_mapping(raw, {"action_id", "approval_receipt"})
    return {
        "action_id": require_opaque_id(payload.get("action_id")),
        "approval_receipt": require_opaque_id(payload.get("approval_receipt")),
    }


def _exact_unique_phase1_action_ids(ids: list[str]) -> bool:
    seen: set[str] = set()
    for action_id in ids:
        if action_id not in PHASE1_ACTION_IDS or action_id in seen:
            return False
        seen.add(action_id)
    return True


def parse_capabilities_result(raw: object) -> dict[str, Any]:
    payload = _require_mapping(raw, {"phase", "search_backend", "plugins", "supported_action_ids"})
    if payload.get("phase") != "phase1" or payload.get("search_backend") != "native_bounded_search":
        _invalid()
    plugins_raw = payload.get("plugins")
    supported_raw = payload.get("supported_action_ids")
    if not isinstance(plugins_raw, list) or not isinstance(supported_raw, list):
        _invalid()
    if len(plugins_raw) > 32 or len(supported_raw) > 32:
        _invalid()
    supported = [require_utf8(item, 1, 64) for item in supported_raw]
    if not _exact_unique_phase1_action_ids(supported):
        _invalid()
    plugins = []
    for plugin in plugins_raw:
        item = _require_mapping(plugin, {"id", "version", "supported_action_ids"})
        plugin_ids = item.get("supported_action_ids")
        if not isinstance(plugin_ids, list) or len(plugin_ids) > 32:
            _invalid()
        parsed_ids = [require_utf8(action_id, 1, 64) for action_id in plugin_ids]
        if not _exact_unique_phase1_action_ids(parsed_ids) or any(
            action_id not in supported for action_id in parsed_ids
        ):
            _invalid()
        plugins.append(
            {
                "id": require_utf8(item.get("id"), 1, 64),
                "version": require_utf8(item.get("version"), 1, 64),
                "supported_action_ids": parsed_ids,
            }
        )
    parsed = {
        "phase": "phase1",
        "search_backend": "native_bounded_search",
        "plugins": plugins,
        "supported_action_ids": supported,
    }
    encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PUBLIC_RESULT_BYTES:
        _invalid()
    return parsed


def parse_search_result(raw: object) -> dict[str, Any]:
    payload = _require_mapping(raw, {"backend", "results"})
    if payload.get("backend") != "native_bounded_search":
        _invalid()
    results_raw = payload.get("results")
    if not isinstance(results_raw, list) or len(results_raw) > 10:
        _invalid()
    results = []
    for hit in results_raw:
        item = _require_mapping(hit, {"path", "title", "snippet", "score", "trust"})
        if item.get("trust") != UNTRUSTED_VAULT_CONTENT:
            _invalid()
        results.append(
            {
                "path": require_vault_path(item.get("path")),
                "title": require_utf8(item.get("title"), 0, 256),
                "snippet": require_utf8(item.get("snippet"), 0, 512),
                "score": require_finite_number(item.get("score")),
                "trust": UNTRUSTED_VAULT_CONTENT,
            }
        )
    parsed = {"backend": "native_bounded_search", "results": results}
    encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PUBLIC_RESULT_BYTES:
        _invalid()
    return parsed


def parse_read_result(raw: object) -> dict[str, Any]:
    payload = _require_mapping(raw)
    allowed = {"path", "truncated", "trust"}
    if "text" in payload:
        allowed = allowed | {"text"}
    if "value" in payload:
        allowed = allowed | {"value"}
    if set(payload) != allowed:
        _invalid()
    if payload.get("trust") != UNTRUSTED_VAULT_CONTENT or not isinstance(payload.get("truncated"), bool):
        _invalid()
    parsed: dict[str, Any] = {
        "path": require_vault_path(payload.get("path")),
        "truncated": payload["truncated"],
        "trust": UNTRUSTED_VAULT_CONTENT,
    }
    if "text" in payload:
        parsed["text"] = require_utf8(payload.get("text"), 0, READ_TEXT_BYTES)
    if "value" in payload:
        parsed["value"] = parse_json_value(payload.get("value"))
    encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PUBLIC_RESULT_BYTES:
        _invalid()
    return parsed


def parse_action_status_result(raw: object) -> dict[str, Any]:
    payload = _require_mapping(raw, {"action_id", "proposal", "approval", "execution", "verification", "receipt_ref"})
    if (
        payload.get("proposal") not in PROPOSAL_STATES
        or payload.get("approval") not in APPROVAL_STATES
        or payload.get("execution") not in EXECUTION_STATES
        or payload.get("verification") not in VERIFICATION_STATES
    ):
        _invalid()
    receipt = payload.get("receipt_ref")
    parsed = {
        "action_id": require_opaque_id(payload.get("action_id")),
        "proposal": payload["proposal"],
        "approval": payload["approval"],
        "execution": payload["execution"],
        "verification": payload["verification"],
        "receipt_ref": None if receipt is None else require_opaque_id(receipt),
    }
    encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PUBLIC_RESULT_BYTES:
        _invalid()
    return parsed


def omit_undefined(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def public_result_bytes(value: object) -> int:
    return utf8_byte_length(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
