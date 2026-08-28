"""Phase 2 atomic structured-file adapter contracts and gateway execution."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade.grokbot_obsidian.actions import ObsidianExecutor
from brigade.grokbot_obsidian.adapters import (
    NATIVE_ENVELOPE_PREFIX,
    NATIVE_ENVELOPE_SUFFIX,
    ExcalidrawAdapter,
    NativeMcpPort,
)
from brigade.grokbot_obsidian.capabilities import (
    hash_capabilities_fingerprint,
    project_phase1,
    reconcile_capabilities,
)
from brigade.grokbot_obsidian.catalog import get_catalog_row, hash_action_content
from brigade.grokbot_obsidian.contracts import (
    ERROR_MESSAGES,
    PHASE2_ONLY_ACTION_IDS,
    TOOLS,
    ObsidianError,
    parse_capabilities_result,
    parse_operator_action,
    parse_phase1_action,
    parse_propose_input,
)
from brigade.grokbot_obsidian import native_client
from brigade.grokbot_obsidian.native_client import (
    MAX_JSONRPC_ENVELOPE_BYTES,
    MAX_OUTBOUND_HARD_CEILING_BYTES,
    MAX_OUTBOUND_REQUEST_BYTES,
    MAX_REPLACEMENT_BYTES,
    StreamableNativeMcpClient,
    encode_jsonrpc,
    outbound_tools_call_size,
    tools_call_payload,
)
from brigade.grokbot_obsidian.operator_adapter import (
    ADAPTER_MANIFEST_VERSION,
    CAS_ADAPTER_TOOLS,
    CALLABLE_OPERATOR_ADAPTER_TOOLS,
    OPERATOR_ADAPTER_TOOLS,
    PRIVATE_TOOL_DESCRIPTIONS,
    PRIVATE_TOOL_INPUT_SCHEMAS,
    PRIVATE_TOOL_RESULT_SCHEMAS,
    expected_private_tool_fingerprint,
    hash_private_tool_fingerprint,
)
from brigade.grokbot_obsidian.store import write_approval_file
from brigade.grokbot_obsidian.tools import ObsidianTools
from tests.test_grokbot_obsidian_actions import ACTION_ID, NONCE, POLICY, RECEIPT, REQUEST, MemoryVault, _ids, _store
from tests.test_grokbot_obsidian_adapters import ScriptedClient, ScriptedExcalidraw, _vault_file
from tests.test_grokbot_obsidian_client import UPSTREAM_KEY, UPSTREAM_URL, _scripted_fetch

IF_MATCH = "aaaaaa"
CANVAS_PATH = "01 - Projects/Board.canvas"
BASE_PATH = "01 - Projects/Dashboard.base"
DRAWING_PATH = "03 - Resources/Excalidraw/scene.excalidraw.md"
CANVAS = {"nodes": [{"id": "n", "type": "text", "x": 0, "y": 0, "width": 10, "height": 10, "text": "ok"}], "edges": []}
BASE = {"filters": {"status": "open"}}
ELEMENTS = [{"id": "e1", "type": "rectangle"}]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canvas_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _envelope(elements: list) -> bytes:
    staged = json.dumps({"elements": elements, "appState": {}, "version": 2}, ensure_ascii=False) + "\n"
    return f"{NATIVE_ENVELOPE_PREFIX}{staged}{NATIVE_ENVELOPE_SUFFIX}".encode("utf-8")


def test_phase2_action_schemas_are_strict_and_keep_six_hex_ifmatch():
    canvas = parse_operator_action({"kind": "patch_canvas", "path": CANVAS_PATH, "canvas": CANVAS, "ifMatch": IF_MATCH})
    assert canvas["ifMatch"] == IF_MATCH
    base = parse_operator_action({"kind": "patch_base", "path": BASE_PATH, "yaml": BASE, "ifMatch": IF_MATCH})
    assert base["yaml"] == BASE
    drawing = parse_operator_action(
        {
            "kind": "update_excalidraw",
            "path": DRAWING_PATH,
            "elements": ELEMENTS,
            "ifMatch": IF_MATCH,
            "embed_path": "01 - Projects/note.md",
        }
    )
    assert drawing["embed_path"] == "01 - Projects/note.md"
    with pytest.raises(ObsidianError):
        parse_operator_action({"kind": "patch_canvas", "path": CANVAS_PATH, "canvas": CANVAS, "ifMatch": "ABCDEF"})
    with pytest.raises(ObsidianError):
        parse_phase1_action({"kind": "patch_canvas", "path": CANVAS_PATH, "canvas": CANVAS, "ifMatch": IF_MATCH})
    parsed = parse_propose_input(
        {
            "request_id": "req-phase2",
            "action": {"kind": "patch_canvas", "path": CANVAS_PATH, "canvas": CANVAS, "ifMatch": IF_MATCH},
        }
    )
    assert parsed["action"]["kind"] == "patch_canvas"
    digest = hash_action_content({"request_id": "req-phase2", "action": parsed["action"]})
    assert len(digest) == 64
    assert get_catalog_row("patch_canvas")["phase"] == "2"
    assert get_catalog_row("patch_base")["kind"] == "patch_base"
    assert get_catalog_row("update_excalidraw")["kind"] == "update_excalidraw"


def _phase2_tools(*, description_version: str = ADAPTER_MANIFEST_VERSION) -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "description": (
                PRIVATE_TOOL_DESCRIPTIONS[name]
                if description_version == ADAPTER_MANIFEST_VERSION
                else PRIVATE_TOOL_DESCRIPTIONS[name].replace(ADAPTER_MANIFEST_VERSION, description_version)
            ),
            "inputSchema": {
                "type": "object",
                "properties": {key: {} for key in PRIVATE_TOOL_INPUT_SCHEMAS[name]},
            },
        }
        for name in sorted(CAS_ADAPTER_TOOLS)
    ]


def _phase2_runtime(plugins, fingerprint):
    return {
        "plugin_inventory": plugins,
        "command_fingerprint": fingerprint,
        "operator_adapter": {
            "manifest_version": ADAPTER_MANIFEST_VERSION,
            "tool_fingerprint": expected_private_tool_fingerprint(),
        },
    }


def test_capabilities_demote_without_adapter_and_promote_on_fingerprint_match():
    plugins = [{"id": "core", "version": "1.0.0", "supported_action_ids": ["create_note"]}]
    commands = [{"id": "app:reload", "name": "Reload"}]
    fingerprint = hash_capabilities_fingerprint({"version": 1, "plugins": plugins, "commands": commands})
    tools = _phase2_tools()
    expected = hash_private_tool_fingerprint(tools)
    assert expected == expected_private_tool_fingerprint()
    phase1 = reconcile_capabilities(
        {"plugin_inventory": plugins, "command_fingerprint": fingerprint},
        lambda: commands,
        adapter_tools=lambda: tools,
    )
    assert phase1["capabilities"]["phase"] == "phase1"
    assert "patch_canvas" not in phase1["capabilities"]["supported_action_ids"]
    promoted = reconcile_capabilities(
        _phase2_runtime(plugins, fingerprint),
        lambda: commands,
        adapter_tools=lambda: tools,
    )
    assert promoted["capabilities"]["phase"] == "phase1"
    assert PHASE2_ONLY_ACTION_IDS <= set(promoted["capabilities"]["supported_action_ids"])
    drifted = reconcile_capabilities(
        {
            "plugin_inventory": plugins,
            "command_fingerprint": fingerprint,
            "operator_adapter": {
                "manifest_version": ADAPTER_MANIFEST_VERSION,
                "tool_fingerprint": "sha256:" + ("a" * 64),
            },
        },
        lambda: commands,
        adapter_tools=lambda: tools,
    )
    assert "patch_canvas" not in drifted["capabilities"]["supported_action_ids"]
    projected = project_phase1(plugins)
    assert "update_excalidraw" not in projected["supported_action_ids"]
    parsed = parse_capabilities_result(promoted["capabilities"])
    assert "patch_base" in parsed["supported_action_ids"]


def test_native_allowlist_includes_only_fixed_private_adapter_tools():
    calls: list[dict[str, object]] = []
    client = StreamableNativeMcpClient(url=UPSTREAM_URL, api_key=UPSTREAM_KEY, fetch=_scripted_fetch(calls))
    client.call_tool(
        "grokbot_replace_canvas_v1",
        {"path": CANVAS_PATH, "expected_sha256": "a" * 64, "replacement_utf8": "{}\n"},
    )
    with pytest.raises(ObsidianError) as caught:
        client.call_tool("command_execute", {})
    assert caught.value.code == "protocol_error"
    with pytest.raises(ObsidianError):
        client.call_tool("grokbot_replace_note_v1", {})
    assert OPERATOR_ADAPTER_TOOLS == {
        "grokbot_replace_canvas_v1",
        "grokbot_replace_base_v1",
        "grokbot_replace_excalidraw_v1",
        "grokbot_lint_note_v1",
        "grokbot_auto_move_note_v1",
        "grokbot_sr_open_review_v1",
        "grokbot_homepage_open_v1",
        "grokbot_omnisearch_v1",
        "grokbot_excalidraw_open_v1",
        "grokbot_excalidraw_export_v1",
    }
    assert "vault_write" not in OPERATOR_ADAPTER_TOOLS
    assert CALLABLE_OPERATOR_ADAPTER_TOOLS == CAS_ADAPTER_TOOLS | {
        "grokbot_lint_note_v1",
        "grokbot_sr_open_review_v1",
        "grokbot_omnisearch_v1",
        "grokbot_excalidraw_open_v1",
    }
    calls_before = len(calls)
    for deferred in (
        "grokbot_auto_move_note_v1",
        "grokbot_homepage_open_v1",
        "grokbot_excalidraw_export_v1",
    ):
        with pytest.raises(ObsidianError) as rejected:
            client.call_tool(deferred, {})
        assert rejected.value.code == "protocol_error"
    assert len(calls) == calls_before
    assert not hasattr(NativeMcpPort, "auto_move_note")
    assert not hasattr(NativeMcpPort, "open_homepage")
    assert not hasattr(NativeMcpPort, "export_excalidraw")
    assert not (CALLABLE_OPERATOR_ADAPTER_TOOLS & TOOLS)


def test_private_adapter_inventory_pins_each_tool_schema_and_result_shape():
    tools = _phase2_tools()
    assert hash_private_tool_fingerprint(tools) == expected_private_tool_fingerprint()
    assert PRIVATE_TOOL_INPUT_SCHEMAS["grokbot_lint_note_v1"] == ("expected_sha256", "path")
    assert PRIVATE_TOOL_INPUT_SCHEMAS["grokbot_auto_move_note_v1"] == ("expected_sha256", "path")
    assert PRIVATE_TOOL_INPUT_SCHEMAS["grokbot_sr_open_review_v1"] == ()
    assert PRIVATE_TOOL_INPUT_SCHEMAS["grokbot_homepage_open_v1"] == ()
    assert PRIVATE_TOOL_INPUT_SCHEMAS["grokbot_omnisearch_v1"] == ("limit", "query")
    assert PRIVATE_TOOL_INPUT_SCHEMAS["grokbot_excalidraw_open_v1"] == ("path",)
    assert PRIVATE_TOOL_INPUT_SCHEMAS["grokbot_excalidraw_export_v1"] == ("format", "path")
    assert PRIVATE_TOOL_RESULT_SCHEMAS["grokbot_lint_note_v1"] == ("after_sha256", "before_sha256", "path")
    assert PRIVATE_TOOL_RESULT_SCHEMAS["grokbot_auto_move_note_v1"] == (
        "content_sha256",
        "destination_path",
        "source_path",
    )
    assert PRIVATE_TOOL_RESULT_SCHEMAS["grokbot_sr_open_review_v1"] == ("opened", "view_id")
    assert PRIVATE_TOOL_RESULT_SCHEMAS["grokbot_homepage_open_v1"] == ("opened", "path")
    assert PRIVATE_TOOL_RESULT_SCHEMAS["grokbot_omnisearch_v1"] == ("hits",)
    assert PRIVATE_TOOL_RESULT_SCHEMAS["grokbot_excalidraw_open_v1"] == ("path", "view_type")
    assert PRIVATE_TOOL_RESULT_SCHEMAS["grokbot_excalidraw_export_v1"] == ("artifact_ref", "content_sha256")
    with pytest.raises(ValueError):
        hash_private_tool_fingerprint(
            tools
            + [
                {
                    "name": "grokbot_lint_note_v1",
                    "description": PRIVATE_TOOL_DESCRIPTIONS["grokbot_lint_note_v1"],
                    "inputSchema": {"type": "object", "properties": {"expected_sha256": {}, "path": {}}},
                }
            ]
        )
    drifted = _phase2_tools()
    drifted[0]["inputSchema"]["properties"]["extra"] = {}
    with pytest.raises(ValueError):
        hash_private_tool_fingerprint(drifted)


def test_native_replace_methods_parse_exact_hash_result_and_reject_unknown_tools():
    current = _canvas_bytes({"nodes": [], "edges": []})
    replacement = _canvas_bytes(CANVAS)
    client = ScriptedClient(
        {
            "grokbot_replace_canvas_v1": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"previous_sha256": _sha(current), "resulting_sha256": _sha(replacement)},
                            separators=(",", ":"),
                        ),
                    }
                ]
            }
        },
        files={CANVAS_PATH: _vault_file(current.decode("utf-8"), [], IF_MATCH)},
    )
    native = NativeMcpPort(client)
    got = native.replace_canvas(CANVAS_PATH, _sha(current), replacement.decode("utf-8"))
    assert got == {"previous_sha256": _sha(current), "resulting_sha256": _sha(replacement)}
    assert client.calls[0][0] == "grokbot_replace_canvas_v1"
    with pytest.raises(ObsidianError) as caught:
        native._call("open_file", {})
    assert caught.value.code == "protocol_error"


def test_phase2_updates_are_rejected_before_mutation_when_adapter_absent(tmp_path: Path):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    native = MemoryVault()
    native.files[CANVAS_PATH] = {"bytes": _canvas_bytes({"nodes": [], "edges": []}), "etag": IF_MATCH}
    executor = ObsidianExecutor(native=native, policy=POLICY, flashcard_note="Cards.md", flashcard_heading="Inbox")
    tools = ObsidianTools(
        native=native,
        policy=POLICY,
        store=store,
        reconcile=lambda: {
            "status": "ok",
            "capabilities": {
                "phase": "phase1",
                "search_backend": "native_bounded_search",
                "plugins": [],
                "supported_action_ids": ["create_note"],
            },
        },
        executor=executor,
        clock=lambda: now,
        random_id=_ids([ACTION_ID, NONCE, RECEIPT]).__next__,
    )
    with pytest.raises(ObsidianError) as caught:
        tools.propose_action(
            {
                "request_id": REQUEST,
                "action": {"kind": "patch_canvas", "path": CANVAS_PATH, "canvas": CANVAS, "ifMatch": IF_MATCH},
            }
        )
    assert caught.value.code == "denied"
    assert store.find_proposal_by_request_id(REQUEST) is None
    assert native.files[CANVAS_PATH]["bytes"] == _canvas_bytes({"nodes": [], "edges": []})


class RecordingNative(MemoryVault):
    def __init__(self) -> None:
        super().__init__()
        self.replace_calls: list[tuple[str, str, str]] = []

    def _replace(self, kind: str, path: str, expected_sha256: str, replacement_utf8: str) -> dict[str, str]:
        current = self.files[path]["bytes"]
        previous = _sha(current)
        if previous != expected_sha256:
            raise ObsidianError("conflict", ERROR_MESSAGES["conflict"])
        data = replacement_utf8.encode("utf-8")
        self.files[path] = {"bytes": data, "etag": "bbbbbb"}
        self.replace_calls.append((kind, path, expected_sha256))
        return {"previous_sha256": previous, "resulting_sha256": _sha(data)}

    def replace_canvas(self, path: str, expected_sha256: str, replacement_utf8: str) -> dict[str, str]:
        return self._replace("canvas", path, expected_sha256, replacement_utf8)

    def replace_base(self, path: str, expected_sha256: str, replacement_utf8: str) -> dict[str, str]:
        return self._replace("base", path, expected_sha256, replacement_utf8)

    def replace_excalidraw(self, path: str, expected_sha256: str, replacement_utf8: str) -> dict[str, str]:
        return self._replace("excalidraw", path, expected_sha256, replacement_utf8)

    def write_new(self, path: str, data: bytes) -> None:
        raise AssertionError("write_new must not run for Phase 2 updates")

    def patch(self, path: str, patch, if_match: str) -> None:
        if path.endswith(".canvas") or path.endswith(".base") or path.endswith(".excalidraw.md"):
            raise AssertionError("native patch must not replace structured files")
        super_patch = getattr(super(), "patch", None)
        if super_patch is None:
            self.files[path] = {"bytes": str(patch).encode("utf-8"), "etag": if_match}
            return
        super_patch(path, patch, if_match)


def test_patch_canvas_uses_full_sha_and_private_cas(tmp_path: Path):
    native = RecordingNative()
    current = _canvas_bytes({"nodes": [], "edges": []})
    native.files[CANVAS_PATH] = {"bytes": current, "etag": IF_MATCH}
    executor = ObsidianExecutor(native=native, policy=POLICY, flashcard_note="Cards.md", flashcard_heading="Inbox")
    result = executor.patch_canvas({"kind": "patch_canvas", "path": CANVAS_PATH, "canvas": CANVAS, "ifMatch": IF_MATCH})
    assert result["outcome"] == "verified"
    assert native.replace_calls[0][2] == _sha(current)
    assert native.files[CANVAS_PATH]["bytes"] == _canvas_bytes(CANVAS)


def test_update_excalidraw_exports_then_cas_and_rereads(tmp_path: Path):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    native = RecordingNative()
    current = _envelope([{"id": "old"}])
    native.files[DRAWING_PATH] = {"bytes": current, "etag": IF_MATCH}
    staged = tmp_path / "staging"
    staged.mkdir()
    helper = tmp_path / "excalidraw"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    session = "session01"
    client = ScriptedExcalidraw(session)

    def start(_spec):
        return client

    def read_file(path: str) -> bytes:
        return json.dumps({"elements": ELEMENTS, "appState": {}, "version": 2}, ensure_ascii=False).encode("utf-8")

    adapter = ExcalidrawAdapter(
        bin_path=str(helper),
        staging_dir=str(staged),
        native=native,
        policy=POLICY,
        read_proof=lambda: {
            "enabled": True,
            "verified_suffix": ".excalidraw.md",
            "probe_receipt_sha256": "sha256:" + hashlib.sha256(b"receipt").hexdigest(),
            "receipt": b"receipt",
        },
        start_client=start,
        read_file=read_file,
        random_id=lambda: session,
        clock=lambda: now,
    )
    executor = ObsidianExecutor(
        native=native,
        policy=POLICY,
        flashcard_note="Cards.md",
        flashcard_heading="Inbox",
        excalidraw=adapter,
    )
    result = executor.update_excalidraw(
        {"kind": "update_excalidraw", "path": DRAWING_PATH, "elements": ELEMENTS, "ifMatch": IF_MATCH}
    )
    assert result["outcome"] == "verified"
    assert client.names[:4] == ["start_session", "create_diagram", "add_elements", "export_diagram"]
    assert native.replace_calls == [("excalidraw", DRAWING_PATH, _sha(current))]
    assert native.files[DRAWING_PATH]["bytes"] == _envelope(ELEMENTS)


def test_gateway_execute_patch_base_is_verified(tmp_path: Path):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    native = RecordingNative()
    current = _canvas_bytes({"filters": {}})
    native.files[BASE_PATH] = {"bytes": current, "etag": IF_MATCH}
    executor = ObsidianExecutor(native=native, policy=POLICY, flashcard_note="Cards.md", flashcard_heading="Inbox")
    tools = ObsidianTools(
        native=native,
        policy=POLICY,
        store=store,
        reconcile=lambda: {
            "status": "ok",
            "capabilities": {
                "phase": "phase1",
                "search_backend": "native_bounded_search",
                "plugins": [],
                "supported_action_ids": ["patch_base"],
            },
        },
        executor=executor,
        clock=lambda: now,
        random_id=_ids([ACTION_ID, NONCE, "d" * 32, "e" * 32, "f" * 32, "1" * 32, "2" * 32]).__next__,
    )
    proposed = tools.propose_action(
        {
            "request_id": REQUEST,
            "action": {"kind": "patch_base", "path": BASE_PATH, "yaml": BASE, "ifMatch": IF_MATCH},
        }
    )
    proposal = store.read_proposal(proposed["action_id"])
    assert proposal is not None
    approval = {key: proposal[key] for key in proposal if key != "created_at"}
    approval["approved_at"] = proposal["created_at"]
    approval["approval_receipt"] = RECEIPT
    write_approval_file(store.approval_dir.as_posix(), approval)
    result = tools.execute_action({"action_id": proposed["action_id"], "approval_receipt": RECEIPT})
    assert result["outcome"] == "verified"
    assert native.files[BASE_PATH]["bytes"] == _canvas_bytes(BASE)


def _tools_call_arguments(path: str, replacement_utf8: str) -> dict[str, str]:
    return {
        "expected_sha256": "a" * 64,
        "path": path,
        "replacement_utf8": replacement_utf8,
    }


def _replacement_for_outbound_size(name: str, path: str, size: int, request_id: int) -> str:
    empty = _tools_call_arguments(path, "")
    base = outbound_tools_call_size(name, empty, request_id=request_id)
    pad = size - base
    assert pad >= 0
    replacement = "B" * pad
    assert outbound_tools_call_size(name, _tools_call_arguments(path, replacement), request_id=request_id) == size
    return replacement


def test_outbound_cap_covers_max_replacement_plus_envelope_below_hard_ceiling():
    assert MAX_REPLACEMENT_BYTES == 262_144
    assert MAX_JSONRPC_ENVELOPE_BYTES == 16_384
    assert MAX_OUTBOUND_REQUEST_BYTES == MAX_REPLACEMENT_BYTES + MAX_JSONRPC_ENVELOPE_BYTES
    assert MAX_OUTBOUND_REQUEST_BYTES < MAX_OUTBOUND_HARD_CEILING_BYTES
    arguments = _tools_call_arguments(DRAWING_PATH, "A" * MAX_REPLACEMENT_BYTES)
    assert outbound_tools_call_size("grokbot_replace_excalidraw_v1", arguments) <= MAX_OUTBOUND_REQUEST_BYTES


def test_streamable_client_accepts_maximum_valid_outbound_and_rejects_one_byte_over():
    name = "grokbot_replace_canvas_v1"
    path = CANVAS_PATH
    request_id = 2
    exact = _replacement_for_outbound_size(name, path, MAX_OUTBOUND_REQUEST_BYTES, request_id)
    over = exact + "X"
    assert outbound_tools_call_size(name, _tools_call_arguments(path, over), request_id=request_id) == (
        MAX_OUTBOUND_REQUEST_BYTES + 1
    )
    recorded: list[int] = []

    def fetch(url, *, method="POST", headers=None, data=None, timeout=15):
        recorded.append(len(data))
        return _scripted_fetch([])(url, method=method, headers=headers, data=data, timeout=timeout)

    accepted = StreamableNativeMcpClient(url=UPSTREAM_URL, api_key=UPSTREAM_KEY, fetch=fetch)
    accepted.call_tool(name, _tools_call_arguments(path, exact))
    assert recorded[-1] == MAX_OUTBOUND_REQUEST_BYTES
    payload = tools_call_payload(name, _tools_call_arguments(path, exact), request_id)
    assert encode_jsonrpc(payload) == json.dumps(payload, separators=(",", ":")).encode("utf-8")
    rejected = StreamableNativeMcpClient(url=UPSTREAM_URL, api_key=UPSTREAM_KEY, fetch=fetch)
    before = len(recorded)
    with pytest.raises(ObsidianError) as caught:
        rejected.call_tool(name, _tools_call_arguments(path, over))
    assert caught.value.code == "protocol_error"
    assert len(recorded) == before + 2
    assert recorded[-1] <= 512


def test_propose_rejects_unsendable_phase2_replacement_before_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(native_client, "MAX_OUTBOUND_REQUEST_BYTES", 256)
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    native = RecordingNative()
    native.files[CANVAS_PATH] = {"bytes": _canvas_bytes({"nodes": [], "edges": []}), "etag": IF_MATCH}
    executor = ObsidianExecutor(native=native, policy=POLICY, flashcard_note="Cards.md", flashcard_heading="Inbox")
    tools = ObsidianTools(
        native=native,
        policy=POLICY,
        store=store,
        reconcile=lambda: {
            "status": "ok",
            "capabilities": {
                "phase": "phase1",
                "search_backend": "native_bounded_search",
                "plugins": [],
                "supported_action_ids": ["patch_canvas"],
            },
        },
        executor=executor,
        clock=lambda: now,
        random_id=_ids([ACTION_ID, NONCE, RECEIPT]).__next__,
    )
    with pytest.raises(ObsidianError) as caught:
        tools.propose_action(
            {
                "request_id": REQUEST,
                "action": {"kind": "patch_canvas", "path": CANVAS_PATH, "canvas": CANVAS, "ifMatch": IF_MATCH},
            }
        )
    assert caught.value.code == "invalid_request"
    assert store.find_proposal_by_request_id(REQUEST) is None
    assert store.read_approval(ACTION_ID) is None
    assert native.files[CANVAS_PATH]["bytes"] == _canvas_bytes({"nodes": [], "edges": []})
    assert native.replace_calls == []


def test_stale_plugin_and_impostor_same_name_schema_demote_to_phase1():
    plugins = [{"id": "core", "version": "1.0.0", "supported_action_ids": ["create_note"]}]
    commands = [{"id": "app:reload", "name": "Reload"}]
    fingerprint = hash_capabilities_fingerprint({"version": 1, "plugins": plugins, "commands": commands})
    runtime = _phase2_runtime(plugins, fingerprint)
    stale = reconcile_capabilities(
        runtime,
        lambda: commands,
        adapter_tools=lambda: _phase2_tools(description_version="0.0.9"),
    )
    assert "patch_canvas" not in stale["capabilities"]["supported_action_ids"]
    impostor = [
        {
            "name": name,
            "description": "unrelated plugin",
            "inputSchema": {
                "type": "object",
                "properties": {"expected_sha256": {}, "path": {}, "replacement_utf8": {}},
            },
        }
        for name in sorted(OPERATOR_ADAPTER_TOOLS)
    ]
    demoted = reconcile_capabilities(runtime, lambda: commands, adapter_tools=lambda: impostor)
    assert "patch_canvas" not in demoted["capabilities"]["supported_action_ids"]
    nameless = [
        {
            "name": name,
            "inputSchema": {
                "type": "object",
                "properties": {"expected_sha256": {}, "path": {}, "replacement_utf8": {}},
            },
        }
        for name in sorted(OPERATOR_ADAPTER_TOOLS)
    ]
    missing = reconcile_capabilities(runtime, lambda: commands, adapter_tools=lambda: nameless)
    assert "update_excalidraw" not in missing["capabilities"]["supported_action_ids"]


def test_update_excalidraw_bind_rechecks_embed_revision_before_claim(tmp_path: Path):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path, clock=lambda: now)
    native = RecordingNative()
    scene = _envelope([{"id": "old"}])
    embed_path = "01 - Projects/note.md"
    native.files[DRAWING_PATH] = {"bytes": scene, "etag": IF_MATCH}
    native.files[embed_path] = {"bytes": b"# Excalidraw\n\n", "etag": "bbbbbb"}
    executor = ObsidianExecutor(native=native, policy=POLICY, flashcard_note="Cards.md", flashcard_heading="Inbox")
    tools = ObsidianTools(
        native=native,
        policy=POLICY,
        store=store,
        reconcile=lambda: {
            "status": "ok",
            "capabilities": {
                "phase": "phase1",
                "search_backend": "native_bounded_search",
                "plugins": [],
                "supported_action_ids": ["update_excalidraw"],
            },
        },
        executor=executor,
        clock=lambda: now,
        random_id=_ids([ACTION_ID, NONCE, "d" * 32, "e" * 32, "f" * 32, "1" * 32, "2" * 32]).__next__,
    )
    proposed = tools.propose_action(
        {
            "request_id": REQUEST,
            "action": {
                "kind": "update_excalidraw",
                "path": DRAWING_PATH,
                "elements": ELEMENTS,
                "ifMatch": IF_MATCH,
                "embed_path": embed_path,
            },
        }
    )
    proposal = store.read_proposal(proposed["action_id"])
    assert proposal is not None
    assert proposal["target_path"] == DRAWING_PATH
    assert proposal["target_version"] == IF_MATCH
    assert proposal["embed_path"] == embed_path
    assert proposal["embed_version"] == "bbbbbb"
    assert proposal["embed_version"] != proposal["target_version"]
    approval = {key: proposal[key] for key in proposal if key != "created_at"}
    approval["approved_at"] = proposal["created_at"]
    approval["approval_receipt"] = RECEIPT
    write_approval_file(store.approval_dir.as_posix(), approval)
    native.files[embed_path] = {"bytes": b"# Excalidraw\n\nchanged\n", "etag": "cccccc"}
    with pytest.raises(ObsidianError) as caught:
        tools.execute_action({"action_id": proposed["action_id"], "approval_receipt": RECEIPT})
    assert caught.value.code == "denied"
    assert native.replace_calls == []
    assert native.files[DRAWING_PATH]["bytes"] == scene
    assert native.files[embed_path]["bytes"] == b"# Excalidraw\n\nchanged\n"
    assert store.read_approval(proposed["action_id"]) is not None
    assert not (store.action_state_path / "consumed" / f"{proposed['action_id']}.json").exists()
