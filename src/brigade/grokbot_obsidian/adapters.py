"""Injectable native MCP and staged four-call Excalidraw adapters."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Protocol

from .content_policy import contains_executable_markdown
from .catalog import HEX64
from .contracts import (
    ERROR_MESSAGES,
    PUBLIC_RESULT_BYTES,
    REVISION_TOKEN_RE,
    ObsidianError,
    parse_json_value,
    parse_operator_action,
    parse_phase1_action,
    require_utf8,
)
from .operator_adapter import (
    CALLABLE_OPERATOR_ADAPTER_TOOLS,
    CAS_ADAPTER_TOOLS,
    PRIVATE_TOOL_RESULT_KEYS,
    PRIVATE_TOOL_RESULT_SCHEMAS,
)
from .path_policy import assert_readable, assert_static_writable, assert_writable, normalize_vault_path, policy_for_tags
from .utf8 import is_well_formed, truncate_utf8, utf8_byte_length

SEARCH_CONTEXT_LENGTH = 100
SEARCH_HITS_MAX = 32
MATCHES_MAX = 8
SNIPPET_BYTES = 512
TITLE_BYTES = 256
COMMANDS_MAX = 512
TAGS_MAX = 64
LINKS_MAX = 256
RESULT_BYTES = PUBLIC_RESULT_BYTES
OMNISEARCH_RESULT_BYTES = 65536
EXCALIDRAW_ROOT = "03 - Resources/Excalidraw"
EMBED_HEADING = ["Excalidraw"]
ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
NATIVE_MCP_TOOLS = frozenset(
    {
        "search_simple",
        "vault_read",
        "vault_get_document_map",
        "vault_write",
        "vault_patch",
        "vault_copy",
        "vault_move",
        "vault_delete",
        "command_list",
    }
)
FIXED_BROWSER = "/usr/bin/true"
MAX_OUTPUT_BYTES = 262_144
SCENE_MAX_DEPTH = 8
SCENE_MAX_ENTRIES = 256 * 256 + 256 + 16
RECEIPT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9]{8,64}$")
PATH_REF_KEYS = frozenset({"file", "url", "path", "toFile", "fromFile", "href", "src"})
NATIVE_ENVELOPE_PREFIX = (
    "---\n\nexcalidraw-plugin: parsed\ntags: [excalidraw]\n\n---\n\n"
    "# Excalidraw Data\n\n## Text Elements\n## Drawing\n```json\n"
)
NATIVE_ENVELOPE_SUFFIX = "```\n%%\n"
STATIC_VAULT_PATH_POLICY = {
    "dailyNotesFolder": "",
    "sensitivePathPrefixes": (),
    "sensitiveTags": (),
    "dashboardRoot": "01 - Projects/Dashboard.base",
    "excalidrawSuffix": ".excalidraw.md",
    "tags": (),
}


class NativeMcpClient(Protocol):
    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> object: ...

    def close(self) -> None: ...


class ExcalidrawLifecycleClient(Protocol):
    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> object: ...

    def close(self) -> None: ...


def _invalid() -> NoReturn:
    raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])


def _denied() -> NoReturn:
    raise ObsidianError("denied", ERROR_MESSAGES["denied"])


def _unavailable() -> NoReturn:
    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])


def _protocol() -> NoReturn:
    raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"])


def _conflict() -> NoReturn:
    raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])


def _not_found() -> NoReturn:
    raise ObsidianError("not_found", ERROR_MESSAGES["not_found"])


def invoke_native(work: Callable[[], Any]) -> Any:
    try:
        return work()
    except ObsidianError:
        raise
    except Exception as exc:
        raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc


def assert_safe_excalidraw_scene(value: object) -> None:
    _walk_scene(value, 0, set(), [0])


def _walk_scene(value: object, depth: int, seen: set[int], budget: list[int]) -> None:
    if isinstance(value, str):
        if not is_well_formed(value) or contains_executable_markdown(value):
            _denied()
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value or value in {float("inf"), float("-inf")}:
            _denied()
        return
    if not isinstance(value, (list, dict)):
        _denied()
    identity = id(value)
    if identity in seen or depth >= SCENE_MAX_DEPTH:
        _denied()
    seen.add(identity)
    if isinstance(value, list):
        budget[0] += len(value)
        if budget[0] > SCENE_MAX_ENTRIES:
            _denied()
        for item in value:
            _walk_scene(item, depth + 1, seen, budget)
        return
    if any(key in {"__proto__", "prototype", "constructor"} for key in value):
        _denied()
    budget[0] += len(value)
    if budget[0] > SCENE_MAX_ENTRIES:
        _denied()
    for item in value.values():
        _walk_scene(item, depth + 1, seen, budget)


def assert_safe_refs(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            assert_safe_refs(item)
        return
    if not isinstance(value, dict):
        return
    for key, field in value.items():
        if key in PATH_REF_KEYS:
            if not isinstance(field, str):
                _invalid()
            try:
                normalize_vault_path(field)
            except ObsidianError:
                _invalid()
        assert_safe_refs(field)


def _tool_text(result: object) -> str:
    if not isinstance(result, dict):
        _protocol()
    if result.get("isError") is True:
        _protocol()
    content = result.get("content")
    if not isinstance(content, list) or not content:
        _protocol()
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        _protocol()
    text = first.get("text")
    if not isinstance(text, str) or utf8_byte_length(text) > MAX_OUTPUT_BYTES:
        _protocol()
    return text


def _etag(value: object) -> str:
    if not isinstance(value, str) or not REVISION_TOKEN_RE.fullmatch(value):
        _protocol()
    return value


class NativeMcpPort:
    def __init__(self, client: NativeMcpClient, *, policy: Mapping[str, Any] | None = None):
        self.client = client
        self.policy = dict(policy or STATIC_VAULT_PATH_POLICY)

    def close(self) -> None:
        closer = getattr(self.client, "close", None)
        if callable(closer):
            closer()

    def _call(self, name: str, arguments: Mapping[str, Any] | None = None) -> object:
        if name not in NATIVE_MCP_TOOLS and name not in CALLABLE_OPERATOR_ADAPTER_TOOLS:
            _protocol()
        return invoke_native(lambda: self.client.call_tool(name, arguments or {}))

    def _policy_for(self, tags: object) -> dict[str, Any]:
        return policy_for_tags(self.policy, tags)

    def _allow_hit(self, path: str, tags: object) -> str | None:
        try:
            return assert_readable(path, self._policy_for(tags))
        except ObsidianError as exc:
            if exc.code == "denied":
                return None
            raise

    def _unwrap_json(self, raw: object) -> object:
        if isinstance(raw, dict) and "content" in raw:
            try:
                return json.loads(_tool_text(raw))
            except json.JSONDecodeError:
                _unavailable()
        return raw

    def _read_vault_file(self, path: str) -> dict[str, Any] | None:
        try:
            raw = self._call("vault_read", {"path": path})
        except ObsidianError as exc:
            if exc.code == "unavailable":
                raise
            raise
        if raw is None:
            return None
        if isinstance(raw, dict) and raw.get("missing") is True:
            return None
        if isinstance(raw, dict) and "content" in raw and "tags" not in raw and raw.get("type") == "text":
            try:
                text = _tool_text(raw)
            except ObsidianError:
                return None
            if text == f"File not found: {path}":
                return None
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                _protocol()
        if not isinstance(raw, dict):
            _protocol()
        content = raw.get("content")
        if content is None:
            content = raw.get("text")
        if content is None:
            return None
        if isinstance(content, bytes):
            text = content.decode("utf-8")
        else:
            if not isinstance(content, str):
                _protocol()
            text = content
        if utf8_byte_length(text) > RESULT_BYTES:
            _protocol()
        tags = raw.get("tags")
        if not isinstance(tags, list) or len(tags) > TAGS_MAX:
            _protocol()
        parsed_tags = [require_utf8(tag, 1, 64) for tag in tags]
        etag = raw.get("etag") or raw.get("versionOf")
        return {"content": text, "tags": parsed_tags, "etag": etag if isinstance(etag, str) else None}

    def _read_authorized_vault_file(self, path: str) -> dict[str, Any] | None:
        normalized = normalize_vault_path(path)
        file = self._read_vault_file(normalized)
        if file is None:
            return None
        assert_readable(normalized, self._policy_for(file["tags"]))
        return {"path": normalized, **file}

    def _parse_search_hit(self, item: object) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        filename = item.get("filename")
        matches = item.get("matches")
        if not isinstance(filename, str) or not isinstance(matches, list) or len(matches) > MATCHES_MAX:
            return None
        raw_score = item.get("score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            return None
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            return None
        if score != score or score in {float("inf"), float("-inf")}:
            return None
        snippet = ""
        if matches:
            first = matches[0]
            if not isinstance(first, dict):
                return None
            match = first.get("match")
            context = first.get("context")
            if not isinstance(match, dict) or not isinstance(context, str):
                return None
            start, end, source = match.get("start"), match.get("end"), match.get("source")
            if not isinstance(start, int) or isinstance(start, bool):
                return None
            if not isinstance(end, int) or isinstance(end, bool) or end < start:
                return None
            if source not in {"filename", "content"}:
                return None
            snippet, _ = truncate_utf8(context, SNIPPET_BYTES)
        return {"filename": filename, "score": score, "snippet": snippet}

    def _title_from_path(self, path: str) -> str:
        title, _ = truncate_utf8(path.rsplit("/", 1)[-1], TITLE_BYTES)
        return title

    def _read_target_arguments(self, path: str, target: Mapping[str, Any]) -> dict[str, Any]:
        kind = target.get("kind")
        if kind == "heading":
            value = target.get("heading")
        else:
            value = target.get("selector")
        return {"path": path, "targetType": kind, "target": value, "scope": "content"}

    def _patch_arguments(self, path: str, patch: Mapping[str, Any], if_match: str) -> dict[str, Any]:
        common = {"path": path, "operation": "replace", "scope": "content", "ifMatch": if_match}
        target = patch.get("target")
        if target == "heading":
            return {**common, "targetType": "heading", "target": patch["heading"], "content": patch["body"]}
        if target == "block":
            return {**common, "targetType": "block", "target": patch["selector"], "content": patch["body"]}
        return {**common, "targetType": "frontmatter", "target": patch["selector"], "value": patch["value"]}

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        raw = self._unwrap_json(self._call("search_simple", {"query": query, "contextLength": SEARCH_CONTEXT_LENGTH}))
        if not isinstance(raw, list) or len(raw) > SEARCH_HITS_MAX:
            _unavailable()
        hits = []
        for item in raw:
            parsed = self._parse_search_hit(item)
            if parsed is None:
                continue
            try:
                normalized = normalize_vault_path(parsed["filename"])
            except ObsidianError as exc:
                if exc.code == "denied":
                    continue
                raise
            if self._allow_hit(normalized, []) is None:
                continue
            file = self._read_vault_file(normalized)
            if file is None:
                continue
            if self._allow_hit(normalized, file["tags"]) is None:
                continue
            try:
                hits.append(
                    {
                        "path": require_utf8(normalized, 1, 512),
                        "title": require_utf8(self._title_from_path(normalized), 0, TITLE_BYTES),
                        "snippet": require_utf8(parsed["snippet"], 0, SNIPPET_BYTES),
                        "score": parsed["score"],
                    }
                )
            except ObsidianError:
                continue
            if len(hits) >= limit:
                break
        return hits

    def read(self, path: str) -> dict[str, Any] | None:
        try:
            file = self._read_authorized_vault_file(path)
        except ObsidianError:
            raise
        if file is None:
            return None
        data = file["content"].encode("utf-8")
        if len(data) > RESULT_BYTES:
            _protocol()
        etag = file.get("etag")
        return {"bytes": data, "etag": _etag(etag)}

    def read_target(self, path: str, target: Mapping[str, Any]) -> Any:
        authorized = self._read_authorized_vault_file(path)
        if authorized is None:
            _not_found()
        raw = self._call("vault_read", self._read_target_arguments(authorized["path"], target))
        if isinstance(raw, dict) and "content" in raw and raw.get("type") == "text":
            raw = json.loads(_tool_text(raw)) if target.get("kind") not in {"heading", "block"} else _tool_text(raw)
        if target.get("kind") in {"heading", "block"}:
            if not isinstance(raw, str):
                _protocol()
            return raw
        return parse_json_value(raw)

    def document_map(self, path: str) -> Any:
        authorized = self._read_authorized_vault_file(path)
        if authorized is None:
            _not_found()
        return parse_json_value(self._call("vault_get_document_map", {"path": authorized["path"]}))

    def tags(self, path: str) -> Any:
        file = self._read_authorized_vault_file(path)
        if file is None:
            _not_found()
        return list(file["tags"])

    def links(self, path: str) -> Any:
        raw = self._authorized_optional_field(path, "links")
        if not isinstance(raw, list) or len(raw) > LINKS_MAX:
            _protocol()
        return [require_utf8(item, 1, 512) for item in raw]

    def backlinks(self, path: str) -> Any:
        raw = self._authorized_optional_field(path, "backlinks")
        if not isinstance(raw, list) or len(raw) > LINKS_MAX:
            _protocol()
        return [require_utf8(item, 1, 512) for item in raw]

    def _authorized_optional_field(self, path: str, field: str) -> object:
        file = self._read_authorized_vault_file(path)
        if file is None:
            _not_found()
        raw = self._call("vault_read", {"path": file["path"]})
        if isinstance(raw, dict) and "content" in raw and "tags" not in raw and raw.get("type") == "text":
            try:
                raw = json.loads(_tool_text(raw))
            except json.JSONDecodeError:
                _protocol()
        return raw.get(field) if isinstance(raw, dict) else []

    def write_new(self, path: str, data: bytes) -> None:
        self._call("vault_write", {"path": path, "content": data.decode("utf-8")})

    def patch(self, path: str, patch: Mapping[str, Any], if_match: str) -> None:
        source = self._read_authorized_vault_file(path)
        if source is None:
            _not_found()
        assert_writable("patch_note", source["path"], self._policy_for(source["tags"]))
        self._call("vault_patch", self._patch_arguments(source["path"], patch, if_match))

    def copy(self, source: str, dest: str) -> None:
        self._transfer("vault_copy", "copy_note", source, dest)

    def move(self, source: str, dest: str) -> None:
        self._transfer("vault_move", "move_note", source, dest)

    def _transfer(self, tool: str, kind: str, source: str, dest: str) -> None:
        authorized = self._read_authorized_vault_file(source)
        if authorized is None:
            _not_found()
        destination = normalize_vault_path(dest)
        assert_writable(kind, authorized["path"], destination, self._policy_for(authorized["tags"]))
        existing = self._read_vault_file(destination)
        if existing is not None:
            _conflict()
        self._call(tool, {"path": authorized["path"], "destination": destination, "allowOverwrite": False})

    def trash(self, path: str) -> None:
        source = self._read_authorized_vault_file(path)
        if source is None:
            _not_found()
        assert_writable("trash_note", source["path"], self._policy_for(source["tags"]))
        self._call("vault_delete", {"path": source["path"], "permanent": False})

    def command_list(self) -> list[dict[str, str]]:
        raw = self._call("command_list", {})
        if isinstance(raw, dict) and "content" in raw:
            raw = json.loads(_tool_text(raw))
        commands = raw.get("commands") if isinstance(raw, dict) else raw
        if not isinstance(commands, list) or len(commands) > COMMANDS_MAX:
            _unavailable()
        parsed = []
        for item in commands:
            if not isinstance(item, dict):
                _unavailable()
            parsed.append({"id": require_utf8(item.get("id"), 1, 256), "name": require_utf8(item.get("name"), 1, 256)})
        return parsed

    def adapter_inventory(self) -> list[dict[str, Any]]:
        lister = getattr(self.client, "list_tools", None)
        if not callable(lister):
            _unavailable()
        raw = invoke_native(lister)
        tools = raw.get("tools") if isinstance(raw, dict) else raw
        if not isinstance(tools, list):
            _unavailable()
        return [item for item in tools if isinstance(item, dict) and item.get("name") in CAS_ADAPTER_TOOLS]

    def _replace_structured(self, name: str, path: str, expected_sha256: str, replacement_utf8: str) -> dict[str, str]:
        if name not in CAS_ADAPTER_TOOLS or not HEX64.fullmatch(expected_sha256):
            _protocol()
        raw = self._call(
            name,
            {"path": path, "expected_sha256": expected_sha256, "replacement_utf8": replacement_utf8},
        )
        try:
            payload = json.loads(_tool_text(raw))
        except json.JSONDecodeError:
            _protocol()
        if not isinstance(payload, dict) or set(payload) != set(PRIVATE_TOOL_RESULT_KEYS):
            _protocol()
        previous = payload.get("previous_sha256")
        resulting = payload.get("resulting_sha256")
        if not isinstance(previous, str) or not HEX64.fullmatch(previous):
            _protocol()
        if not isinstance(resulting, str) or not HEX64.fullmatch(resulting):
            _protocol()
        return {"previous_sha256": previous, "resulting_sha256": resulting}

    def replace_canvas(self, path: str, expected_sha256: str, replacement_utf8: str) -> dict[str, str]:
        return self._replace_structured("grokbot_replace_canvas_v1", path, expected_sha256, replacement_utf8)

    def replace_base(self, path: str, expected_sha256: str, replacement_utf8: str) -> dict[str, str]:
        return self._replace_structured("grokbot_replace_base_v1", path, expected_sha256, replacement_utf8)

    def replace_excalidraw(self, path: str, expected_sha256: str, replacement_utf8: str) -> dict[str, str]:
        return self._replace_structured("grokbot_replace_excalidraw_v1", path, expected_sha256, replacement_utf8)

    def _private_result(self, raw: object, name: str) -> dict[str, Any]:
        try:
            payload = json.loads(_tool_text(raw))
        except json.JSONDecodeError:
            _protocol()
        expected = PRIVATE_TOOL_RESULT_SCHEMAS[name]
        if not isinstance(payload, dict) or set(payload) != set(expected):
            _protocol()
        return payload

    def _private_path(self, value: object) -> str:
        if not isinstance(value, str):
            _protocol()
        try:
            return normalize_vault_path(value)
        except ObsidianError:
            _protocol()
        raise AssertionError("unreachable")

    def lint_note(self, path: str, expected_sha256: str) -> dict[str, str]:
        if not HEX64.fullmatch(expected_sha256):
            _protocol()
        requested = self._private_path(path)
        current = self._read_authorized_vault_file(requested)
        if current is None:
            _protocol()
        if hashlib.sha256(current["content"].encode("utf-8")).hexdigest() != expected_sha256:
            _conflict()
        raw = self._call("grokbot_lint_note_v1", {"path": requested, "expected_sha256": expected_sha256})
        payload = self._private_result(raw, "grokbot_lint_note_v1")
        normalized = self._private_path(payload.get("path"))
        if normalized != requested:
            _protocol()
        file = self._read_authorized_vault_file(normalized)
        if file is None:
            _protocol()
        before, after = payload.get("before_sha256"), payload.get("after_sha256")
        if not isinstance(before, str) or not HEX64.fullmatch(before) or before != expected_sha256:
            _protocol()
        if not isinstance(after, str) or not HEX64.fullmatch(after):
            _protocol()
        if after != hashlib.sha256(file["content"].encode("utf-8")).hexdigest():
            _protocol()
        return {"path": normalized, "before_sha256": before, "after_sha256": after}

    def open_spaced_review(self) -> dict[str, object]:
        payload = self._private_result(self._call("grokbot_sr_open_review_v1", {}), "grokbot_sr_open_review_v1")
        if payload.get("view_id") != "review-queue" or payload.get("opened") is not True:
            _protocol()
        return {"view_id": "review-queue", "opened": True}

    def omnisearch(self, query: str, limit: int) -> list[dict[str, object]]:
        if utf8_byte_length(query) < 1 or utf8_byte_length(query) > 512 or not 1 <= limit <= 10:
            _protocol()
        payload = self._private_result(
            self._call("grokbot_omnisearch_v1", {"query": query, "limit": limit}), "grokbot_omnisearch_v1"
        )
        raw_hits = payload.get("hits")
        if not isinstance(raw_hits, list) or len(raw_hits) > SEARCH_HITS_MAX:
            _protocol()
        hits: list[dict[str, object]] = []
        for item in raw_hits:
            if not isinstance(item, dict) or set(item) != {"path", "title", "snippet", "score"}:
                _protocol()
            path_value = self._private_path(item.get("path"))
            if self._allow_hit(path_value, []) is None:
                continue
            file = self._read_vault_file(path_value)
            if file is None or self._allow_hit(path_value, file["tags"]) is None:
                continue
            title = require_utf8(item.get("title"), 0, TITLE_BYTES)
            snippet = require_utf8(item.get("snippet"), 0, SNIPPET_BYTES)
            score = item.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                _protocol()
            hits.append({"path": path_value, "title": title, "snippet": snippet, "score": float(score)})
            if len(hits) >= limit:
                break
        if (
            utf8_byte_length(json.dumps({"hits": hits}, ensure_ascii=False, separators=(",", ":")))
            > OMNISEARCH_RESULT_BYTES
        ):
            _protocol()
        return hits

    def open_excalidraw(self, path: str) -> dict[str, str]:
        requested = self._private_path(path)
        if self._read_authorized_vault_file(requested) is None:
            _protocol()
        payload = self._private_result(
            self._call("grokbot_excalidraw_open_v1", {"path": requested}), "grokbot_excalidraw_open_v1"
        )
        normalized = self._private_path(payload.get("path"))
        if normalized != requested or payload.get("view_type") != "excalidraw":
            _protocol()
        if self._read_authorized_vault_file(normalized) is None:
            _protocol()
        return {"path": normalized, "view_type": "excalidraw"}


def allowlisted_excalidraw_env(source: Mapping[str, str]) -> dict[str, str]:
    env = {key: source[key] for key in ENV_ALLOWLIST if key in source}
    env["BROWSER"] = FIXED_BROWSER
    return env


def _assert_proof(proof: Mapping[str, Any] | None, suffix: str) -> None:
    if proof is None:
        _unavailable()
    if proof.get("enabled") is not True:
        _denied()
    if proof.get("verified_suffix") != suffix:
        _denied()
    configured = proof.get("probe_receipt_sha256")
    if not isinstance(configured, str) or not RECEIPT_HASH.fullmatch(configured):
        _unavailable()
    receipt = proof.get("receipt")
    if not isinstance(receipt, (bytes, bytearray)) or not receipt:
        _unavailable()
    hashed = "sha256:" + hashlib.sha256(bytes(receipt)).hexdigest()
    if hashed != configured:
        _denied()


def _require_session_echo(result: object, session_id: str) -> None:
    text = _tool_text(result)
    if f"Session ID: {session_id}" not in text.splitlines():
        _protocol()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _existing_verified_artifact(raw: bytes, suffix: str) -> None:
    if suffix == ".excalidraw.md":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _invalid()
        if not text.startswith(NATIVE_ENVELOPE_PREFIX) or not text.endswith(NATIVE_ENVELOPE_SUFFIX):
            _invalid()
        inner = text[len(NATIVE_ENVELOPE_PREFIX) : len(text) - len(NATIVE_ENVELOPE_SUFFIX)]
        _parse_staged_envelope(inner.encode("utf-8"))
        return
    _parse_staged_envelope(raw)


def _parse_staged_envelope(raw: bytes) -> None:
    if not raw or len(raw) > MAX_OUTPUT_BYTES:
        _invalid()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _invalid()
    if not isinstance(parsed, dict) or set(parsed) != {"elements", "appState", "version"}:
        _invalid()
    if not isinstance(parsed["elements"], list) or not parsed["elements"]:
        _invalid()
    if not isinstance(parsed["appState"], dict):
        _invalid()
    if not isinstance(parsed["version"], (int, float)) or parsed["version"] != parsed["version"]:
        _invalid()
    assert_safe_excalidraw_scene(parsed)
    assert_safe_refs(parsed)


def _wrap_native_markdown(staged: bytes) -> bytes:
    text = staged.decode("utf-8")
    fence = text if text.endswith("\n") else f"{text}\n"
    return f"{NATIVE_ENVELOPE_PREFIX}{fence}{NATIVE_ENVELOPE_SUFFIX}".encode("utf-8")


class ExcalidrawAdapter:
    def __init__(
        self,
        *,
        bin_path: str,
        staging_dir: str,
        native: NativeMcpPort,
        policy: Mapping[str, Any],
        read_proof: Callable[[], Mapping[str, Any] | None],
        start_client: Callable[[Mapping[str, Any]], ExcalidrawLifecycleClient],
        read_file: Callable[[str], bytes] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        random_id: Callable[[], str] | None = None,
    ):
        self.bin_path = bin_path
        self.staging_dir = staging_dir
        self.native = native
        self.policy = policy
        self.read_proof = read_proof
        self.start_client = start_client
        self.read_file = read_file or (lambda path: Path(path).read_bytes())
        self.env = env or os.environ
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.random_id = random_id or (lambda: os.urandom(16).hex())

    def create_excalidraw(self, action: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, str]:
        parsed = parse_phase1_action(action)
        if parsed["kind"] != "create_excalidraw":
            _invalid()
        try:
            assert_safe_excalidraw_scene(parsed["elements"])
        except ObsidianError as exc:
            if exc.code == "denied":
                _invalid()
            raise
        assert_safe_refs(parsed["elements"])
        suffix = self.policy["excalidrawSuffix"]
        vault_path = f"{EXCALIDRAW_ROOT}/{parsed['name']}{suffix}"
        assert_static_writable("create_excalidraw", vault_path, self.policy)
        embed_if_match = None
        if parsed.get("embed_path") is not None:
            assert_static_writable("patch_note", parsed["embed_path"], self.policy)
            match = None if context is None else context.get("embedIfMatch")
            if not isinstance(match, str) or not match:
                _invalid()
            embed_if_match = match
        _assert_proof(self.read_proof(), suffix)
        if not self.bin_path.startswith("/") or not self.staging_dir.startswith("/"):
            _unavailable()
        if self.native.read(vault_path) is not None:
            _conflict()
        session_id = self.random_id()
        if not SESSION_ID.fullmatch(session_id):
            _protocol()
        stamp = self.clock().isoformat().replace("-", "").replace(":", "").replace(".", "")
        staging_base = f"{self.staging_dir}/excalidraw-{stamp}-{session_id}"
        spec = {
            "command": self.bin_path,
            "args": (),
            "cwd": self.staging_dir,
            "shell": False,
            "timeoutMs": 45_000,
            "maxOutputBytes": MAX_OUTPUT_BYTES,
            "env": allowlisted_excalidraw_env(self.env),
        }
        client = None
        try:
            try:
                client = self.start_client(spec)
            except ObsidianError:
                raise
            except Exception as exc:
                raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
            calls = (
                ("start_session", {"sessionId": session_id}),
                ("create_diagram", {"sessionId": session_id}),
                ("add_elements", {"sessionId": session_id, "elements": parsed["elements"]}),
                ("export_diagram", {"path": staging_base, "format": "json", "sessionId": session_id}),
            )
            for name, arguments in calls:
                try:
                    result = client.call_tool(name, arguments)
                except ObsidianError:
                    raise
                except Exception as exc:
                    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
                if name in {"start_session", "create_diagram"}:
                    _require_session_echo(result, session_id)
                else:
                    _tool_text(result)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        try:
            staged = self.read_file(f"{staging_base}.json")
        except ObsidianError:
            raise
        except Exception as exc:
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"]) from exc
        _parse_staged_envelope(staged)
        artifact = _wrap_native_markdown(staged)
        if len(artifact) > MAX_OUTPUT_BYTES:
            _invalid()
        invoke_native(lambda: self.native.write_new(vault_path, artifact))
        got = self.native.read(vault_path)
        if got is None or got["bytes"] != artifact:
            return {"outcome": "unverified"}
        embed_path = parsed.get("embed_path")
        if embed_path is None or embed_if_match is None:
            return {"outcome": "verified"}
        try:
            existing = self.native.read_target(embed_path, {"kind": "heading", "heading": list(EMBED_HEADING)})
            if not isinstance(existing, str):
                return {"outcome": "unverified"}
            link = f"![[{vault_path}]]"
            if link in existing.splitlines():
                expected = existing
            else:
                prefix = existing if existing.endswith("\n") or existing == "" else f"{existing}\n"
                expected = f"{prefix}{link}\n"
                invoke_native(
                    lambda: self.native.patch(
                        embed_path,
                        {"target": "heading", "heading": list(EMBED_HEADING), "body": expected},
                        embed_if_match,
                    )
                )
            actual = self.native.read_target(embed_path, {"kind": "heading", "heading": list(EMBED_HEADING)})
            return {"outcome": "verified" if actual == expected else "unverified"}
        except Exception:
            return {"outcome": "unverified"}

    def update_excalidraw(self, action: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, str]:
        parsed = parse_operator_action(action)
        if parsed["kind"] != "update_excalidraw":
            _invalid()
        try:
            assert_safe_excalidraw_scene(parsed["elements"])
        except ObsidianError as exc:
            if exc.code == "denied":
                _invalid()
            raise
        assert_safe_refs(parsed["elements"])
        suffix = self.policy["excalidrawSuffix"]
        vault_path = parsed["path"]
        assert_static_writable("update_excalidraw", vault_path, self.policy)
        embed_if_match = None
        if parsed.get("embed_path") is not None:
            assert_static_writable("patch_note", parsed["embed_path"], self.policy)
            match = None if context is None else context.get("embedIfMatch")
            if not isinstance(match, str) or not match:
                _invalid()
            embed_if_match = match
        _assert_proof(self.read_proof(), suffix)
        existing = self.native.read(vault_path)
        if existing is None:
            _not_found()
        if existing["etag"] != parsed["ifMatch"]:
            _conflict()
        try:
            _existing_verified_artifact(existing["bytes"], suffix)
        except ObsidianError:
            _invalid()
        expected_sha = _sha256_hex(existing["bytes"])
        if not self.bin_path.startswith("/") or not self.staging_dir.startswith("/"):
            _unavailable()
        session_id = self.random_id()
        if not SESSION_ID.fullmatch(session_id):
            _protocol()
        stamp = self.clock().isoformat().replace("-", "").replace(":", "").replace(".", "")
        staging_base = f"{self.staging_dir}/excalidraw-{stamp}-{session_id}"
        spec = {
            "command": self.bin_path,
            "args": (),
            "cwd": self.staging_dir,
            "shell": False,
            "timeoutMs": 45_000,
            "maxOutputBytes": MAX_OUTPUT_BYTES,
            "env": allowlisted_excalidraw_env(self.env),
        }
        client = None
        try:
            try:
                client = self.start_client(spec)
            except ObsidianError:
                raise
            except Exception as exc:
                raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
            calls = (
                ("start_session", {"sessionId": session_id}),
                ("create_diagram", {"sessionId": session_id}),
                ("add_elements", {"sessionId": session_id, "elements": parsed["elements"]}),
                ("export_diagram", {"path": staging_base, "format": "json", "sessionId": session_id}),
            )
            for name, arguments in calls:
                try:
                    result = client.call_tool(name, arguments)
                except ObsidianError:
                    raise
                except Exception as exc:
                    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
                if name in {"start_session", "create_diagram"}:
                    _require_session_echo(result, session_id)
                else:
                    _tool_text(result)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        try:
            staged = self.read_file(f"{staging_base}.json")
        except ObsidianError:
            raise
        except Exception as exc:
            raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"]) from exc
        _parse_staged_envelope(staged)
        artifact = _wrap_native_markdown(staged) if suffix == ".excalidraw.md" else staged
        if len(artifact) > MAX_OUTPUT_BYTES:
            _invalid()
        from .native_client import assert_outbound_tools_call_fits

        replacement = artifact.decode("utf-8")
        assert_outbound_tools_call_fits(
            "grokbot_replace_excalidraw_v1",
            {
                "expected_sha256": expected_sha,
                "path": vault_path,
                "replacement_utf8": replacement,
            },
        )
        try:
            replaced = self.native.replace_excalidraw(vault_path, expected_sha, replacement)
        except Exception:
            return {"outcome": "unverified"}
        got = self.native.read(vault_path)
        if got is None or _sha256_hex(got["bytes"]) != replaced["resulting_sha256"] or got["bytes"] != artifact:
            return {"outcome": "unverified"}
        embed_path = parsed.get("embed_path")
        if embed_path is None or embed_if_match is None:
            return {"outcome": "verified"}
        try:
            existing_embed = self.native.read_target(embed_path, {"kind": "heading", "heading": list(EMBED_HEADING)})
            if not isinstance(existing_embed, str):
                return {"outcome": "unverified"}
            link = f"![[{vault_path}]]"
            if link in existing_embed.splitlines():
                expected = existing_embed
            else:
                prefix = (
                    existing_embed if existing_embed.endswith("\n") or existing_embed == "" else f"{existing_embed}\n"
                )
                expected = f"{prefix}{link}\n"
                invoke_native(
                    lambda: self.native.patch(
                        embed_path,
                        {"target": "heading", "heading": list(EMBED_HEADING), "body": expected},
                        embed_if_match,
                    )
                )
            actual = self.native.read_target(embed_path, {"kind": "heading", "heading": list(EMBED_HEADING)})
            return {"outcome": "verified" if actual == expected else "unverified"}
        except Exception:
            return {"outcome": "unverified"}
