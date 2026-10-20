# Brigade agent plugin: implementation plan

Spec: `docs/phase-agent-plugin.md` (approved 2026-10-19).

**Goal:** Ship a Claude Code and Codex plugin that gives any user Brigade memory (search, read, handoffs) backed by one memory MCP core that runs over stdio and loopback HTTP.

**Architecture:** `mcp_server.py` gains a transport-free `dispatch()`. A new `memory_scope.py` resolves the repo and user stores, and a new `memory_mcp_core.py` owns the six-tool contract. `mcp_http.py` adds a loopback, bearer-gated Streamable HTTP transport. `brigade memory serve-mcp` wires both transports. `plugins/brigade/` packages the server, skills, and a Claude SessionStart hook.

**Tech:** Python 3.10+ standard library only (no new dependencies), pytest, Claude Code and Codex plugin manifests, bash for the hook.

**For agentic workers:** execute task by task in order. Tick each checkbox as you finish it. Do not skip the failing-test step. Leave changes uncommitted only if your dispatcher says so; otherwise commit at the end of each task as written.

## Conventions used in every task

- Run every test, lint, and type check through Brigade:
  `brigade work verify run --target . --command "<CMD>" --capture brigade-work`
  where `<CMD>` is the command shown in the step. Expected results below describe `<CMD>`'s own output.
- `PY` means `.venv/bin/python`.
- Commit messages use conventional commits and end with the executing agent's approved co-author trailer.

## Spec deltas pinned by this plan

These were decided while reading the code. Task 10 writes them back into the spec.

1. `propose_memory` is dropped. The phase 028 proposal path only merges duplicate cards from a memory-care finding. `write_handoff` with `action` `create-card` or `update-card` is the reviewed route for card changes, which the handoff ingestor already processes. Six tools ship, not seven.
2. The Codex plugin ships no hook. codex-cli 0.163.0 exposes no verified plugin hook contract. Codex users get the brief from the `work_brief` tool, which the `brigade-work` skill already tells agents to run first.
3. `/brigade:enforce` is a plugin skill named `enforce`, so it exists in both harnesses. The `memory-status` command is dropped because the `memory_status` tool covers it.
4. `serve-mcp --target` defaults to auto-detection. An explicit `--target` is always used as the repo store, wired or not, which preserves today's `--target .` configs. With no `--target`, the server walks up from its cwd for `.brigade/config.json` and never treats the home directory as a repo.
5. The stdio server name changes from `brigade-memory-readonly` to `brigade-memory` because the server now has a write tool. Client configs key on their own server name, not `serverInfo.name`.
6. The plugin's npm pin tracks `pyproject.toml` through `scripts/version_sync.py`. The root marketplace entries land in the release PR for the first version that contains these commands (Task 12), so `main` never advertises a plugin whose pinned package lacks them. npm `@escoffier-labs/brigade@0.29.0` is already published.
7. A `create-card`/`update-card` handoff whose content fails lint is deleted and returned as a tool error with the lint errors, so the agent can retry. No invalid draft is left in an inbox.

## File map

| File | Status | Responsibility |
|---|---|---|
| `src/brigade/mcp_server.py` | modify | JSON-RPC dispatch shared by all transports; stdio loop. |
| `src/brigade/mcp_http.py` | create | Loopback HTTP transport, bind parsing, bearer token file handling. |
| `src/brigade/memory_scope.py` | create | Resolve the repo store and user store for a session. |
| `src/brigade/memory_mcp_core.py` | create | The six memory tools, resources, and card references. |
| `src/brigade/handoff_cmd/drafts.py` | modify | Extract `build_draft()` (returns payload, raises `ValueError`) from `draft()`. |
| `src/brigade/memory_cmd.py` | modify | Remove the old card MCP code; `serve_mcp()` wires the core to both transports. |
| `src/brigade/cli/memory.py` | modify | New `serve-mcp` flags. |
| `tests/test_mcp_server_dispatch.py` | create | Dispatcher and protocol negotiation. |
| `tests/test_memory_scope.py` | create | Store resolution. |
| `tests/test_handoff_build_draft.py` | create | `build_draft()` contract. |
| `tests/test_memory_mcp_core.py` | create | Tool contract over both stores. |
| `tests/test_mcp_http.py` | create | HTTP security, token files, transport parity. |
| `tests/test_memory_search_mcp.py` | modify | Migrate card MCP tests to the core. |
| `tests/test_module_size_ratchet.py` | modify | Drop the `memory_cmd.py` legacy entry once it is under 2000 lines. |
| `plugins/brigade/...` | create | Plugin manifests, MCP configs, hook, skills (Task 9). |
| `scripts/sync_plugin_skills.py` | create | Copy the canonical `brigade-work` skill into the plugin; `--check` mode for CI. |
| `scripts/version_sync.py` | modify | Add plugin version and npm pin locations. |
| `tests/test_version_sync_script.py` | modify | Clear the new locations in the temp-repo helper. |
| `tests/test_agent_plugin.py` | create | Manifest, pin, skill drift, and hook tests. |
| `docs/agent-plugin.md` | create | User install and Executor guide. |
| `docs/phase-agent-plugin.md` | modify | Apply the spec deltas above. |
| `CHANGELOG.md` | modify | Unreleased entry. |

---

### Task 0: Workspace setup

**Files:** none tracked.

- [x] Wire the worktree into Brigade (writes git-excluded files only): `brigade init --target . --depth repo --harnesses claude --owner claude --git-exclude`, then expect `brigade work brief --target .` to print `work brief: <worktree path>`.
- [x] Create the venv: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`. Expect the last line to contain `Successfully installed brigade-cli`.
- [x] Baseline: `PY -m pytest tests/test_memory_search_mcp.py tests/test_handoff_cmd.py -q -p no:cacheprovider`. Expect all to pass. Stop and report if not.

---

### Task 1: Transport-free MCP dispatch

**Files:**
- Modify: `src/brigade/mcp_server.py` (whole file, 96 lines)
- Test: `tests/test_mcp_server_dispatch.py`

- [x] Write the failing test `tests/test_mcp_server_dispatch.py`:

```python
"""Shared MCP JSON-RPC dispatch used by the stdio and HTTP transports."""

from __future__ import annotations

from brigade import mcp_server


def _handlers() -> mcp_server.Handlers:
    return mcp_server.Handlers(
        server_name="test-server",
        list_resources=lambda: [{"uri": "card://a.md"}],
        read_resource=lambda uri: ("body", "text/markdown") if uri == "card://a.md" else (None, None),
        list_tools=lambda: [{"name": "echo"}],
        call_tool=lambda name, args: ({"name": name, "args": args}, False),
    )


def test_initialize_negotiates_protocol_version():
    h = _handlers()
    legacy = mcp_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, h)
    assert legacy["result"]["protocolVersion"] == "2024-11-05"
    assert legacy["result"]["serverInfo"]["name"] == "test-server"
    echoed = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, h
    )
    assert echoed["result"]["protocolVersion"] == "2025-03-26"
    newest = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}, h
    )
    assert newest["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_get_no_reply_and_ping_answers():
    h = _handlers()
    assert mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, h) is None
    assert mcp_server.dispatch({"jsonrpc": "2.0", "id": 4, "method": "ping"}, h)["result"] == {}


def test_tools_call_and_errors():
    h = _handlers()
    reply = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo", "arguments": {"x": 1}}}, h
    )
    assert reply["result"]["isError"] is False
    assert '"x": 1' in reply["result"]["content"][0]["text"]
    missing = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "card://nope.md"}}, h
    )
    assert missing["error"]["code"] == -32004
    assert mcp_server.dispatch([1, 2], h)["error"]["code"] == -32600
    assert mcp_server.dispatch({"jsonrpc": "2.0", "id": 7, "method": "bogus"}, h)["error"]["code"] == -32601
```

- [x] Run it, watch it fail: `PY -m pytest tests/test_mcp_server_dispatch.py -q -p no:cacheprovider`. Expect FAIL with `AttributeError: module 'brigade.mcp_server' has no attribute 'Handlers'`.
- [x] Replace everything from `def _emit` to the end of `src/brigade/mcp_server.py` with the code below. Keep the module docstring, imports, the four `Callable` aliases, and `response()`. Add `from dataclasses import dataclass` to the imports.

```python
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LEGACY_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class Handlers:
    server_name: str
    list_resources: ResourceLister
    read_resource: ResourceReader
    list_tools: ToolLister
    call_tool: ToolCaller


def negotiate_protocol(requested: object) -> str:
    """Echo a supported client version; answer the newest for an unknown one.

    A request that omits the version keeps the legacy answer older stdio
    clients already received.
    """
    if not isinstance(requested, str) or not requested:
        return LEGACY_PROTOCOL_VERSION
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return SUPPORTED_PROTOCOL_VERSIONS[0]


def dispatch(request: object, handlers: Handlers) -> dict[str, Any] | None:
    """Answer one decoded JSON-RPC message. None means send nothing (a notification)."""
    if not isinstance(request, dict):
        return response(None, error={"code": -32600, "message": "invalid request"})
    request_id = request.get("id")
    method = str(request.get("method") or "")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if request_id is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return response(
            request_id,
            result={
                "protocolVersion": negotiate_protocol(params.get("protocolVersion")),
                "capabilities": {"resources": {}, "tools": {}},
                "serverInfo": {"name": handlers.server_name, "version": "0"},
            },
        )
    if method == "ping":
        return response(request_id, result={})
    if method == "resources/list":
        return response(request_id, result={"resources": handlers.list_resources()})
    if method == "resources/read":
        uri = str(params.get("uri") or "")
        text, mime_type = handlers.read_resource(uri)
        if text is None:
            return response(request_id, error={"code": -32004, "message": f"resource not found: {uri}"})
        return response(request_id, result={"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]})
    if method == "tools/list":
        return response(request_id, result={"tools": handlers.list_tools()})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        payload, failed = handlers.call_tool(name, arguments)
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True)
        return response(request_id, result={"content": [{"type": "text", "text": text}], "isError": failed})
    return response(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def serve_stdio_handlers(handlers: Handlers) -> int:
    """Run an MCP server over stdin/stdout until EOF."""
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _emit(response(None, error={"code": -32700, "message": "parse error"}))
            continue
        reply = dispatch(request, handlers)
        if reply is not None:
            _emit(reply)
    return 0


def serve_stdio(
    *,
    server_name: str,
    list_resources: ResourceLister,
    read_resource: ResourceReader,
    list_tools: ToolLister,
    call_tool: ToolCaller,
) -> int:
    """Run an MCP server over stdin/stdout until EOF.

    read_resource returns (text, mime_type) or (None, None) when the uri is not
    served. call_tool returns (payload, is_error); a non-string payload is
    rendered as indented JSON.
    """
    return serve_stdio_handlers(Handlers(server_name, list_resources, read_resource, list_tools, call_tool))
```

- [x] Update the module docstring's first two lines to: `"""Shared zero-dependency MCP JSON-RPC dispatch and stdio loop.` / blank / `Stations expose local data over MCP (skills, memory cards). The JSON-RPC` (leave the rest).
- [x] Run to green: `PY -m pytest tests/test_mcp_server_dispatch.py tests/test_memory_search_mcp.py tests/test_skills_cmd.py -q -p no:cacheprovider`. Expect all PASS.
- [x] Commit: `git add src/brigade/mcp_server.py tests/test_mcp_server_dispatch.py && git commit -m "refactor(mcp): share JSON-RPC dispatch across transports"`

---

### Task 2: Memory store resolution

**Files:**
- Create: `src/brigade/memory_scope.py`
- Test: `tests/test_memory_scope.py`

- [x] Write the failing test `tests/test_memory_scope.py`:

```python
"""Repo and user memory store resolution for the memory MCP server."""

from __future__ import annotations

from pathlib import Path

import pytest

from brigade import memory_scope


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    return home


def test_explicit_target_is_repo_store_even_when_unwired(home: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    res = memory_scope.resolve(target=repo)
    assert [s.origin for s in res.stores] == ["repo", "user"]
    assert res.repo_root == repo.resolve()
    assert res.repo_wired is False
    assert res.stores[1].root == (home / ".brigade").resolve()


def test_no_target_and_no_wired_repo_gives_user_store_only(home: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    res = memory_scope.resolve(cwd=tmp_path)
    assert [s.origin for s in res.stores] == ["user"]
    assert res.repo_root is None


def test_wired_repo_is_detected_but_home_never_is(home: Path, tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: repo)
    res = memory_scope.resolve(cwd=repo)
    assert res.repo_root == repo.resolve() and res.repo_wired is True
    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: home)
    assert memory_scope.resolve(cwd=home).repo_root is None


def test_invalid_repo_config_degrades_to_user_store(home: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".brigade").mkdir(parents=True)
    (repo / ".brigade" / "memory-care.toml").write_text("card_roots = 7\n")
    res = memory_scope.resolve(target=repo)
    assert [s.origin for s in res.stores] == ["user"]
    assert res.repo_config_error
    assert res.repo_root == repo.resolve()


def test_target_equal_to_user_root_is_not_duplicated(home: Path):
    res = memory_scope.resolve(target=home / ".brigade")
    assert [s.origin for s in res.stores] == ["user"]


def test_http_token_path_lives_under_brigade_home(home: Path):
    assert memory_scope.http_token_path() == (home / ".brigade").resolve() / "mcp-http.token"
```

- [x] Run it, watch it fail: `PY -m pytest tests/test_memory_scope.py -q -p no:cacheprovider`. Expect FAIL with `ImportError: cannot import name 'memory_scope'`.
- [x] Create `src/brigade/memory_scope.py`:

```python
"""Resolve the memory stores an MCP session reads and writes.

Two stores exist. The repo store is an explicit --target, or the nearest
Brigade-wired repo above the server's cwd (never the home directory). The user
store lives under BRIGADE_HOME so memory works outside any repo. Repo results
take precedence; an invalid repo memory-care config drops the repo store and is
reported instead of failing the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import memory_cmd
from .claude_hooks.paths import is_operator_home
from .fleet_client import brigade_home
from .wiring import resolve_wired_target

REPO = "repo"
USER = "user"
USER_HANDOFF_INBOX = "memory/handoffs"
HTTP_TOKEN_NAME = "mcp-http.token"


@dataclass(frozen=True)
class MemoryStore:
    origin: str
    root: Path
    config: memory_cmd.MemoryCareConfig


@dataclass(frozen=True)
class Resolution:
    stores: tuple[MemoryStore, ...]
    repo_root: Path | None
    repo_wired: bool
    repo_config_error: str | None


def user_root() -> Path:
    return brigade_home().expanduser().resolve()


def http_token_path() -> Path:
    return user_root() / HTTP_TOKEN_NAME


def _repo_root(target: Path | None, cwd: Path) -> tuple[Path | None, bool]:
    if target is not None:
        root = target.expanduser().resolve()
        return root, (root / ".brigade" / "config.json").is_file()
    found = resolve_wired_target(str(cwd), harness=None)
    if found is None or is_operator_home(found):
        return None, False
    return found.resolve(), True


def resolve(*, target: Path | None = None, cwd: Path | None = None) -> Resolution:
    repo_root, wired = _repo_root(target, cwd if cwd is not None else Path.cwd())
    user = user_root()
    stores: list[MemoryStore] = []
    error: str | None = None
    if repo_root is not None and repo_root != user:
        try:
            config = memory_cmd.load_config(repo_root) or memory_cmd.MemoryCareConfig()
        except ValueError as exc:
            error = str(exc)
        else:
            stores.append(MemoryStore(REPO, repo_root, config))
    stores.append(MemoryStore(USER, user, memory_cmd.MemoryCareConfig()))
    return Resolution(tuple(stores), repo_root, wired, error)
```

The user store passes `BRIGADE_HOME` as the memory-care target with the default config, so its cards live at `$BRIGADE_HOME/memory/cards/` (the default `card_roots`). That is the layout the spec names.

- [x] Run to green: same command. Expect 6 PASS.
- [x] Commit: `git add src/brigade/memory_scope.py tests/test_memory_scope.py && git commit -m "feat(memory): resolve repo and user memory stores"`

---

### Task 3: Extract `build_draft()` from the handoff draft command

**Files:**
- Modify: `src/brigade/handoff_cmd/drafts.py:769-874` (`draft()`)
- Test: `tests/test_handoff_build_draft.py`

- [x] Write the failing test `tests/test_handoff_build_draft.py`:

```python
"""build_draft() writes one handoff and returns its payload without printing."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from brigade.handoff_cmd import drafts


def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = time.struct_time((2026, 10, 19, 12, 0, 0, 0, 292, 0))
    monkeypatch.setattr(drafts.time, "gmtime", lambda *args: frozen)


def test_build_draft_returns_payload(tmp_path: Path, monkeypatch, capsys):
    _freeze(monkeypatch)
    payload = drafts.build_draft(
        target=tmp_path, title="Plugin memory", summary="Short summary.", content="Body.", inbox=str(tmp_path / "in")
    )
    assert payload["valid"] is True
    assert payload["action"] == "no-card"
    assert Path(payload["path"]).is_file()
    assert capsys.readouterr().out == ""


def test_build_draft_refuses_overwrite(tmp_path: Path, monkeypatch):
    _freeze(monkeypatch)
    kwargs = dict(target=tmp_path, title="Same", summary="S.", content="Body.", inbox=str(tmp_path / "in"))
    drafts.build_draft(**kwargs)
    with pytest.raises(ValueError, match="already exists"):
        drafts.build_draft(**kwargs)


def test_build_draft_validates_inputs(tmp_path: Path):
    with pytest.raises(ValueError, match="--target-card is required"):
        drafts.build_draft(target=tmp_path, title="T", summary="S.", content="Body.", action="create-card")
    with pytest.raises(ValueError, match="--action must be one of"):
        drafts.build_draft(target=tmp_path, title="T", summary="S.", content="Body.", action="delete")
```

- [x] Run it, watch it fail: `PY -m pytest tests/test_handoff_build_draft.py -q -p no:cacheprovider`. Expect FAIL with `AttributeError: module 'brigade.handoff_cmd.drafts' has no attribute 'build_draft'`.
- [x] In `src/brigade/handoff_cmd/drafts.py`, insert `build_draft()` immediately above `def draft(`:

```python
def build_draft(
    *,
    target: Path,
    title: str,
    summary: str,
    content: str | None = None,
    content_file: Path | None = None,
    handoff_type: str = "workflow",
    action: str = NO_CARD_ACTION,
    target_card: str | None = None,
    target_document: str | None = DEFAULT_DRAFT_DOCUMENT,
    fact: list[str] | None = None,
    evidence: list[str] | None = None,
    inbox: str = DEFAULT_DRAFT_INBOX,
    draft_id: str | None = None,
    force: bool = False,
    guard: bool = False,
    guard_policy: str = "personal",
) -> dict[str, Any]:
    """Write one handoff draft and return its payload. Raises ValueError on bad input."""
    action = action.strip().casefold()
    if action not in HANDOFF_ACTIONS:
        raise ValueError("--action must be one of: " + ", ".join(HANDOFF_ACTIONS))
    if not title.strip():
        raise ValueError("--title is required")
    if not summary.strip():
        raise ValueError("--summary is required")
    if action in CARD_ACTIONS:
        if not target_card:
            raise ValueError("--target-card is required for card handoffs")
        target_document = None
    else:
        if not target_document:
            raise ValueError("--target-document is required for no-card handoffs")
        target_card = None
    suggested_content = _read_suggested_content(content, content_file)
    if not suggested_content:
        raise ValueError("suggested content is empty")
    inbox_path, inbox_label = _draft_inbox_path(target, inbox)
    created_at = _iso_from_timestamp()
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    safe_id = _slugify(draft_id or title)
    path = inbox_path / f"{timestamp}-{safe_id}.md"
    if path.exists() and not force:
        raise ValueError(f"handoff draft already exists: {path}")
    text = _render_handoff_draft(
        handoff_type=handoff_type,
        title=title,
        summary=summary,
        facts=fact or [],
        evidence=evidence or [],
        action=action,
        target_card=target_card,
        target_document=target_document,
        suggested_content=suggested_content,
    )
    inbox_path.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    lint_result = _linting_mod.lint_file(path)
    guard_result = _linting_mod._guard_handoff_path(path, target=target, policy=guard_policy) if guard else None
    return {
        "target": str(target),
        "created_at": created_at,
        "path": str(path),
        "id": path.stem,
        "inbox": inbox_label,
        "action": action,
        "target_card": target_card,
        "target_document": target_document,
        "lint": lint_result.as_dict(),
        "content_guard": guard_result,
        "valid": lint_result.valid and (guard_result is None or guard_result.get("exit_code") == 0),
    }
```

- [x] Replace the body of `draft()` (keep its signature unchanged) with:

```python
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        payload = build_draft(
            target=target,
            title=title,
            summary=summary,
            content=content,
            content_file=content_file,
            handoff_type=handoff_type,
            action=action,
            target_card=target_card,
            target_document=target_document,
            fact=fact,
            evidence=evidence,
            inbox=inbox,
            draft_id=draft_id,
            force=force,
            guard=guard,
            guard_policy=guard_policy,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    lint = payload["lint"]
    path = payload["path"]
    print(f"handoff draft: {path}")
    print(f"id: {payload['id']}")
    print(f"inbox: {payload['inbox']}")
    print(f"action: {payload['action']}")
    if payload["target_card"]:
        print(f"target_card: {payload['target_card']}")
    if payload["target_document"]:
        print(f"target_document: {payload['target_document']}")
    print(f"lint: {'ok' if lint['valid'] else 'fail'}")
    guard_result = payload["content_guard"]
    if guard_result is not None:
        guard_ok = guard_result.get("exit_code") == 0
        print(f"content_guard: {'ok' if guard_ok else 'fail'} ({guard_policy})")
        if not guard_ok:
            print(f"content_guard_detail: {guard_result.get('detail')}")
    for error in lint["errors"]:
        print(f"error: {error}")
    if not lint["valid"]:
        print(f"note: invalid draft kept at {path}; repair or delete it, then rerun `brigade handoff lint`.")
    return 0 if payload["valid"] else 1
```

- [x] Run to green: `PY -m pytest tests/test_handoff_build_draft.py tests/test_handoff_cmd.py -q -p no:cacheprovider`. Expect all PASS. The CLI messages are byte-identical to before because each `ValueError` carries the old text after `error: `.
- [x] Commit: `git add src/brigade/handoff_cmd/drafts.py tests/test_handoff_build_draft.py && git commit -m "refactor(handoff): expose build_draft for non-CLI callers"`

---

### Task 4: Memory MCP core, read tools

**Files:**
- Create: `src/brigade/memory_mcp_core.py`
- Test: `tests/test_memory_mcp_core.py`

- [x] Write the failing test `tests/test_memory_mcp_core.py`:

```python
"""Memory MCP tool contract across the repo and user stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import memory_mcp_core


def _card(root: Path, name: str, title: str, body: str) -> None:
    cards = root / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / name).write_text(f"---\ntitle: {title}\n---\n{body}\n")


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    user = home / ".brigade"
    repo = tmp_path / "repo"
    repo.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRIGADE_HOME", str(user))
    return repo, user


def _tools(repo: Path) -> memory_mcp_core.MemoryTools:
    return memory_mcp_core.MemoryTools(target=repo, inbox="claude")


def test_tool_specs_name_six_tools_with_annotations():
    specs = {spec["name"]: spec for spec in memory_mcp_core.tool_specs()}
    assert sorted(specs) == sorted(
        ["list_cards", "get_card", "search_cards", "memory_status", "work_brief", "write_handoff"]
    )
    assert specs["get_card"]["annotations"]["readOnlyHint"] is True
    assert specs["write_handoff"]["annotations"]["readOnlyHint"] is False
    assert specs["write_handoff"]["annotations"]["destructiveHint"] is False


def test_list_cards_labels_origin_and_honors_scope(stores):
    repo, user = stores
    _card(repo, "a.md", "Repo card", "repo body")
    _card(user, "b.md", "User card", "user body")
    tools = _tools(repo)
    items, failed = tools.call("list_cards", {})
    assert failed is False
    assert {(i["origin"], i["ref"]) for i in items} == {
        ("repo", "repo:memory/cards/a.md"),
        ("user", "user:memory/cards/b.md"),
    }
    repo_only, _ = tools.call("list_cards", {"scope": "repo"})
    assert [i["origin"] for i in repo_only] == ["repo"]
    assert tools.call("list_cards", {"scope": "nope"}) == ("scope must be one of: repo, user, all", True)


def test_get_card_resolves_repo_first_then_user(stores):
    repo, user = stores
    _card(repo, "same.md", "Repo", "from repo")
    _card(user, "same.md", "User", "from user")
    _card(user, "only.md", "Only", "user only")
    tools = _tools(repo)
    assert "from repo" in tools.call("get_card", {"path": "memory/cards/same.md"})[0]
    assert "from repo" in tools.call("get_card", {"path": "card://memory/cards/same.md"})[0]
    assert "from user" in tools.call("get_card", {"path": "user:memory/cards/same.md"})[0]
    assert "user only" in tools.call("get_card", {"path": "memory/cards/only.md"})[0]
    assert tools.call("get_card", {"path": "repo:memory/cards/only.md"}) == (
        "card not found: repo:memory/cards/only.md",
        True,
    )


def test_get_card_refuses_traversal_and_symlink_escape(stores, tmp_path: Path):
    repo, _ = stores
    _card(repo, "real.md", "Real", "ok")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    (repo / "memory" / "cards" / "evil.md").symlink_to(secret)
    tools = _tools(repo)
    assert tools.call("get_card", {"path": "memory/cards/evil.md"})[1] is True
    assert tools.call("get_card", {"path": "../secret.txt"})[1] is True


def test_search_ranks_repo_first_on_ties(stores):
    repo, user = stores
    _card(repo, "r.md", "Vlan notes", "trunk")
    _card(user, "u.md", "Vlan notes", "trunk")
    matches, failed = _tools(repo).call("search_cards", {"query": "vlan"})
    assert failed is False
    assert [m["origin"] for m in matches] == ["repo", "user"]
    assert matches[0]["ref"] == "repo:memory/cards/r.md"


def test_resources_use_card_uris_per_origin(stores):
    repo, user = stores
    _card(repo, "a.md", "A", "a")
    _card(user, "b.md", "B", "b")
    tools = _tools(repo)
    uris = {r["uri"] for r in tools.resources()}
    assert uris == {"card://memory/cards/a.md", "card://user:memory/cards/b.md"}
    text, mime = tools.read_resource("card://user:memory/cards/b.md")
    assert text is not None and mime == "text/markdown"


def test_memory_status_reports_stores_and_config_error(stores):
    repo, user = stores
    _card(user, "b.md", "B", "b")
    status, failed = _tools(repo).call("memory_status", {})
    assert failed is False
    assert status["repo"]["root"] == str(repo.resolve())
    assert status["repo"]["wired"] is False
    assert status["user"]["card_count"] == 1
    assert status["claude_hooks_installed"] is False
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "memory-care.toml").write_text("card_roots = 7\n")
    degraded, _ = _tools(repo).call("memory_status", {})
    assert degraded["repo"]["config_error"]
    assert degraded["repo"]["card_count"] == 0


def test_unknown_tool_is_a_tool_error(stores):
    repo, _ = stores
    assert _tools(repo).call("nope", {}) == ("unknown tool: nope", True)
    assert json.dumps(_tools(repo).call("list_cards", {})[0]) == "[]"
```

- [x] Run it, watch it fail: `PY -m pytest tests/test_memory_mcp_core.py -q -p no:cacheprovider`. Expect FAIL with `ImportError: cannot import name 'memory_mcp_core'`.
- [x] Create `src/brigade/memory_mcp_core.py`:

```python
"""Brigade memory MCP tools, independent of transport.

One contract serves the stdio and HTTP transports. Read tools span the repo and
user stores (repo first); write_handoff drafts a Memory Handoff, which is also
the reviewed route for proposing card changes. Calls return (payload, is_error),
the shape mcp_server.ToolCaller expects.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import mcp_server, memory_cmd, memory_scope
from .memory_scope import REPO, USER, MemoryStore, Resolution

SERVER_NAME = "brigade-memory"
CARD_URI_PREFIX = "card://"
SEARCH_LIMIT = 20
SCOPES = ("repo", "user", "all")

_READ = {"readOnlyHint": True}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}

ToolResult = tuple[object, bool]


def card_ref(origin: str, rel: str) -> str:
    return f"{origin}:{rel}"


def card_uri(origin: str, rel: str) -> str:
    return f"{CARD_URI_PREFIX}{rel}" if origin == REPO else f"{CARD_URI_PREFIX}{USER}:{rel}"


def split_ref(ref: str) -> tuple[str | None, str]:
    """Parse repo:<rel>, user:<rel>, card://<ref>, or a bare path (origin None)."""
    if ref.startswith(CARD_URI_PREFIX):
        ref = ref[len(CARD_URI_PREFIX) :]
    for origin in (REPO, USER):
        prefix = f"{origin}:"
        if ref.startswith(prefix):
            return origin, ref[len(prefix) :]
    return None, ref


def _read_in_store(store: MemoryStore, rel: str) -> str | None:
    candidate = (store.root / rel).resolve()
    # Only files whose real path stays inside the store; _iter_cards already
    # skips symlinks, so a .md link pointing outside never matches.
    if not candidate.is_relative_to(store.root):
        return None
    for path in memory_cmd._iter_cards(store.root, store.config):
        if path.resolve() == candidate:
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    return None


def _scope(arguments: dict[str, Any]) -> str:
    value = arguments.get("scope", "all")
    if value not in SCOPES:
        raise ValueError("scope must be one of: " + ", ".join(SCOPES))
    return str(value)


def tool_specs() -> list[dict[str, Any]]:
    scope = {"type": "string", "enum": list(SCOPES), "description": "Store to read. Default all."}
    strings = {"type": "array", "items": {"type": "string"}}
    return [
        {
            "name": "list_cards",
            "description": "List memory cards from the repo and user stores, labeled by origin.",
            "inputSchema": {"type": "object", "properties": {"scope": scope}},
            "annotations": _READ,
        },
        {
            "name": "get_card",
            "description": "Read one memory card. Accepts repo:<path>, user:<path>, card://<path>, "
            "or a bare path (repo store first, then user).",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            "annotations": _READ,
        },
        {
            "name": "search_cards",
            "description": "Keyword-search memory cards across both stores; repo matches rank first on ties.",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
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

    def handlers(self) -> mcp_server.Handlers:
        return mcp_server.Handlers(
            server_name=SERVER_NAME,
            list_resources=self.resources,
            read_resource=self.read_resource,
            list_tools=tool_specs,
            call_tool=self.call,
        )

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
            for path in memory_cmd._iter_cards(store.root, store.config):
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
        except ValueError as exc:
            return str(exc), True
        except OSError as exc:
            return str(exc), True

    def _tool_table(self) -> dict[str, Callable[[dict[str, Any]], ToolResult]]:
        return {
            "list_cards": self._list_cards,
            "get_card": self._get_card,
            "search_cards": self._search_cards,
            "memory_status": self._memory_status,
        }

    def _list_cards(self, arguments: dict[str, Any]) -> ToolResult:
        scope = _scope(arguments)
        items: list[dict[str, Any]] = []
        for store in self.resolution().stores:
            if scope != "all" and store.origin != scope:
                continue
            for path in memory_cmd._iter_cards(store.root, store.config):
                rel = str(path.relative_to(store.root))
                items.append(
                    {"path": rel, "uri": card_uri(store.origin, rel), "origin": store.origin, "ref": card_ref(store.origin, rel)}
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
            for match in memory_cmd.search_cards_payload(store.root, query, limit=SEARCH_LIMIT)["matches"]:
                ranked.append((rank, {**match, "origin": store.origin, "ref": card_ref(store.origin, match["path"])}))
        ranked.sort(key=lambda item: (-item[1]["score"], item[0], item[1]["path"]))
        return [match for _, match in ranked[:SEARCH_LIMIT]], False

    def _memory_status(self, arguments: dict[str, Any]) -> ToolResult:
        res = self.resolution()
        counts = {store.origin: len(memory_cmd._iter_cards(store.root, store.config)) for store in res.stores}
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
            "handoff_inbox": self._inbox,
        }, False
```

- [x] Run to green: same command as the failing run. Expect 8 PASS.
- [x] Lint and type check: `PY -m ruff check src/brigade/memory_mcp_core.py src/brigade/memory_scope.py src/brigade/mcp_server.py` (expect `All checks passed!`), then `PY -m mypy src/brigade/memory_mcp_core.py src/brigade/memory_scope.py src/brigade/mcp_server.py` (expect `Success: no issues found`). The repo's ruff config does not enforce line length (`select = ["B", "E4", "E7", "E9", "F"]`).
- [x] Commit: `git add src/brigade/memory_mcp_core.py tests/test_memory_mcp_core.py && git commit -m "feat(memory): add memory MCP core read tools"`

---

### Task 5: Memory MCP core, `write_handoff` and `work_brief`

**Files:**
- Modify: `src/brigade/memory_mcp_core.py`
- Test: `tests/test_memory_mcp_core.py` (append)

- [x] Append the failing tests to `tests/test_memory_mcp_core.py`:

```python
def test_write_handoff_lands_in_repo_inbox(stores):
    repo, _ = stores
    result, failed = _tools(repo).call(
        "write_handoff", {"title": "Plugin memory", "summary": "Short summary.", "content": "Body.", "facts": ["f1"]}
    )
    assert failed is False
    assert result["written"] is True and result["origin"] == "repo"
    assert result["path"].startswith(".claude/memory-handoffs/")
    assert (repo / result["path"]).is_file()


def test_write_handoff_without_repo_uses_user_store(stores, monkeypatch, tmp_path: Path):
    _, user = stores
    from brigade import memory_scope

    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    tools = memory_mcp_core.MemoryTools(cwd=tmp_path, inbox="claude")
    result, failed = tools.call("write_handoff", {"title": "T", "summary": "S.", "content": "Body."})
    assert failed is False and result["origin"] == "user"
    assert result["path"].startswith("memory/handoffs/")
    assert (user / result["path"]).is_file()
    assert (user / "memory").stat().st_mode & 0o777 == 0o700


def test_write_handoff_rejects_bad_input_and_removes_invalid_drafts(stores):
    repo, _ = stores
    tools = _tools(repo)
    assert tools.call("write_handoff", {"summary": "S.", "content": "Body."}) == ("title is required", True)
    assert tools.call("write_handoff", {"title": "T", "summary": "S.", "content": "Body.", "facts": "x"}) == (
        "facts must be a list of strings",
        True,
    )
    result, failed = tools.call(
        "write_handoff",
        {"title": "Card", "summary": "S.", "content": "no frontmatter", "action": "create-card", "target_card": "c.md"},
    )
    assert failed is True and result["written"] is False
    assert any("frontmatter" in error for error in result["errors"])
    inbox = repo / ".claude" / "memory-handoffs"
    assert not inbox.exists() or not list(inbox.glob("*.md"))


def test_work_brief_outside_wired_repo_points_at_user_store(stores, monkeypatch, tmp_path: Path):
    from brigade import memory_scope

    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    result, failed = memory_mcp_core.MemoryTools(cwd=tmp_path).call("work_brief", {})
    assert failed is False and result["wired"] is False
    assert result["user_store"].endswith(".brigade/memory")


def test_work_brief_in_wired_repo_returns_brief_subset(stores, monkeypatch):
    repo, _ = stores
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "config.json").write_text("{}")
    from brigade.work_cmd.session import briefing

    fake = {"git": {"branch": "main"}, "active_session": None, "latest_session": None, "next": "ship", "extra": 1}
    monkeypatch.setattr(briefing, "_brief_payload", lambda target, limit=3: fake)
    result, failed = _tools(repo).call("work_brief", {})
    assert failed is False
    assert result == {
        "wired": True,
        "root": str(repo.resolve()),
        "git": {"branch": "main"},
        "active_session": None,
        "latest_session": None,
        "next": "ship",
    }
```

- [x] Run, watch them fail: `PY -m pytest tests/test_memory_mcp_core.py -q -p no:cacheprovider`. Expect the five new tests to FAIL with `unknown tool: write_handoff` / `unknown tool: work_brief` assertion mismatches.
- [x] In `src/brigade/memory_mcp_core.py`, add these imports below the existing ones:

```python
from .handoff_cmd import drafts as handoff_drafts
from .handoff_cmd.models import DEFAULT_DRAFT_DOCUMENT
```

  add these module constants below `SCOPES`:

```python
BRIEF_KEYS = ("git", "active_session", "latest_session", "next")
UNWIRED_HINT = (
    "This directory is not a Brigade-wired repo, so memory tools use the user store. "
    "Run the Brigade enforce skill to wire this repo into the full work loop."
)
```

  add these helpers above `def tool_specs`:

```python
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
    return list(value)
```

  extend `_tool_table` with `"work_brief": self._work_brief,` and `"write_handoff": self._write_handoff,`, and add these methods to `MemoryTools`:

```python
    def _work_brief(self, arguments: dict[str, Any]) -> ToolResult:
        res = self.resolution()
        if res.repo_root is None or not res.repo_wired:
            return {
                "wired": False,
                "user_store": str(memory_scope.user_root() / "memory"),
                "hint": UNWIRED_HINT,
            }, False
        from .work_cmd.session import briefing

        payload = briefing._brief_payload(res.repo_root, limit=3)
        return {"wired": True, "root": str(res.repo_root), **{key: payload.get(key) for key in BRIEF_KEYS}}, False

    def _write_handoff(self, arguments: dict[str, Any]) -> ToolResult:
        title = _required_text(arguments, "title")
        summary = _required_text(arguments, "summary")
        content = _required_text(arguments, "content")
        target_card = _optional_text(arguments, "target_card")
        target_document = _optional_text(arguments, "target_document") or DEFAULT_DRAFT_DOCUMENT
        facts = _string_list(arguments, "facts")
        evidence = _string_list(arguments, "evidence")
        repo = next((store for store in self.resolution().stores if store.origin == REPO), None)
        if repo is not None:
            root, inbox, origin = repo.root, self._inbox, REPO
        else:
            root = memory_scope.user_root()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            (root / "memory").mkdir(mode=0o700, exist_ok=True)
            inbox, origin = str(root / memory_scope.USER_HANDOFF_INBOX), USER
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
        path = Path(payload["path"])
        if not payload["valid"]:
            path.unlink(missing_ok=True)
            return {"written": False, "errors": payload["lint"]["errors"]}, True
        return {
            "written": True,
            "origin": origin,
            "path": str(path.relative_to(root)),
            "id": payload["id"],
            "action": payload["action"],
        }, False
```

- [x] Run to green: same command. Expect 13 PASS.
- [x] Lint and types: `PY -m ruff check src/brigade/memory_mcp_core.py` then `PY -m mypy src/brigade/memory_mcp_core.py`. Expect clean.
- [x] Commit: `git add src/brigade/memory_mcp_core.py tests/test_memory_mcp_core.py && git commit -m "feat(memory): add write_handoff and work_brief MCP tools"`

---

### Task 6: Loopback HTTP transport

**Files:**
- Create: `src/brigade/mcp_http.py`
- Test: `tests/test_mcp_http.py`

- [x] Write the failing test `tests/test_mcp_http.py`:

```python
"""Loopback Streamable HTTP transport: binding, auth, origin, token files, parity."""

from __future__ import annotations

import io
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brigade import mcp_http, mcp_server, memory_mcp_core

TOKEN = "test-token-value"


def _handlers() -> mcp_server.Handlers:
    return mcp_server.Handlers(
        server_name="t",
        list_resources=lambda: [],
        read_resource=lambda uri: (None, None),
        list_tools=lambda: [{"name": "echo"}],
        call_tool=lambda name, args: ({"name": name}, False),
    )


@pytest.fixture
def server():
    srv = mcp_http.build_server(_handlers(), host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _post(url: str, body: object, headers: dict[str, str] | None = None, method: str = "POST") -> tuple[int, object]:
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data if method == "POST" else None, method=method)
    for key, value in {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", **(headers or {})}.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def test_parse_bind_accepts_only_loopback():
    assert mcp_http.parse_bind("127.0.0.1:8790") == ("127.0.0.1", 8790)
    assert mcp_http.parse_bind("[::1]:0") == ("::1", 0)
    for bad in ("0.0.0.0:8790", "203.0.113.5:80", "localhost:8790", "127.0.0.1", "127.0.0.1:99999"):
        with pytest.raises(ValueError):
            mcp_http.parse_bind(bad)


def test_round_trip_and_notification(server):
    status, body = _post(f"{server}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200 and body["result"]["tools"] == [{"name": "echo"}]
    status, body = _post(f"{server}/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and body is None


def test_auth_origin_path_method_and_batch(server):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert _post(f"{server}/mcp", ping, {"Authorization": "Bearer wrong"})[0] == 401
    assert _post(f"{server}/mcp", ping, {"Authorization": ""})[0] == 401
    assert _post(f"{server}/mcp", ping, {"Origin": "https://evil.example"})[0] == 403
    assert _post(f"{server}/mcp", ping, {"Origin": "http://localhost:3000"})[0] == 200
    assert _post(f"{server}/other", ping)[0] == 404
    assert _post(f"{server}/mcp", ping, method="GET")[0] == 405
    assert _post(f"{server}/mcp", [ping])[0] == 400


def test_ensure_token_creates_private_file_and_reuses_it(tmp_path: Path):
    path = tmp_path / "home" / "mcp-http.token"
    mcp_http.ensure_token(path)
    assert path.stat().st_mode & 0o777 == 0o600
    first = mcp_http.read_token(path)
    mcp_http.ensure_token(path)
    assert mcp_http.read_token(path) == first and len(first) >= 32


def test_read_token_refuses_loose_modes_and_symlinks(tmp_path: Path):
    loose = tmp_path / "loose"
    loose.write_text("x\n")
    os.chmod(loose, 0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        mcp_http.read_token(loose)
    real = tmp_path / "real"
    real.write_text("x\n")
    os.chmod(real, 0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        mcp_http.read_token(link)


def test_stdio_and_http_answer_identically(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    repo = tmp_path / "repo"
    cards = repo / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "a.md").write_text("---\ntitle: Vlan\n---\ntrunk\n")
    handlers = memory_mcp_core.MemoryTools(target=repo).handlers()
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_cards", "arguments": {"query": "vlan"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "card://memory/cards/a.md"}},
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    mcp_server.serve_stdio_handlers(handlers)
    stdio_replies = [json.loads(line) for line in out.getvalue().splitlines()]
    srv = mcp_http.build_server(handlers, host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
        http_replies = [_post(url, r)[1] for r in requests]
    finally:
        srv.shutdown()
        srv.server_close()
    assert http_replies == stdio_replies
```

- [x] Run, watch it fail: `PY -m pytest tests/test_mcp_http.py -q -p no:cacheprovider`. Expect FAIL with `ImportError: cannot import name 'mcp_http'`.
- [x] Create `src/brigade/mcp_http.py`:

```python
"""Loopback Streamable HTTP transport for the shared MCP dispatcher.

Phase 1 binds only to loopback, requires a bearer token read from a private
file, rejects non-loopback Origin headers (DNS rebinding), and answers each POST
with one JSON body: no server-sent events and no sessions.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import stat
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .mcp_server import Handlers, dispatch, response

LOOPBACK_HOSTS = ("127.0.0.1", "::1")
ORIGIN_HOSTS = ("127.0.0.1", "::1", "localhost")
MCP_PATH = "/mcp"
MAX_BODY_BYTES = 1024 * 1024


def parse_bind(value: str) -> tuple[str, int]:
    host, sep, port_text = value.rpartition(":")
    host = host.strip("[]")
    if not sep or host not in LOOPBACK_HOSTS:
        raise ValueError(f"--http must bind to 127.0.0.1 or [::1], got {value!r}")
    if not port_text.isdigit() or int(port_text) > 65535:
        raise ValueError(f"--http port must be 0-65535, got {port_text!r}")
    return host, int(port_text)


def read_token(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"bearer file must be a regular file, not a symlink: {path}")
    if info.st_mode & 0o077:
        raise ValueError(f"bearer file must be mode 0600: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"bearer file is empty: {path}")
    return token


def ensure_token(path: Path) -> Path:
    """Create a random bearer token at path (mode 0600) unless one already exists."""
    if path.exists() or path.is_symlink():
        read_token(path)
        return path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_urlsafe(32) + "\n")
    return path


def _origin_allowed(origin: str | None) -> bool:
    if origin is None:
        return True
    return urlsplit(origin).hostname in ORIGIN_HOSTS


def make_handler(handlers: Handlers, token: str) -> type[BaseHTTPRequestHandler]:
    expected = f"Bearer {token}".encode()

    class McpRequestHandler(BaseHTTPRequestHandler):
        server_version = "brigade-mcp"

        def log_message(self, format: str, *args: object) -> None:
            return  # keep request lines out of logs; tool arguments are user content

        def _send(self, status: int, payload: object | None = None) -> None:
            body = b"" if payload is None else json.dumps(payload, sort_keys=True).encode()
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _admitted(self) -> bool:
            if urlsplit(self.path).path != MCP_PATH:
                self._send(404, {"error": "not found"})
                return False
            if not _origin_allowed(self.headers.get("Origin")):
                self._send(403, {"error": "origin not allowed"})
                return False
            supplied = (self.headers.get("Authorization") or "").encode()
            if not hmac.compare_digest(supplied, expected):
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            if self._admitted():
                self._send(405, {"error": "use POST"})

        do_PUT = do_GET
        do_PATCH = do_GET
        do_DELETE = do_GET

        def do_POST(self) -> None:
            if not self._admitted():
                return
            length_text = self.headers.get("Content-Length") or ""
            if not length_text.isdigit():
                self._send(411, {"error": "content length required"})
                return
            if int(length_text) > MAX_BODY_BYTES:
                self._send(413, {"error": "request too large"})
                return
            raw = self.rfile.read(int(length_text))
            try:
                request = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, response(None, error={"code": -32700, "message": "parse error"}))
                return
            if isinstance(request, list):
                self._send(400, response(None, error={"code": -32600, "message": "batch requests are not supported"}))
                return
            reply = dispatch(request, handlers)
            if reply is None:
                self._send(202)
                return
            self._send(200, reply)

    return McpRequestHandler


class _LoopbackServer(HTTPServer):
    def __init__(self, host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> None:
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, port), handler)


def build_server(handlers: Handlers, *, host: str, port: int, token: str) -> HTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing non-loopback bind: {host}")
    return _LoopbackServer(host, port, make_handler(handlers, token))


def serve_http(handlers: Handlers, *, host: str, port: int, token: str) -> int:
    server = build_server(handlers, host=host, port=port, token=token)
    shown = f"[{host}]" if ":" in host else host
    print(f"brigade memory MCP listening on http://{shown}:{server.server_address[1]}{MCP_PATH}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
```

- [x] Run to green: same command. Expect 6 PASS.
- [x] Lint and types: `PY -m ruff check src/brigade/mcp_http.py tests/test_mcp_http.py` and `PY -m mypy src/brigade/mcp_http.py`. Expect `All checks passed!` and `Success: no issues found`.
- [x] Commit: `git add src/brigade/mcp_http.py tests/test_mcp_http.py && git commit -m "feat(mcp): add loopback bearer-gated HTTP transport"`

---

### Task 6b: Security hardening from the Daybreak review

Added after the read-only Daybreak review of Tasks 4-6 (0 critical, 0 high, 5 medium, 3 low). Each fix gets a failing test first, in the existing test file for that module.

**Files:**
- Modify: `src/brigade/memory_mcp_core.py`, `src/brigade/mcp_http.py`, `src/brigade/handoff_cmd/drafts.py`
- Test: `tests/test_memory_mcp_core.py`, `tests/test_mcp_http.py`, `tests/test_handoff_build_draft.py`

- [x] M1 card enumeration containment: `list_cards`, `search_cards`, `resources()` and `_read_in_store` only return cards whose resolved path stays under the resolved `store.root`. A store whose `memory/cards` directory is itself a symlink to an outside directory yields no cards from it. Do not change `memory_cmd._iter_cards` (CLI behavior stays as is); filter in the core with one helper. Read the file through a no-follow open (reuse `card_fingerprint.read_text_nofollow` or `managed_block.read_text_nofollow` if its contract fits).
- [x] M2 handoff destination: in `write_handoff`, refuse (tool error) when the resolved inbox is not under the resolved store root, which covers a symlinked `.claude/memory-handoffs`, `.codex/memory-handoffs`, or user `memory/handoffs`. In `build_draft`, when `force` is false, create the file with `O_CREAT | O_EXCL | O_NOFOLLOW` (mode 0600) instead of `exists()` then `write_text()`, and raise the same `handoff draft already exists: <path>` ValueError on `FileExistsError`. Keep `force=True` behavior. `brigade handoff draft` CLI output stays byte-identical; the existing handoff tests must stay green.
- [x] M3 bearer file: `read_token` opens once with `O_RDONLY | O_NOFOLLOW`, validates the descriptor with `fstat` (regular file, owned by the current uid, no group/other bits), reads at most 4096 bytes from it, and keeps the existing error messages. `ensure_token` keeps `O_EXCL` creation.
- [x] M4 slow clients: the request handler sets a socket timeout (class attribute `timeout = 10`) so a partial request cannot hold the single-threaded server; a test opens a raw socket, sends half a request line, and asserts the server answers a second client within the deadline (patch the timeout down to keep the test fast).
- [x] M5 user store modes: when `write_handoff` writes to the user store, the inbox directory is created 0700 and the handoff file is 0600. Do not chmod or reject a pre-existing `$BRIGADE_HOME`.
- [x] L1 Host and Origin: reject (403) a `Host` header that is not a loopback literal or `localhost` (port optional); a malformed `Origin` or a non-http(s) scheme is a 403, never a 500.
- [x] L2 parser failures: `ValueError` and `RecursionError` from `json.loads` return a fixed 400 parse error with no traceback on stderr.
- [x] L3 size bounds: `write_handoff` rejects `content` over 256 KiB and `facts`/`evidence` lists over 50 items with a tool error.
- [x] Run to green: `PY -m pytest tests/test_memory_mcp_core.py tests/test_mcp_http.py tests/test_handoff_build_draft.py tests/test_handoff_cmd.py tests/test_memory_search_mcp.py -q -p no:cacheprovider`, then ruff check, ruff format --check and mypy on the three modules.
- [x] Commit: `fix(mcp): harden memory MCP transports per security review`

---

### Task 7: Wire `serve-mcp` to the core and both transports

**Files:**
- Modify: `src/brigade/memory_cmd.py:1888-2012` (from the `# --- read-only MCP server` comment to the end)
- Modify: `src/brigade/cli/memory.py:300-309` and `:496-497`
- Modify: `tests/test_memory_search_mcp.py:10,166-215`
- Modify: `tests/test_module_size_ratchet.py:22`

- [x] In `tests/test_memory_search_mcp.py`, replace lines 9-10 with `from brigade import cli, memory_cmd, memory_mcp_core` (the old names are only used by the tests replaced below), and add `import pytest` after `import json`. Replace the three tests from `def test_card_mcp_resources_and_read_are_scoped` to the end of the file with:

```python
def test_card_mcp_resources_and_read_are_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "brigade-home"))
    _card(tmp_path, "a.md", "Card A", "hello world")
    tools = memory_mcp_core.MemoryTools(target=tmp_path)
    assert any(r["uri"] == "card://memory/cards/a.md" for r in tools.resources())
    text, mime = tools.read_resource("card://memory/cards/a.md")
    assert text is not None and "hello world" in text and mime == "text/markdown"
    assert tools.read_resource("card://../secret.md") == (None, None)


def test_card_mcp_read_refuses_symlink_escape(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "brigade-home"))
    target = tmp_path / "ws"
    _card(target, "real.md", "Real", "ok content")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    (target / "memory" / "cards" / "evil.md").symlink_to(secret)
    tools = memory_mcp_core.MemoryTools(target=target)
    assert tools.read_resource("card://memory/cards/evil.md") == (None, None)
    text, _ = tools.read_resource("card://memory/cards/real.md")
    assert text is not None and "ok content" in text


def test_card_mcp_stdio_roundtrip(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "brigade-home"))
    _card(tmp_path, "a.md", "Card A", "hello mcp")
    requests = (
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/list"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "get_card", "arguments": {"path": "memory/cards/a.md"}},
                    }
                ),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(requests))
    rc = memory_cmd.serve_mcp(target=tmp_path, stdio=True)
    assert rc == 0
    responses = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "brigade-memory"
    assert any(r["uri"] == "card://memory/cards/a.md" for r in responses[1]["result"]["resources"])
    assert "hello mcp" in responses[2]["result"]["content"][0]["text"]


def test_serve_mcp_cli_refuses_non_loopback_and_both_transports(tmp_path: Path, capsys):
    assert cli.main(["memory", "serve-mcp", "--target", str(tmp_path), "--http", "0.0.0.0:8790"]) == 2
    assert "127.0.0.1 or [::1]" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["memory", "serve-mcp", "--stdio", "--http", "127.0.0.1:8790"])


def test_serve_mcp_print_token_path_creates_private_token(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "bh"))
    assert cli.main(["memory", "serve-mcp", "--print-token-path"]) == 0
    printed = Path(capsys.readouterr().out.strip())
    assert printed == (tmp_path / "bh").resolve() / "mcp-http.token"
    assert printed.stat().st_mode & 0o777 == 0o600


def test_serve_mcp_contract_lists_six_tools(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "bh"))
    assert cli.main(["memory", "serve-mcp", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["read_only"] is False
    assert len(payload["tools"]) == 6
    assert [s["origin"] for s in payload["stores"]] == ["repo", "user"]
```

- [x] Run, watch them fail: `PY -m pytest tests/test_memory_search_mcp.py -q -p no:cacheprovider`. Expect the new CLI tests and the stdio roundtrip to FAIL (`unrecognized arguments: --http`, server name `brigade-memory-readonly`).
- [x] In `src/brigade/memory_cmd.py`, delete everything from the line `# --- read-only MCP server exposing memory cards over card:// ---` through the end of `_run_card_mcp_stdio()` (the `_CARD_URI_PREFIX`, `_card_uri`, `_mcp_card_resources`, `_mcp_read_card`, `_mcp_card_tool_specs`, `_mcp_card_tool_call`, `_run_card_mcp_stdio` definitions). Replace `serve_mcp()` with:

```python
# --- memory MCP server (stdio or loopback HTTP); the tool contract lives in memory_mcp_core ---


def serve_mcp(
    *,
    target: Path | None = None,
    stdio: bool = False,
    http: str | None = None,
    bearer_file: Path | None = None,
    print_token_path: bool = False,
    inbox: str = "codex",
    json_output: bool = False,
) -> int:
    from . import mcp_http, memory_mcp_core, memory_scope

    if print_token_path:
        try:
            if bearer_file is not None:
                mcp_http.read_token(bearer_file)
                token_path = bearer_file
            else:
                token_path = mcp_http.ensure_token(memory_scope.http_token_path())
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(token_path)
        return 0
    tools = memory_mcp_core.MemoryTools(target=target, inbox=inbox)
    if stdio:
        return mcp_server.serve_stdio_handlers(tools.handlers())
    if http is not None:
        try:
            host, port = mcp_http.parse_bind(http)
            token_file = bearer_file if bearer_file is not None else mcp_http.ensure_token(memory_scope.http_token_path())
            token = mcp_http.read_token(token_file)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return mcp_http.serve_http(tools.handlers(), host=host, port=port, token=token)
    resolution = tools.resolution()
    if resolution.repo_config_error:
        print(f"error: invalid memory-care config: {resolution.repo_config_error}", file=sys.stderr)
        return 2
    tool_names = [str(spec["name"]) for spec in memory_mcp_core.tool_specs()]
    payload: dict[str, Any] = {
        "target": str(resolution.repo_root) if resolution.repo_root is not None else None,
        "read_only": False,
        "resource_scheme": "card://<repo-relative-path> and card://user:<path>",
        "tools": tool_names,
        "stores": [{"origin": store.origin, "root": str(store.root)} for store in resolution.stores],
        "registered_resources": len(tools.resources()),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("memory MCP: ready")
    for store in payload["stores"]:
        print(f"store: {store['origin']} {store['root']}")
    print(f"tools: {', '.join(tool_names)}")
    print(f"registered_resources: {payload['registered_resources']}")
    print("run over stdio with: brigade memory serve-mcp --stdio")
    print("or loopback HTTP with: brigade memory serve-mcp --http 127.0.0.1:8790")
    return 0
```

  Keep the `mcp_server` import at the top of `memory_cmd.py`; it is still used.

- [x] In `src/brigade/cli/memory.py`, replace lines 300-309 (the `serve-mcp` parser) with:

```python
    p_memory_serve_mcp = memory_sub.add_parser(
        "serve-mcp", help="Serve Brigade memory over MCP (stdio or loopback HTTP); prints the contract otherwise."
    )
    p_memory_serve_mcp.add_argument(
        "--target",
        "-t",
        type=Path,
        default=None,
        help="Repo whose cards form the repo store. Default: nearest Brigade-wired repo above the cwd.",
    )
    serve_transport = p_memory_serve_mcp.add_mutually_exclusive_group()
    serve_transport.add_argument("--stdio", action="store_true", help="Run the JSON-RPC stdio server.")
    serve_transport.add_argument(
        "--http", metavar="HOST:PORT", default=None, help="Run Streamable HTTP on loopback, e.g. 127.0.0.1:8790."
    )
    serve_transport.add_argument(
        "--print-token-path",
        action="store_true",
        help="Create the HTTP bearer token file if missing and print its path (never the token).",
    )
    p_memory_serve_mcp.add_argument(
        "--bearer-file", type=Path, default=None, help="Existing mode-0600 bearer token file for --http."
    )
    p_memory_serve_mcp.add_argument(
        "--inbox", default="codex", help="Handoff inbox alias or path for write_handoff in the repo store."
    )
    p_memory_serve_mcp.add_argument("--json", action="store_true", help="Print the contract as JSON.")
```

  and replace the dispatch at lines 496-497 with:

```python
    if args.memory_command == "serve-mcp":
        return memory_cmd.serve_mcp(
            target=args.target,
            stdio=args.stdio,
            http=args.http,
            bearer_file=args.bearer_file,
            print_token_path=args.print_token_path,
            inbox=args.inbox,
            json_output=args.json,
        )
```

- [x] Count lines: `wc -l src/brigade/memory_cmd.py`. Expect about 1955 (2012 minus the 125-line block plus the 68-line `serve_mcp`). Delete the line `"src/brigade/memory_cmd.py": 2093,` from `LEGACY_OVERSIZED` in `tests/test_module_size_ratchet.py`; `test_allowlist_only_shrinks` fails while a module under 2000 lines keeps an entry.
- [x] Run to green: `PY -m pytest tests/test_memory_search_mcp.py tests/test_memory_mcp_core.py tests/test_mcp_http.py tests/test_module_size_ratchet.py tests/test_cli_inventory_contract.py tests/test_harness_user_scope.py tests/test_harness_user_scope_slice2.py -q -p no:cacheprovider`. Expect all PASS. The command inventory lists command names only, so new flags do not change it.
- [x] Lint and types: `PY -m ruff check src/brigade/memory_cmd.py src/brigade/cli/memory.py` and `PY -m mypy src/brigade/memory_cmd.py src/brigade/cli/memory.py`. Expect clean.
- [x] Commit: `git add src/brigade/memory_cmd.py src/brigade/cli/memory.py tests/test_memory_search_mcp.py tests/test_module_size_ratchet.py && git commit -m "feat(memory): serve the memory MCP core over stdio and loopback HTTP"`

---

### Task 8: Plugin skills sync script

**Files:**
- Create: `scripts/sync_plugin_skills.py`
- Create (generated): `plugins/brigade/skills/brigade-work/SKILL.md`

- [x] Create `scripts/sync_plugin_skills.py`:

```python
#!/usr/bin/env python3
"""Copy canonical Brigade skills into the agent plugin; --check fails on drift.

The plugin directory is a build output for these files, never a place to edit
them. Edit src/brigade/templates/skills/<name>/SKILL.md, then run --write.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SYNCED = [
    ("src/brigade/templates/skills/brigade-work/SKILL.md", "plugins/brigade/skills/brigade-work/SKILL.md"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite plugin copies from the canonical sources")
    args = parser.parse_args()
    drift = []
    for source_rel, copy_rel in SYNCED:
        source = (ROOT / source_rel).read_bytes()
        copy = ROOT / copy_rel
        if copy.is_file() and copy.read_bytes() == source:
            continue
        if args.write:
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(source)
            print(f"wrote {copy_rel}")
        else:
            drift.append(copy_rel)
    for rel in drift:
        print(f"plugin skill drift: {rel} (run scripts/sync_plugin_skills.py --write)", file=sys.stderr)
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] Generate: `PY scripts/sync_plugin_skills.py --write`. Expect `wrote plugins/brigade/skills/brigade-work/SKILL.md`. Then `PY scripts/sync_plugin_skills.py`, expect exit 0 and no output.
- [x] Commit: `git add scripts/sync_plugin_skills.py plugins/brigade/skills/brigade-work/SKILL.md && git commit -m "build(plugin): sync the brigade-work skill into the plugin"`

---

### Task 9: Plugin package, version sync, and plugin tests

**Files:**
- Create: `plugins/brigade/.claude-plugin/plugin.json`
- Create: `plugins/brigade/.codex-plugin/plugin.json`
- Create: `plugins/brigade/.mcp.json`
- Create: `plugins/brigade/codex.mcp.json`
- Create: `plugins/brigade/hooks/hooks.json`
- Create: `plugins/brigade/hooks/session-start.sh` (mode 0755)
- Create: `plugins/brigade/skills/enforce/SKILL.md`
- Create: `plugins/brigade/README.md`
- Modify: `scripts/version_sync.py:42-71`
- Modify: `tests/test_version_sync_script.py:20`
- Test: `tests/test_agent_plugin.py`

- [x] Write the failing test `tests/test_agent_plugin.py`:

```python
"""Agent plugin package: manifests, pins, skill drift, and the SessionStart hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from brigade import toml_compat

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "brigade"
HOOK = PLUGIN / "hooks" / "session-start.sh"


def _version() -> str:
    return toml_compat.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _json(rel: str) -> dict:
    return json.loads((PLUGIN / rel).read_text())


def test_manifests_name_and_version_match_pyproject():
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        manifest = _json(rel)
        assert manifest["name"] == "brigade"
        assert manifest["version"] == _version()
    codex = _json(".codex-plugin/plugin.json")
    assert (PLUGIN / codex["skills"]).is_dir()
    assert (PLUGIN / codex["mcpServers"]).is_file()


def test_mcp_configs_pin_the_npm_package_and_inbox():
    for rel, inbox in ((".mcp.json", "claude"), ("codex.mcp.json", "codex")):
        server = _json(rel)["mcpServers"]["brigade-memory"]
        assert server["command"] == "npx"
        assert server["args"] == [
            "-y",
            f"@escoffier-labs/brigade@{_version()}",
            "memory",
            "serve-mcp",
            "--stdio",
            "--inbox",
            inbox,
        ]


def test_plugin_skills_match_canonical_sources():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/sync_plugin_skills.py")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (PLUGIN / "skills" / "enforce" / "SKILL.md").read_text().startswith("---\nname: enforce\n")


def _run_hook(tmp_path: Path, project: Path, path_dirs: list[Path]) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([*map(str, path_dirs), "/usr/bin", "/bin"]),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    return subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True, timeout=30)


def test_hook_exits_zero_without_brigade_on_path(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    result = _run_hook(tmp_path, project, [])
    assert result.returncode == 0
    assert "brigade-memory MCP tools" in result.stdout


def test_hook_prints_brief_in_wired_repo_and_skips_when_enforced(tmp_path: Path):
    project = tmp_path / "proj"
    (project / ".brigade").mkdir(parents=True)
    (project / ".brigade" / "config.json").write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "brigade"
    fake.write_text("#!/bin/sh\necho FAKE-BRIEF \"$@\"\n")
    fake.chmod(0o755)
    result = _run_hook(tmp_path, project, [bin_dir])
    assert result.returncode == 0
    assert "FAKE-BRIEF work brief --target" in result.stdout
    (project / ".brigade" / "claude-hooks.json").write_text("{}")
    assert _run_hook(tmp_path, project, [bin_dir]).stdout == ""
```

- [x] Run, watch it fail: `PY -m pytest tests/test_agent_plugin.py -q -p no:cacheprovider`. Expect FAIL with `FileNotFoundError` for `plugins/brigade/.claude-plugin/plugin.json`.
- [x] Create `plugins/brigade/.claude-plugin/plugin.json`:

```json
{
  "name": "brigade",
  "version": "0.29.0",
  "description": "Agent memory that works in every repo: search and read memory cards, write reviewed handoffs, and opt in to Brigade's verified work loop.",
  "author": {
    "name": "Solomon Neas"
  },
  "homepage": "https://brigade.tools",
  "repository": "https://github.com/escoffier-labs/brigade",
  "license": "MIT",
  "keywords": ["memory", "agent-memory", "handoffs", "mcp", "brigade"]
}
```

- [x] Create `plugins/brigade/.codex-plugin/plugin.json`:

```json
{
  "name": "brigade",
  "version": "0.29.0",
  "description": "Agent memory that works in every repo: search and read memory cards, write reviewed handoffs, and opt in to Brigade's verified work loop.",
  "author": {
    "name": "Solomon Neas",
    "url": "https://brigade.tools"
  },
  "homepage": "https://brigade.tools",
  "repository": "https://github.com/escoffier-labs/brigade",
  "license": "MIT",
  "keywords": ["memory", "agent-memory", "handoffs", "mcp", "brigade"],
  "skills": "./skills/",
  "mcpServers": "./codex.mcp.json",
  "interface": {
    "displayName": "Brigade",
    "shortDescription": "Agent memory across repos, with an opt-in verified work loop",
    "longDescription": "Brigade gives Codex durable memory: search and read memory cards from the current repo and your user store, write Memory Handoffs that are reviewed before they become cards, and turn on Brigade's verified work loop per repo when you want it.",
    "developerName": "Solomon Neas",
    "category": "Developer Tools",
    "capabilities": ["Read", "Write"],
    "websiteURL": "https://brigade.tools",
    "defaultPrompt": ["Search Brigade memory for what we already decided about this repo."]
  }
}
```

- [x] Create `plugins/brigade/.mcp.json`:

```json
{
  "mcpServers": {
    "brigade-memory": {
      "command": "npx",
      "args": ["-y", "@escoffier-labs/brigade@0.29.0", "memory", "serve-mcp", "--stdio", "--inbox", "claude"]
    }
  }
}
```

- [x] Create `plugins/brigade/codex.mcp.json` with the same content, except the last argument is `"codex"`.
- [x] Create `plugins/brigade/hooks/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

- [x] Create `plugins/brigade/hooks/session-start.sh` and run `chmod 755 plugins/brigade/hooks/session-start.sh`:

```bash
#!/usr/bin/env bash
# Brigade plugin SessionStart hook: add a short work brief to the session.
# It must never block a session, so every path exits 0.
set -u

project="${CLAUDE_PROJECT_DIR:-$PWD}"
claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# Brigade's enforced hooks already inject the brief; stay quiet when installed.
if [ -f "$claude_home/brigade/claude-hooks.json" ]; then
  exit 0
fi

root=""
probe="$project"
while [ -n "$probe" ] && [ "$probe" != "/" ] && [ "$probe" != "$HOME" ]; do
  if [ -f "$probe/.brigade/config.json" ]; then
    root="$probe"
    break
  fi
  probe="$(dirname "$probe")"
done

if [ -n "$root" ] && [ -f "$root/.brigade/claude-hooks.json" ]; then
  exit 0
fi

if [ -n "$root" ] && command -v brigade >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 brigade work brief --target "$root" 2>/dev/null | head -n 40
  else
    brigade work brief --target "$root" 2>/dev/null | head -n 40
  fi
  exit 0
fi

echo "Brigade memory is available through the brigade-memory MCP tools (search_cards, get_card, write_handoff). Memory outside a Brigade-wired repo lives in ${BRIGADE_HOME:-$HOME/.brigade}/memory."
exit 0
```

- [x] Create `plugins/brigade/skills/enforce/SKILL.md`:

```markdown
---
name: enforce
description: Use when the user asks to turn on Brigade's full work loop (verification capture, raw-check blocking, and the handoff-before-stop gate) in the current repository.
---

# Turn on the Brigade work loop

The Brigade plugin starts in memory-first mode. This skill wires the current repository into Brigade's enforced loop.

1. Run `brigade --version`. If the command is missing, tell the user to install the CLI with `pipx install brigade-cli` or `npm install -g @escoffier-labs/brigade`, then stop.
2. Use `claude` as the harness name in Claude Code and `codex` in Codex.
3. Show the user this command and explain it before running it. It writes `.brigade/` into the repository, installs that harness's Brigade hooks, and adds local git excludes so none of it is committed:

   `brigade init --target . --depth repo --harnesses <harness> --owner <harness> --git-exclude`

4. After the user agrees, run it. Then run `brigade work brief --target .` and summarize the brief.
5. Tell the user the enforced hooks take effect in the next session.
```

- [x] Create `plugins/brigade/README.md`:

```markdown
# Brigade plugin

Brigade memory for Claude Code and Codex. See [docs/agent-plugin.md](../../docs/agent-plugin.md) for install steps, the tool list, and the loopback HTTP mode for MCP gateways.

The MCP server runs `npx -y @escoffier-labs/brigade@<version> memory serve-mcp --stdio`. If you already installed the CLI with pipx, you can change `command` to `brigade` and drop the first two arguments.

`skills/brigade-work/SKILL.md` is generated. Edit `src/brigade/templates/skills/brigade-work/SKILL.md` and run `scripts/sync_plugin_skills.py --write`.
```

- [x] In `scripts/version_sync.py`, add below `RECORDINGS = [...]`:

```python
# The agent plugin manifests and the npm pin their MCP configs launch.
PLUGIN_LOCATIONS = [
    ("plugins/brigade/.claude-plugin/plugin.json", r'"version"\s*:\s*"([^"]+)"'),
    ("plugins/brigade/.codex-plugin/plugin.json", r'"version"\s*:\s*"([^"]+)"'),
    ("plugins/brigade/.mcp.json", r'"@escoffier-labs/brigade@([^"]+)"'),
    ("plugins/brigade/codex.mcp.json", r'"@escoffier-labs/brigade@([^"]+)"'),
]
```

  and in `_locations()`, add `locs.extend(PLUGIN_LOCATIONS)` after `locs.extend(RECORDINGS)`.
- [x] In `tests/test_version_sync_script.py`, add `module.PLUGIN_LOCATIONS = []` on the line after `module.RECORDINGS = []`.
- [x] Run to green: `PY -m pytest tests/test_agent_plugin.py tests/test_version_sync_script.py tests/test_release_version_sync.py -q -p no:cacheprovider`. Expect all PASS. Then `PY scripts/version_sync.py --check`. Expect `version=0.29.0 checked=17 locations` and exit 0.
- [x] Validate the Claude plugin: `claude plugin validate plugins/brigade`. Expect a success line and no errors. Fix any reported manifest error, then rerun.
- [x] Commit: `git add plugins/brigade scripts/version_sync.py tests/test_version_sync_script.py tests/test_agent_plugin.py && git commit -m "feat(plugin): package Brigade memory as a Claude Code and Codex plugin"`

---

### Task 10: Documentation and changelog

**Files:**
- Create: `docs/agent-plugin.md`
- Modify: `docs/phase-agent-plugin.md` (apply the spec deltas)
- Modify: `CHANGELOG.md` (Unreleased section)

- [x] Create `docs/agent-plugin.md` with these sections and this exact content outline:
  1. `# Brigade agent plugin`: one paragraph on what the plugin does (memory tools, `brigade-work` skill, Claude SessionStart brief, opt-in `enforce` skill).
  2. `## Install in Claude Code`: `/plugin marketplace add escoffier-labs/brigade`, then `/plugin install brigade@brigade`. Note it requires Brigade 0.30.0 or later on npm.
  3. `## Install in Codex`: `codex plugin marketplace add escoffier-labs/brigade`, then `codex plugin add brigade`.
  4. `## Tools`: a table of the six tools from `memory_mcp_core.tool_specs()` with one line each, and the reference grammar (`repo:<path>`, `user:<path>`, `card://<path>`, bare path).
  5. `## Where memory lives`: the repo store rules (explicit `--target`, or the nearest `.brigade/config.json` above the cwd, never home) and the user store (`$BRIGADE_HOME/memory/cards`, handoffs in `$BRIGADE_HOME/memory/handoffs`).
  6. `## Loopback HTTP for MCP gateways`: `brigade memory serve-mcp --print-token-path`, then `brigade memory serve-mcp --http 127.0.0.1:8790`. Register `http://127.0.0.1:8790/mcp` with header `Authorization: Bearer <contents of the token file>`. Executor v1 can use the stdio command instead; Executor v2 needs the HTTP mode. Binding off loopback is refused.
  7. `## Turn on the full work loop`: what the `enforce` skill runs.
- [x] In `docs/phase-agent-plugin.md`: in the tool contract table, delete the `propose_memory` row and add to the `write_handoff` row: "With `action` `create-card` or `update-card`, it is the reviewed route for proposing card changes." In the packaging tree, delete `commands/enforce.md` and `commands/memory-status.md`, add `skills/enforce/SKILL.md` and `codex.mcp.json`, and note under SessionStart hook: "Claude Code only; Codex has no verified plugin hook contract in codex-cli 0.163.0." Under Transports, add: "`--target` defaults to auto-detection; an explicit `--target` is always the repo store." Under Dependencies, replace the npm bullet with: "npm `@escoffier-labs/brigade@0.29.0` is published. The plugin pins the pyproject version through `scripts/version_sync.py`, and the marketplace entries land with the first release that contains these commands."
- [x] Add under the CHANGELOG `## Unreleased` heading (create the heading at the top if missing):

```markdown
### Added
- Brigade agent plugin for Claude Code and Codex (`plugins/brigade/`): memory MCP tools, the `brigade-work` skill, a non-blocking SessionStart brief, and an opt-in `enforce` skill.
- `brigade memory serve-mcp --http 127.0.0.1:<port>`: loopback Streamable HTTP with a private bearer token file, for MCP gateways that do not accept stdio servers.
- Memory MCP tools `memory_status`, `work_brief`, and `write_handoff`; card tools now span the repo store and a user store under `BRIGADE_HOME`.

### Changed
- The memory MCP server reports `serverInfo.name` `brigade-memory` (was `brigade-memory-readonly`), and `serve-mcp --target` defaults to the nearest Brigade-wired repo.
```

- [x] Commit: `git add docs/agent-plugin.md docs/phase-agent-plugin.md CHANGELOG.md && git commit -m "docs: document the Brigade agent plugin and HTTP memory server"`

---

### Task 11: Full verification and manual canaries

**Files:** none changed unless a canary fails.

- [x] Fast suite: `PY -m pytest tests/test_mcp_server_dispatch.py tests/test_memory_scope.py tests/test_handoff_build_draft.py tests/test_memory_mcp_core.py tests/test_mcp_http.py tests/test_memory_search_mcp.py tests/test_agent_plugin.py tests/test_handoff_cmd.py tests/test_skills_cmd.py tests/test_module_size_ratchet.py tests/test_version_sync_script.py -q -p no:cacheprovider`. Expect all PASS.
- [x] Full gate: launch `./scripts/verify` as a systemd user unit (`systemd-run --user --unit brigade-plugin-verify --working-directory "$PWD" ./scripts/verify`), then wait for it with `journalctl --user -u brigade-plugin-verify -f` until the unit exits. Expect exit 0. A Bash-tool foreground run hits the 10-minute cap and a background `&` launch breaks claim tests, so use the unit.
- [x] Install this branch's CLI for the canaries (npm does not have these commands until the release): from a detached worktree of this branch, `pipx install --force .`. Expect `brigade memory serve-mcp --help` to list `--http`.
- [x] Claude Code canary: copy `plugins/brigade` to a scratch directory, edit the copy's `.mcp.json` so the server is `"command": "brigade"` with args `["memory", "serve-mcp", "--stdio", "--inbox", "claude"]`, then in a scratch git repo run `claude --plugin-dir <scratch copy>` and ask it to call `memory_status` and then `write_handoff`. Expect `memory_status` to show the scratch repo under `repo` (this proves Claude Code starts the server in the project directory) and a new file in the scratch repo's `.claude/memory-handoffs/`. Delete the scratch copy afterwards.
- [x] Codex canary: `codex plugin marketplace add` is also release-gated, so run `codex mcp add brigade-canary -- brigade memory serve-mcp --stdio --inbox codex` in a scratch repo, ask Codex to call `search_cards` and `write_handoff`, then `codex mcp remove brigade-canary`.
- [x] Executor v2 canary: start `brigade memory serve-mcp --http 127.0.0.1:8790` in a scratch repo, register `http://127.0.0.1:8790/mcp` in Executor v2 with the bearer header from `--print-token-path`, list tools (expect six), and call `search_cards`. Record the result in the PR body. If Executor v2 is not installed on the host, record "Executor v2 canary not run: not installed" in the PR body.
- [ ] Open the PR against `main` after the `pass` skill gate, with the canary results in the body. No UI is involved, so no screenshots.

---

### Task 13: Adopt main's loopback HTTP transport (#1816)

Added after `origin/main` landed #1816 (loopback Streamable HTTP for `memory serve-mcp` and `skills serve-mcp`) and released 0.30.0 while this branch was in flight. Merge commit `a260d1954` took main's `src/brigade/mcp_server.py` and `tests/test_mcp_http.py` whole and deleted this branch's `src/brigade/mcp_http.py` and `tests/test_mcp_server_dispatch.py`. `src/brigade/memory_cmd.py` and `src/brigade/cli/memory.py` kept this branch's side. Main's versions of those two files are saved at `/tmp/brigade-plugin-fire/pre-merge/memory_cmd.main.py` and `cli_memory.main.py`; this branch's old transport and tests are in the same directory for reference.

Main's transport API (`src/brigade/mcp_server.py`): `handle_message(message, *, server_name, list_resources, read_resource, list_tools, call_tool)`, `serve_stdio(**same)`, and `run_http_from_env(*, server_name, host, port, default_port, environ=None, **callbacks)`, which reads the token from `BRIGADE_MCP_HTTP_TOKEN` (32+ characters) and exits 2 on bad config or bind errors. There is no `Handlers` dataclass and no `serve_stdio_handlers`.

**Files:** `src/brigade/memory_mcp_core.py`, `src/brigade/memory_cmd.py`, `src/brigade/cli/memory.py`, `src/brigade/memory_scope.py`, `tests/test_memory_mcp_core.py`, `tests/test_memory_scope.py`, `tests/test_memory_search_mcp.py`, `tests/test_mcp_http.py`, `mcp-registry/memory/server.json`, `docs/agent-plugin.md`, `docs/phase-agent-plugin.md`, `CHANGELOG.md`, `plugins/brigade/README.md` (only if it mentions HTTP).

- [x] Core: replace `MemoryTools.handlers()` with `MemoryTools.callbacks() -> dict[str, Any]` returning `server_name`, `list_resources`, `read_resource`, `list_tools`, `call_tool`, suitable for `**` into `mcp_server.serve_stdio` and `run_http_from_env`. Port main's richer tool and input-field descriptions for `list_cards`, `get_card`, `search_cards` (see the saved `memory_cmd.main.py`), adapted to the two stores and origin-qualified refs.
- [x] `memory_cmd.serve_mcp(*, target=None, stdio=False, http=False, host=None, port=None, inbox="codex", json_output=False)`: keep `MCP_HTTP_DEFAULT_PORT = 8931`; stdio goes through `mcp_server.serve_stdio(**tools.callbacks())`; http goes through `mcp_server.run_http_from_env(**tools.callbacks(), host=host, port=port, default_port=MCP_HTTP_DEFAULT_PORT)`; keep main's error messages for `--stdio` with `--http` and for `--host`/`--port` without `--http`; the contract output names the token env var and default port the way main's did, plus the stores and six tools. `memory_cmd.py` must stay under 2000 lines.
- [x] CLI: main's `--http` (store_true), `--host`, `--port` flags and help text, plus this branch's `--target` (default None, auto-detect) and `--inbox`. Remove `--bearer-file` and `--print-token-path`.
- [x] `memory_scope`: remove `HTTP_TOKEN_NAME` and `http_token_path()` and their test.
- [x] Tests: update `tests/test_memory_search_mcp.py` CLI tests to the new flags (drop the token-path test). In main's `tests/test_mcp_http.py`, update memory-server expectations (server name `brigade-memory`, six tools, any read-only assertion) without weakening the security tests; leave skills tests alone. Port the stdio/HTTP parity test from the saved pre-merge `test_mcp_http.py` onto main's `make_http_server`/`handle_message`, as a memory-core test. Drop tests that only exercised the deleted bearer-file transport.
- [x] `mcp-registry/memory/server.json`: the description no longer says read-only. Use: "Brigade memory cards and handoffs: list, search, and read cards across a repo store and a user store, show memory status and the work brief, and write Memory Handoffs."
- [x] Docs: `docs/agent-plugin.md` and `docs/phase-agent-plugin.md` HTTP sections describe main's transport: `BRIGADE_MCP_HTTP_TOKEN` (32+ characters, never a flag), `brigade memory serve-mcp --http [--host 127.0.0.1] [--port 8931]`, `POST /mcp` and `GET /healthz`. Remove all token-file and `--print-token-path`/`--bearer-file` text. Keep the Codex approval note.
- [x] `CHANGELOG.md` `[Unreleased]`: delete this branch's `--http ... bearer token file` bullet (main's #1816 bullet covers HTTP), keep the plugin and core-tools bullets, and move this branch's `Changed` bullets (server name, `--target` default, handoff draft mode 0600) from the `[0.30.0]` section into `[Unreleased]` `Changed`. Main's #1816 bullet says the memory server is read-only; amend it to say the memory server now also has `write_handoff`.
- [x] Run to green: `PY -m pytest tests/test_memory_mcp_core.py tests/test_memory_scope.py tests/test_memory_search_mcp.py tests/test_mcp_http.py tests/test_handoff_build_draft.py tests/test_handoff_cmd.py tests/test_skills_cmd.py tests/test_agent_plugin.py tests/test_version_sync_script.py tests/test_release_version_sync.py tests/test_module_size_ratchet.py tests/test_cli_inventory_contract.py tests/test_conftest_home_isolation.py -q -p no:cacheprovider`, `scripts/version_sync.py --check`, then ruff check, ruff format --check, and mypy on the changed modules.
- [x] Commit: `refactor(memory): serve the memory MCP core over main's HTTP transport`

---

### Task 14: Fixes from the Opus final review

Added after an independent read-only Opus 5.5 review of `git diff origin/main...HEAD` at `a7b3bcad4` (no blockers, 7 should-fix, several nits). Each behavior fix gets a failing test first.

**Files:** `src/brigade/memory_scope.py`, `src/brigade/memory_mcp_core.py`, `src/brigade/memory_cmd.py`, `plugins/brigade/hooks/session-start.sh`, `plugins/brigade/skills/enforce/SKILL.md`, `scripts/sync_plugin_skills.py`, `README.md`, `docs/technical-guide.md`, `docs/agent-plugin.md`, `docs/phase-agent-plugin.md`, `docs/phase-agent-plugin-plan.md` (Task 12 text only), `CHANGELOG.md`, and the matching tests.

- [x] R1 repo detection: with no `--target`, the walk up from the cwd also treats a directory containing `.brigade/memory-care.toml` as the repo store (still never the home directory, still stopping at the nearest marker). `repo_wired` keeps meaning `.brigade/config.json` resolved through `resolve_wired_target`. Add a test against real files (no mock) for: memory-care-only repo detected, nested repo stops at the nearest marker, home directory skipped. Update the spec's resolution paragraph.
- [x] R2 handoff destination: `write_handoff` picks the repo inbox whenever `resolution.repo_root` is set, even when the repo store was dropped for an invalid memory-care config. Test: wired repo plus invalid config writes to the repo inbox with `origin: repo`.
- [x] R3 explicit target: `serve_mcp` exits 2 with `error: --target is not a directory: <path>` when an explicit `--target` is not an existing directory (all modes).
- [x] R4 `work_brief`: build the four keys (`git`, `active_session`, `latest_session`, `next`) from the cheap helpers the full brief uses (for example `_git_snapshot`, `_active_session_info`, `_collect_sessions`, `_resolve_next_task`; confirm the real names and signatures in `src/brigade/work_cmd/`), so the tool never contacts the fleet hub or publishes presence. If no cheap path exists for a key, return null for it rather than calling `_brief_payload`. Separately, `MemoryTools.call()` catches any other `Exception` and returns `"<tool> failed: <ExcType>: <msg>"` as a tool error, so one bad call cannot end the stdio server.
- [x] R5 enforce skill and guide: the full enforced hooks (verify capture, raw-check blocking, Stop gate) are installed for Claude Code only; say what Codex gets from `brigade init` (skills and AGENTS guidance, no hooks). Remove the claim that nothing is committed; say `--git-exclude` keeps Brigade's own files out of git and that `.claude/settings.json` may be tracked in some repos.
- [x] R6 guide accuracy: `docs/agent-plugin.md` must not claim the `brigade-work` skill tells agents to call the `work_brief` tool. Say Codex users can call `work_brief` for the same brief. Do not edit the canonical skill.
- [x] R7 docs: `README.md` and `docs/technical-guide.md` no longer call the memory MCP server read-only or list three tools; the skills server stays read-only. In `docs/agent-plugin.md`, state that the marketplace install works from the release that adds the marketplace entries (0.31.0) and that until then `plugins/brigade` can be loaded locally only with the `command` swapped to a `brigade` CLI built from this branch. Document that user-store handoffs are plain files under `$BRIGADE_HOME/memory/handoffs` and can be checked with `brigade handoff lint <file>` (confirm the exact command exists first).
- [x] Nits:
  - `session-start.sh`: use `${HOME:-}` so an unset HOME still exits 0.
  - `memory_status.handoff_inbox`: report where `write_handoff` would actually write (the repo inbox alias, or the user-store path).
  - `UNWIRED_HINT`: only say "uses the user store" when no repo store is active.
  - Errors from `build_draft` surfaced through the tool name tool arguments (`action`, `title`, `summary`, `target_card`, `target_document`), not CLI flags.
  - `_search_cards`: search each store with that store's own config (`store.config`), so search and `get_card` agree.
  - `sync_plugin_skills.py`: fix the docstring to match the real flags.
  - Stale text: spec line 3 ("will go in"), the spec's `proposals/` layout, the spec's npm 0.29.0 line, and Task 12's 0.30.0 references now say 0.31.0.
  - Tests: `test_memory_status_reports_stores_and_config_error` adds a repo card before asserting the degraded count; `test_build_draft_creates_private_file_without_following_symlinks` drops the unasserted `force=True` tail; the CLI test drops assertions about `--print-token-path`/`--bearer-file`.
- [x] Run to green: the Task 13 pytest list plus `tests/test_work_cmd_session.py` if `work_brief` helpers come from there; `scripts/version_sync.py --check`; `scripts/sync_plugin_skills.py`; ruff check, ruff format --check, mypy on changed modules; `bash -n plugins/brigade/hooks/session-start.sh`.
- [x] Commit: `fix(memory): address final review findings for the agent plugin`

---

### Task 12: Publish the marketplace entries (with the 0.31.0 release PR)

Run this task inside the release PR that bumps `pyproject.toml` to the first version containing Tasks 1-9. Do not run it before.

**Files:**
- Create: `.claude-plugin/marketplace.json`
- Create: `.agents/plugins/marketplace.json`
- Modify: `RELEASE.md` (one checklist line)

- [ ] Run `PY scripts/version_sync.py --write` so the plugin manifests and npm pins match the release version.
- [ ] Create `.claude-plugin/marketplace.json`:

```json
{
  "name": "brigade",
  "description": "Brigade agent memory and work loop",
  "owner": {
    "name": "Solomon Neas"
  },
  "plugins": [
    {
      "name": "brigade",
      "description": "Agent memory that works in every repo, with an opt-in verified work loop",
      "source": "./plugins/brigade",
      "category": "development",
      "author": {
        "name": "Solomon Neas"
      },
      "homepage": "https://brigade.tools"
    }
  ]
}
```

- [ ] Create `.agents/plugins/marketplace.json`:

```json
{
  "name": "brigade",
  "interface": {
    "displayName": "Brigade"
  },
  "plugins": [
    {
      "name": "brigade",
      "source": {
        "source": "local",
        "path": "./plugins/brigade"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Developer Tools"
    }
  ]
}
```

- [ ] Validate: `claude plugin validate .claude-plugin/marketplace.json`. Expect success.
- [ ] Add to `RELEASE.md`'s release checklist: "After npm publish, install the plugin from a clean HOME in Claude Code (`/plugin marketplace add escoffier-labs/brigade`) and Codex (`codex plugin marketplace add escoffier-labs/brigade`) and call `memory_status`."
- [ ] Commit inside the release PR: `git add .claude-plugin .agents RELEASE.md plugins/brigade && git commit -m "feat(plugin): publish the Brigade plugin marketplace entries"`
