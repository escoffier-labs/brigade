"""Phase 1 note, canvas, base, and template executors."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, NoReturn

from .adapters import NativeMcpPort, invoke_native
from .content_policy import assert_safe_markdown
from .contracts import ERROR_MESSAGES, ObsidianError, parse_operator_action, parse_phase1_action
from .native_client import assert_outbound_tools_call_fits, serialize_structured_replacement
from .path_policy import assert_static_writable

EXCALIDRAW_ROOT = "03 - Resources/Excalidraw"


def _conflict() -> NoReturn:
    raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])


def _not_found() -> NoReturn:
    raise ObsidianError("not_found", ERROR_MESSAGES["not_found"])


def _protocol() -> NoReturn:
    raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"])


def _quote_yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_note_bytes(action: Mapping[str, Any], created: str) -> bytes:
    tags = action["tags"]
    tag_block = (
        "tags: []" if not tags else "tags:\n" + "".join(f"  - {_quote_yaml(tag)}\n" for tag in tags).rstrip("\n")
    )
    raw = (
        "---\n"
        f"type: {_quote_yaml(action['type'])}\n"
        f"para: {_quote_yaml(action['para'])}\n"
        f"agent: {_quote_yaml(action['agent'])}\n"
        f"{tag_block}\n"
        f"created: {_quote_yaml(created)}\n"
        "---\n\n"
        f"{action['body']}"
    )
    if not raw.endswith("\n"):
        raw += "\n"
    return raw.encode("utf-8")


def _same_json(left: object, right: object) -> bool:
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":")
    )


def _require_absent(native: NativeMcpPort, path: str) -> None:
    if native.read(path) is not None:
        _conflict()


def _read_existing(native: NativeMcpPort, path: str) -> dict[str, Any]:
    got = native.read(path)
    if got is None:
        _not_found()
    return got


def _verify_bytes(native: NativeMcpPort, path: str, expected: bytes) -> dict[str, str]:
    try:
        got = native.read(path)
        if got is None or got["bytes"] != expected:
            return {"outcome": "unverified"}
        return {"outcome": "verified"}
    except Exception:
        return {"outcome": "unverified"}


def _deny_sensitive_tags(tags: list[str], policy: Mapping[str, Any]) -> None:
    sensitive = set(policy.get("sensitiveTags", ()))
    if any(tag in sensitive for tag in tags):
        raise ObsidianError("denied", ERROR_MESSAGES["denied"])


class ObsidianExecutor:
    def __init__(
        self,
        *,
        native: NativeMcpPort,
        policy: Mapping[str, Any],
        flashcard_note: str,
        flashcard_heading: str,
        templates: list[Mapping[str, Any]] | None = None,
        excalidraw: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.native = native
        self.policy = policy
        self.flashcard_note = flashcard_note
        self.flashcard_heading = flashcard_heading
        self.templates = list(templates or [])
        self.excalidraw = excalidraw
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create_note(self, action: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "create_note":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_safe_markdown(parsed)
        assert_static_writable("create_note", parsed["path"], self.policy)
        _deny_sensitive_tags(parsed["tags"], self.policy)
        _require_absent(self.native, parsed["path"])
        data = create_note_bytes(parsed, self.clock().isoformat().replace("+00:00", "Z"))
        invoke_native(lambda: self.native.write_new(parsed["path"], data))
        return _verify_bytes(self.native, parsed["path"], data)

    def patch_note(self, action: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "patch_note":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_safe_markdown(parsed)
        assert_static_writable("patch_note", parsed["path"], self.policy)
        patch = parsed["patch"]
        invoke_native(lambda: self.native.patch(parsed["path"], patch, parsed["ifMatch"]))
        try:
            if patch["target"] == "heading":
                actual = self.native.read_target(parsed["path"], {"kind": "heading", "heading": patch["heading"]})
                return {"outcome": "verified" if actual == patch["body"] else "unverified"}
            if patch["target"] == "block":
                actual = self.native.read_target(parsed["path"], {"kind": "block", "selector": patch["selector"]})
                return {"outcome": "verified" if actual == patch["body"] else "unverified"}
            actual = self.native.read_target(parsed["path"], {"kind": "frontmatter", "selector": patch["selector"]})
            return {"outcome": "verified" if _same_json(actual, patch["value"]) else "unverified"}
        except Exception:
            return {"outcome": "unverified"}

    def copy_note(self, action: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "copy_note":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_static_writable("copy_note", parsed["from"], parsed["to"], self.policy)
        source = _read_existing(self.native, parsed["from"])
        _require_absent(self.native, parsed["to"])
        invoke_native(lambda: self.native.copy(parsed["from"], parsed["to"]))
        dest = _verify_bytes(self.native, parsed["to"], source["bytes"])
        if dest["outcome"] != "verified":
            return dest
        return _verify_bytes(self.native, parsed["from"], source["bytes"])

    def move_note(self, action: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "move_note":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_static_writable("move_note", parsed["from"], parsed["to"], self.policy)
        source = _read_existing(self.native, parsed["from"])
        _require_absent(self.native, parsed["to"])
        invoke_native(lambda: self.native.move(parsed["from"], parsed["to"]))
        dest = _verify_bytes(self.native, parsed["to"], source["bytes"])
        if dest["outcome"] != "verified":
            return dest
        try:
            if self.native.read(parsed["from"]) is not None:
                return {"outcome": "unverified"}
        except Exception:
            return {"outcome": "unverified"}
        return {"outcome": "verified"}

    def trash_note(self, action: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "trash_note":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_static_writable("trash_note", parsed["path"], self.policy)
        _read_existing(self.native, parsed["path"])
        invoke_native(lambda: self.native.trash(parsed["path"]))
        try:
            return {"outcome": "verified" if self.native.read(parsed["path"]) is None else "unverified"}
        except Exception:
            return {"outcome": "unverified"}

    def append_flashcard(self, action: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "append_flashcard":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_safe_markdown(parsed)
        assert_safe_markdown(self.flashcard_heading)
        assert_static_writable("append_flashcard", self.flashcard_note, self.policy)
        heading = [self.flashcard_heading]
        current_file = _read_existing(self.native, self.flashcard_note)
        if current_file["etag"] != parsed["ifMatch"]:
            _conflict()
        current = invoke_native(
            lambda: self.native.read_target(self.flashcard_note, {"kind": "heading", "heading": heading})
        )
        if not isinstance(current, str):
            _protocol()
        if parsed["card"] in current.splitlines():
            _conflict()
        prefix = current if current.endswith("\n") or current == "" else f"{current}\n"
        body = f"{prefix}{parsed['card']}\n"
        invoke_native(
            lambda: self.native.patch(
                self.flashcard_note,
                {"target": "heading", "heading": heading, "body": body},
                parsed["ifMatch"],
            )
        )
        try:
            actual = self.native.read_target(self.flashcard_note, {"kind": "heading", "heading": heading})
            return {"outcome": "verified" if actual == body else "unverified"}
        except Exception:
            return {"outcome": "unverified"}

    def create_canvas(self, action: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "create_canvas":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_static_writable("create_canvas", parsed["path"], self.policy)
        _require_absent(self.native, parsed["path"])
        data = (json.dumps(parsed["canvas"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        invoke_native(lambda: self.native.write_new(parsed["path"], data))
        return _verify_bytes(self.native, parsed["path"], data)

    def create_base(self, action: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "create_base":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_static_writable("create_base", parsed["path"], self.policy)
        _require_absent(self.native, parsed["path"])
        data = (json.dumps(parsed["yaml"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        invoke_native(lambda: self.native.write_new(parsed["path"], data))
        return _verify_bytes(self.native, parsed["path"], data)

    def apply_template(self, action: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "apply_template":
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        template = context.get("template")
        if not isinstance(template, Mapping):
            _protocol()
        assert_safe_markdown(template.get("body"))
        assert_static_writable("apply_template", parsed["path"], self.policy)
        body = str(template["body"])
        for key, value in parsed["variables"].items():
            rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            body = body.replace("{{" + key + "}}", rendered)
        assert_safe_markdown(body)
        data = (body if body.endswith("\n") else f"{body}\n").encode("utf-8")
        if parsed.get("ifMatch") is None:
            _require_absent(self.native, parsed["path"])
            invoke_native(lambda: self.native.write_new(parsed["path"], data))
            return _verify_bytes(self.native, parsed["path"], data)
        invoke_native(
            lambda: self.native.patch(
                parsed["path"],
                {"target": "heading", "heading": ["Template"], "body": body},
                parsed["ifMatch"],
            )
        )
        try:
            actual = self.native.read_target(parsed["path"], {"kind": "heading", "heading": ["Template"]})
            return {"outcome": "verified" if actual == body else "unverified"}
        except Exception:
            return {"outcome": "unverified"}

    def create_excalidraw(self, action: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, str]:
        if self.excalidraw is None:
            raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"])
        return self.excalidraw.create_excalidraw(action, context)

    def _replace_structured(self, action: Mapping[str, Any], kind: str, payload_key: str, replacer) -> dict[str, str]:
        parsed = parse_operator_action(action)
        if parsed["kind"] != kind:
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])
        assert_static_writable(kind, parsed["path"], self.policy)
        current = _read_existing(self.native, parsed["path"])
        if current["etag"] != parsed["ifMatch"]:
            _conflict()
        expected = hashlib.sha256(current["bytes"]).hexdigest()
        replacement = serialize_structured_replacement(parsed[payload_key])
        data = replacement.encode("utf-8")
        name = "grokbot_replace_canvas_v1" if kind == "patch_canvas" else "grokbot_replace_base_v1"
        assert_outbound_tools_call_fits(
            name,
            {
                "expected_sha256": expected,
                "path": parsed["path"],
                "replacement_utf8": replacement,
            },
        )
        invoke_native(lambda: replacer(parsed["path"], expected, replacement))
        return _verify_bytes(self.native, parsed["path"], data)

    def patch_canvas(self, action: Mapping[str, Any]) -> dict[str, str]:
        return self._replace_structured(action, "patch_canvas", "canvas", self.native.replace_canvas)

    def patch_base(self, action: Mapping[str, Any]) -> dict[str, str]:
        return self._replace_structured(action, "patch_base", "yaml", self.native.replace_base)

    def update_excalidraw(self, action: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, str]:
        if self.excalidraw is None:
            raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"])
        return self.excalidraw.update_excalidraw(action, context)
