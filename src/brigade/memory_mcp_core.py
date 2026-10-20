"""Brigade memory MCP tools, independent of transport.

One contract serves the stdio and HTTP transports. Read tools span the repo and
user stores (repo first); write_handoff drafts a Memory Handoff, which is also
the reviewed route for proposing card changes. Calls return (payload, is_error),
the shape mcp_server.ToolCaller expects.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import memory_cmd, memory_scope
from .card_fingerprint import read_text_nofollow
from .handoff_cmd import drafts as handoff_drafts
from .handoff_cmd.models import DEFAULT_DRAFT_DOCUMENT
from .memory_scope import REPO, USER, MemoryStore, Resolution

SERVER_NAME = "brigade-memory"
CARD_URI_PREFIX = "card://"
# User-store cards get their own scheme: "card://user:<rel>" would put a colon in the URI authority.
USER_CARD_URI_PREFIX = "user-card://"
_NATIVE_SEP = os.sep
SEARCH_LIMIT = 20
MAX_CONTENT_BYTES = 256 * 1024
MAX_LIST_ITEMS = 50
SCOPES = ("repo", "user", "all")
BRIEF_KEYS = ("git", "active_session", "latest_session", "next")
UNWIRED_HINT = (
    "This directory is not a Brigade-wired repo, so memory tools use the user store. "
    "Run the Brigade enforce skill to wire this repo into the full work loop."
)
UNWIRED_REPO_HINT = (
    "This repo is not Brigade-wired, so no work brief is available. "
    "Run the Brigade enforce skill to wire this repo into the full work loop."
)
# build_draft words its errors for the CLI; the tool reports its own argument names.
_DRAFT_FLAG_ARGS = (
    ("--target-document", "target_document"),
    ("--target-card", "target_card"),
    ("--action", "action"),
    ("--title", "title"),
    ("--summary", "summary"),
)

_READ = {"readOnlyHint": True}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}

ToolResult = tuple[object, bool]


def _posix(rel: str) -> str:
    """Report card paths with forward slashes. Only the native separator is rewritten,
    since a backslash is an ordinary filename character on POSIX."""
    return rel.replace(_NATIVE_SEP, "/") if _NATIVE_SEP != "/" else rel


def card_ref(origin: str, rel: str) -> str:
    return f"{origin}:{_posix(rel)}"


def card_uri(origin: str, rel: str) -> str:
    # Repo card:// URIs keep their historical unencoded form for existing clients.
    rel = _posix(rel)
    return f"{CARD_URI_PREFIX}{rel}" if origin == REPO else f"{USER_CARD_URI_PREFIX}{quote(rel, safe='/')}"


def split_ref(ref: str) -> tuple[str | None, str]:
    """Parse repo:<rel>, user:<rel>, card://<ref>, user-card://<rel>, or a bare path (origin None)."""
    if ref.startswith(USER_CARD_URI_PREFIX):
        return USER, unquote(ref[len(USER_CARD_URI_PREFIX) :])
    if ref.startswith(CARD_URI_PREFIX):
        ref = ref[len(CARD_URI_PREFIX) :]
    for origin in (REPO, USER):
        prefix = f"{origin}:"
        if ref.startswith(prefix):
            return origin, ref[len(prefix) :]
    return None, ref


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _store_cards(store: MemoryStore) -> list[Path]:
    """Cards whose real path stays inside the store.

    _iter_cards skips symlinked files but follows a symlinked card root, so a
    memory/cards link to an outside directory would otherwise leak its cards.
    """
    return [path for path in memory_cmd._iter_cards(store.root, store.config) if _inside(path, store.root)]


def _read_in_store(store: MemoryStore, rel: str) -> str | None:
    candidate = (store.root / rel).resolve()
    if not candidate.is_relative_to(store.root.resolve()):
        return None
    for path in _store_cards(store):
        if path.resolve() == candidate:
            try:
                return read_text_nofollow(path)
            except OSError:
                return None
    return None


def _scope(arguments: dict[str, Any]) -> str:
    value = arguments.get("scope", "all")
    if value not in SCOPES:
        raise ValueError("scope must be one of: " + ", ".join(SCOPES))
    return str(value)


def _required_text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _optional_text(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value or None


def _string_list(arguments: dict[str, Any], key: str) -> list[str]:
    value = arguments.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{key} must have at most {MAX_LIST_ITEMS} items")
    return list(value)


def tool_specs() -> list[dict[str, Any]]:
    scope = {"type": "string", "enum": list(SCOPES), "description": "Store to read. Default all."}
    strings = {"type": "array", "items": {"type": "string"}}
    return [
        {
            "name": "list_cards",
            "description": (
                "List every memory card in the repo and user stores, with each card's store-relative path, "
                "card:// URI, origin (repo or user), and origin-qualified ref. Use search_cards to find cards "
                "by topic."
            ),
            "inputSchema": {"type": "object", "properties": {"scope": scope}},
            "annotations": _READ,
        },
        {
            "name": "get_card",
            "description": "Read one memory card's full markdown, frontmatter included.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "A ref such as repo:memory/cards/release-process.md or user:memory/cards/x.md, a "
                            "card:// URI from list_cards or search_cards, or a bare store-relative path, which "
                            "is looked up in the repo store first and then the user store."
                        ),
                    }
                },
                "required": ["path"],
            },
            "annotations": _READ,
        },
        {
            "name": "search_cards",
            "description": (
                "Keyword-search memory cards across the repo and user stores. A term in a card's title, tags, "
                "or summary outranks one found only in the body, and repo matches rank first on ties. Returns "
                f"up to {SEARCH_LIMIT} matches with path, title, tags, summary, score, origin, and ref; read "
                "one with get_card."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Space-separated keywords, case-insensitive."}
                },
                "required": ["query"],
            },
            "annotations": _READ,
        },
        {
            "name": "memory_status",
            "description": "Show the resolved memory stores, card counts, and whether the repo is Brigade-wired.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": _READ,
        },
        {
            "name": "work_brief",
            "description": "Compact Brigade work brief for the current repo: branch, dirty files, sessions, next task.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": _READ,
        },
        {
            "name": "write_handoff",
            "description": "Write a Memory Handoff to the repo inbox, or the user store outside a repo. "
            "Use action create-card or update-card with target_card and frontmatter content to propose "
            "a card change for review. Never edits canonical cards and never overwrites a file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "content": {"type": "string", "description": "Suggested card or document content."},
                    "type": {"type": "string", "description": "workflow, decision, setup, bugfix, ..."},
                    "action": {"type": "string", "enum": ["no-card", "create-card", "update-card"]},
                    "target_card": {"type": "string", "description": "Card filename for card actions."},
                    "target_document": {"type": "string", "description": "Document for no-card handoffs."},
                    "facts": strings,
                    "evidence": strings,
                },
                "required": ["title", "summary", "content"],
            },
            "annotations": _WRITE,
        },
    ]


class MemoryTools:
    def __init__(self, *, target: Path | None = None, cwd: Path | None = None, inbox: str = "codex") -> None:
        self._target = target
        self._cwd = cwd
        self._inbox = inbox

    def resolution(self) -> Resolution:
        return memory_scope.resolve(target=self._target, cwd=self._cwd)

    def callbacks(self) -> dict[str, Any]:
        """Keyword arguments for mcp_server.serve_stdio and run_http_from_env."""
        return {
            "server_name": SERVER_NAME,
            "list_resources": self.resources,
            "read_resource": self.read_resource,
            "list_tools": tool_specs,
            "call_tool": self.call,
        }

    def read_card(self, ref: str) -> tuple[str | None, str | None]:
        origin, rel = split_ref(ref)
        for store in self.resolution().stores:
            if origin is not None and store.origin != origin:
                continue
            text = _read_in_store(store, rel)
            if text is not None:
                return text, store.origin
        return None, None

    def resources(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for store in self.resolution().stores:
            for path in _store_cards(store):
                fields = memory_cmd._card_search_fields(path, store.root)
                if not fields:
                    continue
                items.append(
                    {
                        "uri": card_uri(store.origin, fields["rel"]),
                        "name": fields["title"],
                        "mimeType": "text/markdown",
                        "description": fields["summary"],
                    }
                )
        return items

    def read_resource(self, uri: str) -> tuple[str, str] | tuple[None, None]:
        text, _ = self.read_card(uri)
        if text is None:
            return None, None
        return text, "text/markdown"

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        handler = self._tool_table().get(name)
        if handler is None:
            return f"unknown tool: {name}", True
        try:
            return handler(arguments)
        except (ValueError, OSError) as exc:
            return str(exc), True
        except Exception as exc:
            # One bad call must not end a long-lived stdio or HTTP server.
            return f"{name} failed: {type(exc).__name__}: {exc}", True

    def _tool_table(self) -> dict[str, Callable[[dict[str, Any]], ToolResult]]:
        return {
            "list_cards": self._list_cards,
            "get_card": self._get_card,
            "search_cards": self._search_cards,
            "memory_status": self._memory_status,
            "work_brief": self._work_brief,
            "write_handoff": self._write_handoff,
        }

    def _list_cards(self, arguments: dict[str, Any]) -> ToolResult:
        scope = _scope(arguments)
        items: list[dict[str, Any]] = []
        for store in self.resolution().stores:
            if scope != "all" and store.origin != scope:
                continue
            for path in _store_cards(store):
                rel = path.relative_to(store.root).as_posix()
                items.append(
                    {
                        "path": rel,
                        "uri": card_uri(store.origin, rel),
                        "origin": store.origin,
                        "ref": card_ref(store.origin, rel),
                    }
                )
        return items, False

    def _get_card(self, arguments: dict[str, Any]) -> ToolResult:
        ref = str(arguments.get("path") or "")
        text, _ = self.read_card(ref)
        if text is None:
            return f"card not found: {ref}", True
        return text, False

    def _search_cards(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "")
        ranked: list[tuple[int, dict[str, Any]]] = []
        for rank, store in enumerate(self.resolution().stores):
            # Uncapped so outside-the-store matches dropped here cannot crowd out real ones.
            payload = memory_cmd.search_cards_payload(store.root, query, limit=sys.maxsize, config=store.config)
            for match in payload["matches"]:
                match = {**match, "path": _posix(match["path"])}
                if not _inside(store.root / match["path"], store.root):
                    continue
                ranked.append((rank, {**match, "origin": store.origin, "ref": card_ref(store.origin, match["path"])}))
        ranked.sort(key=lambda item: (-item[1]["score"], item[0], item[1]["path"]))
        return [match for _, match in ranked[:SEARCH_LIMIT]], False

    def _memory_status(self, arguments: dict[str, Any]) -> ToolResult:
        res = self.resolution()
        counts = {store.origin: len(_store_cards(store)) for store in res.stores}
        user = memory_scope.user_root()
        repo: dict[str, Any] | None = None
        if res.repo_root is not None:
            repo = {
                "root": str(res.repo_root),
                "wired": res.repo_wired,
                "card_count": counts.get(REPO, 0),
                "config_error": res.repo_config_error,
            }
        return {
            "repo": repo,
            "user": {"root": str(user), "exists": (user / "memory").is_dir(), "card_count": counts.get(USER, 0)},
            "claude_hooks_installed": res.repo_root is not None
            and (res.repo_root / ".brigade" / "claude-hooks.json").is_file(),
            "handoff_inbox": self._inbox
            if res.repo_root is not None
            else str(memory_scope.user_root() / memory_scope.USER_HANDOFF_INBOX),
        }, False

    def _work_brief(self, arguments: dict[str, Any]) -> ToolResult:
        res = self.resolution()
        if res.repo_root is None or not res.repo_wired:
            return {
                "wired": False,
                "user_store": str(memory_scope.user_root() / "memory"),
                "hint": UNWIRED_HINT if res.repo_root is None else UNWIRED_REPO_HINT,
            }, False
        # The same helpers the full brief uses for these keys, without its hub,
        # presence, and health sections, so the tool stays local and fast.
        from .work_cmd import helpers
        from .work_cmd.session.lifecycle import _resolve_next_task

        root = res.repo_root
        sessions, _ = helpers._collect_sessions(helpers._work_root(root))
        brief = {
            "git": helpers._git_snapshot(root),
            "active_session": helpers._active_session_info(root),
            "latest_session": helpers._session_info(*sessions[0]) if sessions else None,
            "next": str(_resolve_next_task(root)["task"]),
        }
        return {"wired": True, "root": str(root), **{key: brief[key] for key in BRIEF_KEYS}}, False

    def _write_handoff(self, arguments: dict[str, Any]) -> ToolResult:
        title = _required_text(arguments, "title")
        summary = _required_text(arguments, "summary")
        content = _required_text(arguments, "content")
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError(f"content must be at most {MAX_CONTENT_BYTES} bytes")
        target_card = _optional_text(arguments, "target_card")
        target_document = _optional_text(arguments, "target_document") or DEFAULT_DRAFT_DOCUMENT
        facts = _string_list(arguments, "facts")
        evidence = _string_list(arguments, "evidence")
        repo_root = self.resolution().repo_root
        if repo_root is not None:
            # The repo inbox stays the destination even when an invalid
            # memory-care config dropped the repo store from reads.
            root, inbox, origin = repo_root, self._inbox, REPO
        else:
            root = memory_scope.user_root()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            (root / "memory").mkdir(mode=0o700, exist_ok=True)
            inbox, origin = str(root / memory_scope.USER_HANDOFF_INBOX), USER
        inbox_path, _ = handoff_drafts._draft_inbox_path(root, inbox)
        if not inbox_path.is_relative_to(root.resolve()):
            raise ValueError(f"handoff inbox resolves outside the memory store: {inbox_path}")
        if origin == USER:
            inbox_path.mkdir(mode=0o700, exist_ok=True)
            # mkdir leaves an existing inbox as it was; keep handoff names private.
            if inbox_path.stat().st_mode & 0o077:
                inbox_path.chmod(0o700)
        try:
            payload = handoff_drafts.build_draft(
                target=root,
                title=title,
                summary=summary,
                content=content,
                handoff_type=_optional_text(arguments, "type") or "workflow",
                action=_optional_text(arguments, "action") or "no-card",
                target_card=target_card,
                target_document=target_document,
                fact=facts,
                evidence=evidence,
                inbox=inbox,
            )
        except ValueError as exc:
            message = str(exc)
            for flag, arg in _DRAFT_FLAG_ARGS:
                message = message.replace(flag, arg)
            raise ValueError(message) from exc
        path = Path(payload["path"])
        if not payload["valid"]:
            path.unlink(missing_ok=True)
            return {"written": False, "errors": payload["lint"]["errors"]}, True
        return {
            "written": True,
            "origin": origin,
            "path": path.relative_to(root).as_posix(),
            "id": payload["id"],
            "action": payload["action"],
        }, False
