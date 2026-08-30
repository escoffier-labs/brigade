"""Native MCP adapter and staged four-call Excalidraw lifecycle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brigade.grokbot_obsidian.adapters import (
    ENV_ALLOWLIST,
    MAX_OUTPUT_BYTES,
    NATIVE_MCP_TOOLS,
    ExcalidrawAdapter,
    NativeMcpPort,
    allowlisted_excalidraw_env,
    assert_safe_excalidraw_scene,
)
from brigade.grokbot_obsidian.contracts import ObsidianError
from brigade.grokbot_obsidian.path_policy import normalize_vault_path


class ScriptedClient:
    def __init__(self, responses: dict[str, object], *, files: dict[str, object] | None = None):
        self.responses = responses
        self.files = files or {}
        self.calls: list[tuple[str, object]] = []

    def call_tool(self, name: str, arguments=None):
        payload = arguments or {}
        self.calls.append((name, payload))
        if name == "vault_read" and isinstance(payload, dict) and set(payload) == {"path"}:
            if payload["path"] in self.files:
                return self.files[payload["path"]]
            if "vault_read" not in self.responses:
                return None
        if name not in self.responses:
            raise AssertionError(name)
        return self.responses[name]

    def close(self) -> None:
        return None


SENSITIVE_POLICY = {
    "dailyNotesFolder": "",
    "sensitivePathPrefixes": (),
    "sensitiveTags": ("private",),
    "dashboardRoot": "01 - Projects/Dashboard.base",
    "excalidrawSuffix": ".excalidraw.md",
    "tags": (),
}


def _hit(filename: str, context: str, score: float = 1.0) -> dict[str, object]:
    return {
        "filename": filename,
        "score": score,
        "matches": [
            {
                "match": {"start": 0, "end": max(len(context), 0), "source": "content"},
                "context": context,
            }
        ],
    }


def _vault_file(content: str, tags: list[str], etag: str = "aaaaaa") -> dict[str, object]:
    return {"content": content, "tags": tags, "etag": etag}


def _private_result(payload: object) -> dict[str, object]:
    return {"content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}]}


class ScriptedExcalidraw:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.names: list[str] = []
        self.env = None

    def call_tool(self, name: str, arguments=None):
        self.names.append(name)
        text = f"Session ID: {self.session_id}\nok"
        return {"content": [{"type": "text", "text": text}]}

    def close(self) -> None:
        self.names.append("close")


class MemoryNative:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, object]] = {}
        self.targets: dict[str, str] = {}

    def read(self, path: str):
        got = self.files.get(path)
        return None if got is None else got

    def write_new(self, path: str, data: bytes) -> None:
        self.files[path] = {"bytes": data, "etag": "aaaaaa"}

    def read_target(self, path: str, target):
        return self.targets.get(path, "")

    def patch(self, path, patch, if_match) -> None:
        self.targets[path] = patch["body"]


def test_native_search_and_trash_never_use_permanent_delete():
    client = ScriptedClient(
        {
            "search_simple": [_hit("01 - Projects/demo.md", "hi")],
            "vault_delete": {"ok": True},
        },
        files={"01 - Projects/demo.md": _vault_file("hello", [])},
    )
    port = NativeMcpPort(client)
    hits = port.search("demo", 5)
    assert hits[0]["path"] == "01 - Projects/demo.md"
    assert hits[0]["snippet"] == "hi"
    assert client.calls[0] == ("search_simple", {"query": "demo", "contextLength": 100})
    port.trash("01 - Projects/demo.md")
    assert client.calls[-1] == ("vault_delete", {"path": "01 - Projects/demo.md", "permanent": False})
    assert "vault_write" not in {name for name, _ in client.calls}
    assert "limit" not in client.calls[0][1]


def test_search_omits_limit_and_derives_snippet_from_bounded_matches():
    client = ScriptedClient(
        {
            "search_simple": [
                _hit("01 - Projects/demo.md", "first match context"),
                {
                    "filename": "01 - Projects/extra.md",
                    "score": 0.5,
                    "matches": [
                        {
                            "match": {"start": 0, "end": 3, "source": "filename"},
                            "context": "sec",
                        },
                        {
                            "match": {"start": 4, "end": 8, "source": "content"},
                            "context": "ignored second match",
                        },
                    ],
                },
            ]
        },
        files={
            "01 - Projects/demo.md": _vault_file("hello", []),
            "01 - Projects/extra.md": _vault_file("more", []),
        },
    )
    port = NativeMcpPort(client)
    hits = port.search("demo", 2)
    assert [hit["snippet"] for hit in hits] == ["first match context", "sec"]
    assert [hit["title"] for hit in hits] == ["demo.md", "extra.md"]
    assert client.calls[0] == ("search_simple", {"query": "demo", "contextLength": 100})


def test_sensitive_tags_filter_search_and_deny_read_patch_move_trash():
    allowed = "01 - Projects/demo.md"
    tagged = "01 - Projects/secret.md"
    client = ScriptedClient(
        {
            "search_simple": [_hit(allowed, "public"), _hit(tagged, "hidden")],
            "vault_patch": {"ok": True},
            "vault_move": {"ok": True},
            "vault_delete": {"ok": True},
        },
        files={
            allowed: _vault_file("public note", []),
            tagged: _vault_file("secret note", ["private"]),
        },
    )
    port = NativeMcpPort(client, policy=SENSITIVE_POLICY)
    hits = port.search("note", 8)
    assert [hit["path"] for hit in hits] == [allowed]
    assert port.read(allowed) is not None
    with pytest.raises(ObsidianError) as read_denied:
        port.read(tagged)
    assert read_denied.value.code == "denied"
    with pytest.raises(ObsidianError) as patch_denied:
        port.patch(tagged, {"target": "heading", "heading": ["Intro"], "body": "x\n"}, "aaaaaa")
    assert patch_denied.value.code == "denied"
    with pytest.raises(ObsidianError) as move_denied:
        port.move(tagged, "04 - Archive/secret.md")
    assert move_denied.value.code == "denied"
    with pytest.raises(ObsidianError) as trash_denied:
        port.trash(tagged)
    assert trash_denied.value.code == "denied"
    assert "vault_patch" not in {name for name, _ in client.calls}
    assert "vault_move" not in {name for name, _ in client.calls}
    assert "vault_delete" not in {name for name, _ in client.calls}


def test_private_workflow_gateway_methods_use_fixed_tools_and_filter_sensitive_omnisearch_hits():
    note = "01 - Projects/note.md"
    drawing = "03 - Resources/Excalidraw/scene.excalidraw.md"
    secret = "01 - Projects/secret.md"
    note_hash = hashlib.sha256(b"note").hexdigest()
    client = ScriptedClient(
        {
            "grokbot_lint_note_v1": _private_result(
                {"path": note, "before_sha256": note_hash, "after_sha256": note_hash}
            ),
            "grokbot_sr_open_review_v1": _private_result({"view_id": "review-queue", "opened": True}),
            "grokbot_omnisearch_v1": _private_result(
                {
                    "hits": [
                        {"path": note, "title": "Note", "snippet": "allowed", "score": 1.0},
                        {"path": secret, "title": "Secret", "snippet": "hidden", "score": 0.5},
                    ]
                }
            ),
            "grokbot_excalidraw_open_v1": _private_result({"path": drawing, "view_type": "excalidraw"}),
        },
        files={
            note: _vault_file("note", []),
            secret: _vault_file("secret", ["private"]),
            drawing: _vault_file("drawing", []),
        },
    )
    port = NativeMcpPort(client, policy=SENSITIVE_POLICY)
    assert port.lint_note(note, note_hash)["after_sha256"] == note_hash
    assert port.open_spaced_review() == {"view_id": "review-queue", "opened": True}
    assert port.omnisearch("note", 10) == [{"path": note, "title": "Note", "snippet": "allowed", "score": 1.0}]
    assert port.open_excalidraw(drawing) == {"path": drawing, "view_type": "excalidraw"}
    assert [name for name, _ in client.calls if name.startswith("grokbot_")] == [
        "grokbot_lint_note_v1",
        "grokbot_sr_open_review_v1",
        "grokbot_omnisearch_v1",
        "grokbot_excalidraw_open_v1",
    ]


def test_private_path_workflows_authorize_sensitive_files_before_the_private_call():
    note = "01 - Projects/secret.md"
    drawing = "03 - Resources/Excalidraw/secret.excalidraw.md"
    client = ScriptedClient(
        {
            "grokbot_lint_note_v1": _private_result(
                {"path": note, "before_sha256": "a" * 64, "after_sha256": "a" * 64}
            ),
            "grokbot_excalidraw_open_v1": _private_result({"path": drawing, "view_type": "excalidraw"}),
        },
        files={note: _vault_file("secret", ["private"]), drawing: _vault_file("secret", ["private"])},
    )
    port = NativeMcpPort(client, policy=SENSITIVE_POLICY)
    for call in (
        lambda: port.lint_note(note, hashlib.sha256(b"secret").hexdigest()),
        lambda: port.open_excalidraw(drawing),
    ):
        with pytest.raises(ObsidianError) as caught:
            call()
        assert caught.value.code == "denied"
    assert not [name for name, _ in client.calls if name.startswith("grokbot_")]


@pytest.mark.parametrize(
    ("payload", "method"),
    [
        (
            {"path": "01 - Projects/note.md", "before_sha256": "a" * 64, "after_sha256": "b" * 64, "extra": True},
            "lint_note",
        ),
        ({"path": "01 - Projects/note.md", "before_sha256": "a" * 64}, "lint_note"),
        ({"view_id": "wrong-view", "opened": True}, "open_spaced_review"),
        ({"hits": [{"path": "01 - Projects/note.md", "title": "Note", "snippet": "ok", "score": True}]}, "omnisearch"),
        ({"path": "01 - Projects/note.md", "view_type": "markdown"}, "open_excalidraw"),
    ],
)
def test_private_workflow_gateway_result_drift_fails_closed(payload: dict[str, object], method: str):
    names = {
        "lint_note": "grokbot_lint_note_v1",
        "open_spaced_review": "grokbot_sr_open_review_v1",
        "omnisearch": "grokbot_omnisearch_v1",
        "open_excalidraw": "grokbot_excalidraw_open_v1",
    }
    note = "01 - Projects/note.md"
    client = ScriptedClient(
        {names[method]: _private_result(payload)},
        files={note: _vault_file("note", [])},
    )
    port = NativeMcpPort(client)
    with pytest.raises(ObsidianError) as caught:
        if method == "lint_note":
            port.lint_note(note, hashlib.sha256(b"note").hexdigest())
        elif method == "open_spaced_review":
            port.open_spaced_review()
        elif method == "omnisearch":
            port.omnisearch("note", 1)
        elif method == "open_excalidraw":
            port.open_excalidraw(note)
        else:
            port.open_excalidraw(note)
    assert caught.value.code == "protocol_error"


def test_excalidraw_four_call_order_and_allowlisted_env(tmp_path: Path):
    session = "session01"
    client = ScriptedExcalidraw(session)
    staged = tmp_path / "scene.json"
    staged.write_text(
        '{"elements":[{"id":"1","type":"rectangle"}],"appState":{"grid":false},"version":1}\n',
        encoding="utf-8",
    )
    captured = {}

    def start(spec):
        captured.update(spec)
        return client

    adapter = ExcalidrawAdapter(
        bin_path="/usr/bin/true",
        staging_dir=str(tmp_path),
        native=MemoryNative(),
        policy={
            "dailyNotesFolder": "",
            "sensitivePathPrefixes": (),
            "sensitiveTags": (),
            "dashboardRoot": "01 - Projects/Dashboard.base",
            "excalidrawSuffix": ".excalidraw.md",
            "tags": (),
        },
        read_proof=lambda: {
            "enabled": True,
            "verified_suffix": ".excalidraw.md",
            "probe_receipt_sha256": "sha256:" + ("0" * 64),
            "receipt": b"probe",
        },
        start_client=start,
        read_file=lambda path: staged.read_bytes(),
        env={"HOME": "/tmp", "SECRET": "nope", "PATH": "/usr/bin"},
        random_id=lambda: session,
    )
    digest = "sha256:" + hashlib.sha256(b"probe").hexdigest()
    adapter.read_proof = lambda: {
        "enabled": True,
        "verified_suffix": ".excalidraw.md",
        "probe_receipt_sha256": digest,
        "receipt": b"probe",
    }
    result = adapter.create_excalidraw(
        {
            "kind": "create_excalidraw",
            "name": "Board",
            "elements": [{"id": "1", "type": "rectangle"}],
        }
    )
    assert result["outcome"] == "verified"
    assert client.names == ["start_session", "create_diagram", "add_elements", "export_diagram", "close"]
    assert captured["env"]["BROWSER"] == "/usr/bin/true"
    assert set(captured["env"]) <= set(ENV_ALLOWLIST) | {"BROWSER"}
    assert "SECRET" not in captured["env"]
    assert captured["shell"] is False


def test_scene_walk_rejects_executable_markdown():
    with pytest.raises(ObsidianError):
        assert_safe_excalidraw_scene([{"text": "<% tp.file.title %>"}])
    assert_safe_excalidraw_scene([{"id": "1", "type": "rectangle"}])


def test_allowlisted_env_pins_browser():
    env = allowlisted_excalidraw_env({"HOME": "/tmp", "BROWSER": "/bin/chrome", "AWS_SECRET": "x"})
    assert env["BROWSER"] == "/usr/bin/true"
    assert "AWS_SECRET" not in env


def test_native_mcp_allowlist_rejects_command_execute():
    client = ScriptedClient({"command_execute": {"ok": True}})
    port = NativeMcpPort(client)
    with pytest.raises(ObsidianError) as caught:
        port._call("command_execute", {})
    assert caught.value.code == "protocol_error"
    assert client.calls == []
    assert "command_execute" not in NATIVE_MCP_TOOLS
    assert "vault_write" in NATIVE_MCP_TOOLS


def test_targeted_read_uses_target_type_target_and_scope():
    heading = ["Parent", "Child"]
    path = "01 - Projects/demo.md"
    client = ScriptedClient(
        {
            "vault_read": "Heading body",
        },
        files={path: _vault_file("Heading body", [])},
    )
    port = NativeMcpPort(client)
    assert port.read_target(path, {"kind": "heading", "heading": heading}) == "Heading body"
    assert client.calls == [
        ("vault_read", {"path": path}),
        (
            "vault_read",
            {
                "path": path,
                "targetType": "heading",
                "target": heading,
                "scope": "content",
            },
        ),
    ]


def test_targeted_patch_uses_exact_top_level_fields_and_never_vault_write():
    heading = ["Parent", "Child"]
    path = "01 - Projects/demo.md"
    client = ScriptedClient(
        {"vault_patch": {"ok": True}, "vault_write": {"ok": True}},
        files={path: _vault_file("hello", [])},
    )
    port = NativeMcpPort(client)
    port.patch(
        path,
        {"target": "heading", "heading": heading, "body": "Replacement\n"},
        "aaaaaa",
    )
    port.patch(
        path,
        {"target": "block", "selector": "^block-id", "body": "replacement"},
        "aaaaaa",
    )
    port.patch(
        path,
        {"target": "frontmatter", "selector": "count", "value": 4},
        "aaaaaa",
    )
    assert [name for name, _ in client.calls] == [
        "vault_read",
        "vault_patch",
        "vault_read",
        "vault_patch",
        "vault_read",
        "vault_patch",
    ]
    assert [call for call in client.calls if call[0] == "vault_patch"] == [
        (
            "vault_patch",
            {
                "path": "01 - Projects/demo.md",
                "operation": "replace",
                "scope": "content",
                "ifMatch": "aaaaaa",
                "targetType": "heading",
                "target": heading,
                "content": "Replacement\n",
            },
        ),
        (
            "vault_patch",
            {
                "path": "01 - Projects/demo.md",
                "operation": "replace",
                "scope": "content",
                "ifMatch": "aaaaaa",
                "targetType": "block",
                "target": "^block-id",
                "content": "replacement",
            },
        ),
        (
            "vault_patch",
            {
                "path": "01 - Projects/demo.md",
                "operation": "replace",
                "scope": "content",
                "ifMatch": "aaaaaa",
                "targetType": "frontmatter",
                "target": "count",
                "value": 4,
            },
        ),
    ]
    assert "vault_write" not in {name for name, _ in client.calls}


def test_copy_and_move_use_path_destination_and_forbid_overwrite():
    source = "01 - Projects/demo.md"
    client = ScriptedClient(
        {"vault_copy": {"ok": True}, "vault_move": {"ok": True}},
        files={source: _vault_file("hello", [])},
    )
    port = NativeMcpPort(client)
    port.copy(source, "00 - Inbox/Agent Notes/copy.md")
    port.move(source, "04 - Archive/demo.md")
    assert (
        "vault_copy",
        {"from": source, "to": "00 - Inbox/Agent Notes/copy.md", "allowOverwrite": False},
    ) not in client.calls
    assert [call for call in client.calls if call[0] in {"vault_copy", "vault_move"}] == [
        (
            "vault_copy",
            {
                "path": source,
                "destination": "00 - Inbox/Agent Notes/copy.md",
                "allowOverwrite": False,
            },
        ),
        (
            "vault_move",
            {
                "path": source,
                "destination": "04 - Archive/demo.md",
                "allowOverwrite": False,
            },
        ),
    ]


def test_vault_read_exact_missing_file_envelope_returns_none():
    requested = "01 - Projects/missing-note.md"
    normalized = normalize_vault_path(requested)
    client = ScriptedClient(
        {
            "vault_read": {
                "isError": True,
                "content": [{"type": "text", "text": f"File not found: {normalized}"}],
            }
        }
    )
    port = NativeMcpPort(client)
    assert port.read(requested) is None
    assert client.calls == [("vault_read", {"path": normalized})]


@pytest.mark.parametrize(
    "envelope",
    [
        {
            "isError": True,
            "content": [{"type": "text", "text": "File not found: 01 - Projects/other.md"}],
        },
        {
            "isError": True,
            "content": [{"type": "text", "text": "File not found: 01 - Projects/missing-note.md"}],
            "extra": True,
        },
        {
            "isError": True,
            "content": [
                {"type": "text", "text": "File not found: 01 - Projects/missing-note.md"},
                {"type": "text", "text": "extra"},
            ],
        },
        {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "File not found: 01 - Projects/missing-note.md",
                    "extra": True,
                }
            ],
        },
        {
            "isError": True,
            "content": [{"type": "image", "text": "File not found: 01 - Projects/missing-note.md"}],
        },
        {"isError": True, "content": [{"type": "text", "text": "Permission denied"}]},
        {"isError": True, "content": [{"type": "text", "text": 1}]},
        {"isError": True, "content": [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}]},
        {"isError": True, "missing": True},
        {
            "isError": 1,
            "content": [{"type": "text", "text": "File not found: 01 - Projects/missing-note.md"}],
        },
        {"isError": True, "content": []},
    ],
    ids=[
        "different_path",
        "extra_top_level_keys",
        "extra_content_blocks",
        "extra_block_keys",
        "non_text_block",
        "other_error_message",
        "non_string_text",
        "text_over_max_output_bytes",
        "is_error_true_and_missing_true",
        "is_error_integer_1",
        "empty_is_error_content",
    ],
)
def test_vault_read_inexact_error_envelope_is_protocol_error(envelope: dict[str, object]):
    requested = "01 - Projects/missing-note.md"
    client = ScriptedClient({"vault_read": envelope})
    port = NativeMcpPort(client)
    with pytest.raises(ObsidianError) as caught:
        port.read(requested)
    assert caught.value.code == "protocol_error"


@pytest.mark.parametrize(
    "envelope",
    [
        {"type": "text", "content": []},
        {
            "type": "text",
            "content": [{"type": "image", "text": "File not found: 01 - Projects/missing-note.md"}],
        },
        {"type": "text", "content": [{"type": "text", "text": 1}]},
        {
            "type": "text",
            "content": [{"type": "text", "text": "x" * (MAX_OUTPUT_BYTES + 1)}],
        },
    ],
    ids=[
        "empty_content",
        "non_text_content",
        "non_string_text",
        "oversized_text",
    ],
)
def test_vault_read_malformed_legacy_type_text_wrapper_is_protocol_error(
    envelope: dict[str, object],
):
    requested = "01 - Projects/missing-note.md"
    client = ScriptedClient({"vault_read": envelope})
    port = NativeMcpPort(client)
    with pytest.raises(ObsidianError) as caught:
        port.read(requested)
    assert caught.value.code == "protocol_error"


def test_excalidraw_embed_is_a_staged_heading_patch(tmp_path: Path):
    session = "session02"
    client = ScriptedExcalidraw(session)
    staged = tmp_path / "scene.json"
    staged.write_text(
        '{"elements":[{"id":"1","type":"rectangle"}],"appState":{"grid":false},"version":1}\n',
        encoding="utf-8",
    )
    native = MemoryNative()
    native.files["01 - Projects/demo.md"] = {"bytes": b"# Excalidraw\n", "etag": "aaaaaa"}
    native.targets["01 - Projects/demo.md"] = ""
    digest = "sha256:" + hashlib.sha256(b"probe").hexdigest()
    adapter = ExcalidrawAdapter(
        bin_path="/usr/bin/true",
        staging_dir=str(tmp_path),
        native=native,
        policy={
            "dailyNotesFolder": "",
            "sensitivePathPrefixes": (),
            "sensitiveTags": (),
            "dashboardRoot": "01 - Projects/Dashboard.base",
            "excalidrawSuffix": ".excalidraw.md",
            "tags": (),
        },
        read_proof=lambda: {
            "enabled": True,
            "verified_suffix": ".excalidraw.md",
            "probe_receipt_sha256": digest,
            "receipt": b"probe",
        },
        start_client=lambda spec: client,
        read_file=lambda path: staged.read_bytes(),
        env={"HOME": "/tmp", "PATH": "/usr/bin"},
        random_id=lambda: session,
    )
    result = adapter.create_excalidraw(
        {
            "kind": "create_excalidraw",
            "name": "Board",
            "elements": [{"id": "1", "type": "rectangle"}],
            "embed_path": "01 - Projects/demo.md",
        },
        {"embedIfMatch": "aaaaaa"},
    )
    assert result["outcome"] == "verified"
    assert client.names == ["start_session", "create_diagram", "add_elements", "export_diagram", "close"]
    assert "![[03 - Resources/Excalidraw/Board.excalidraw.md]]" in native.targets["01 - Projects/demo.md"]
