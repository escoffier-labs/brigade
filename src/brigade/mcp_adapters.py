"""Canonical MCP server schema + per-provider adapters.

Brigade keeps one canonical MCP server catalog (`.brigade/mcp.json`) and projects
it into each agent tool's native MCP config file. The shapes are NOT uniform: most
tools use a JSON ``mcpServers`` object, but Codex uses TOML ``[mcp_servers.*]`` tables,
VS Code uses a JSON ``servers`` key with a separate ``inputs`` array, OpenCode uses an
``mcp`` key with a command-array shape, and Antigravity uses ``serverUrl`` (not ``url``)
for remote servers and lives in a user-global file.

Each adapter owns four things: how a canonical server serializes into the provider's
per-server dict (``to_provider``), how to read one back (``from_provider``, used by
``brigade mcp import``), and how to read/merge the provider's whole config file
preserving every key Brigade does not own (``read_file`` / ``write_file``). The engine
in ``mcp_cmd`` stays format-agnostic and works only in terms of per-server dicts.

Secrets are never inlined: a canonical ``env``/``headers`` value is either ``{"ref": "VAR"}``
(emitted as a ``${VAR}`` reference the tool expands at launch, or a VS Code ``${input:VAR}``)
or ``{"literal": "..."}`` (the user's explicit choice, which doctor flags).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import toml_compat as tomllib
from .tools_cmd import HIGH_RISK_COMMAND_PATTERNS, UNSAFE_FIELD_PATTERN

TRANSPORTS = ("stdio", "http", "sse")
_REF_RE = re.compile(r"^\$\{(?:input:)?([A-Za-z_][A-Za-z0-9_]*)\}$")


# --------------------------------------------------------------------------- #
# Canonical model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CanonicalServer:
    """One MCP server in Brigade's canonical catalog."""

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, dict[str, str]] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, dict[str, str]] = field(default_factory=dict)
    timeout: int | None = None
    enabled: bool = True
    targets: tuple[str, ...] | None = None
    description: str = ""

    @property
    def is_remote(self) -> bool:
        return self.transport in ("http", "sse")


def _normalize_env(raw: object) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Normalize an env/headers map to {KEY: {"ref"|"literal": value}}.

    A bare string is treated as a literal (paste convenience) and warned. Returns
    (normalized, warnings).
    """
    out: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return out, warnings
    for key, value in raw.items():
        key = str(key)
        if isinstance(value, dict) and "ref" in value:
            out[key] = {"ref": str(value["ref"])}
        elif isinstance(value, dict) and "literal" in value:
            out[key] = {"literal": str(value["literal"])}
        elif isinstance(value, str):
            out[key] = {"literal": value}
            warnings.append(f'env {key}: bare string treated as a literal; use {{"ref": "VAR"}} for a reference')
        else:
            warnings.append(f"env {key}: unsupported value, skipped")
    return out, warnings


def server_from_dict(name: str, raw: dict[str, Any]) -> tuple[CanonicalServer, list[str]]:
    """Build a CanonicalServer from a canonical-file entry; returns (server, warnings)."""
    warnings: list[str] = []
    transport = str(raw.get("transport") or "stdio")
    if transport not in TRANSPORTS:
        warnings.append(f"{name}: unknown transport {transport!r}, defaulting to stdio")
        transport = "stdio"
    env, env_warn = _normalize_env(raw.get("env"))
    headers, header_warn = _normalize_env(raw.get("headers"))
    warnings.extend(f"{name}: {w}" for w in (*env_warn, *header_warn))
    args_raw = raw.get("args") or []
    args = tuple(str(a) for a in args_raw) if isinstance(args_raw, list) else ()
    targets_raw = raw.get("targets")
    targets = tuple(str(t) for t in targets_raw) if isinstance(targets_raw, list) else None
    timeout = raw.get("timeout")
    return (
        CanonicalServer(
            name=name,
            transport=transport,
            command=str(raw["command"]) if raw.get("command") else None,
            args=args,
            env=env,
            url=str(raw["url"]) if raw.get("url") else None,
            headers=headers,
            timeout=int(timeout) if isinstance(timeout, (int, float)) else None,
            enabled=bool(raw.get("enabled", True)),
            targets=targets,
            description=str(raw.get("description") or ""),
        ),
        warnings,
    )


def server_to_dict(server: CanonicalServer) -> dict[str, Any]:
    """Serialize a CanonicalServer back to a canonical-file entry (for add/import writes)."""
    out: dict[str, Any] = {"transport": server.transport, "enabled": server.enabled}
    if server.command:
        out["command"] = server.command
    if server.args:
        out["args"] = list(server.args)
    if server.env:
        out["env"] = {k: dict(v) for k, v in server.env.items()}
    if server.url:
        out["url"] = server.url
    if server.headers:
        out["headers"] = {k: dict(v) for k, v in server.headers.items()}
    if server.timeout is not None:
        out["timeout"] = server.timeout
    if server.targets is not None:
        out["targets"] = list(server.targets)
    if server.description:
        out["description"] = server.description
    return out


# --------------------------------------------------------------------------- #
# Validation (reuses the risk patterns from tools_cmd, single source of truth)
# --------------------------------------------------------------------------- #


def _is_high_risk(command: object) -> bool:
    return isinstance(command, str) and any(p.search(command) for p in HIGH_RISK_COMMAND_PATTERNS)


def validate_server(server: CanonicalServer) -> list[tuple[str, str]]:
    """Return (severity, message) issues for a server. severity in {error, warn}."""
    issues: list[tuple[str, str]] = []
    if server.is_remote:
        if not server.url:
            issues.append(("error", f"{server.name}: remote transport requires a url"))
    else:
        if not server.command:
            issues.append(("error", f"{server.name}: stdio transport requires a command"))
        elif _is_high_risk(server.command):
            issues.append(("error", f"{server.name}: command shape is high risk"))
    if server.timeout is None:
        issues.append(("warn", f"{server.name}: no timeout set"))
    for scope, mapping in (("env", server.env), ("headers", server.headers)):
        for key, value in mapping.items():
            if "literal" in value and UNSAFE_FIELD_PATTERN.search(key):
                issues.append(("warn", f'{server.name}: {scope} {key} is an inlined secret; prefer {{"ref": ...}}'))
    return issues


# --------------------------------------------------------------------------- #
# env reference emission / parsing
# --------------------------------------------------------------------------- #


def _emit_env(mapping: dict[str, dict[str, str]], env_style: str) -> dict[str, str]:
    """Render canonical env/headers into the literal map a provider config carries."""
    out: dict[str, str] = {}
    for key, value in mapping.items():
        if "ref" in value:
            var = value["ref"]
            out[key] = f"${{input:{var}}}" if env_style == "vscode-inputs" else f"${{{var}}}"
        else:
            out[key] = value.get("literal", "")
    return out


def _parse_env(raw: object, *, keep_secrets: bool = False) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Reverse of _emit_env for import. By default demotes literal-looking secrets to refs.

    With keep_secrets=True, secret-looking literals are kept verbatim instead (used when
    syncing existing working configs whose tools do not expand ``${VAR}``, where dropping
    the value would break the server). Returns (canonical_env, demoted_keys).
    """
    out: dict[str, dict[str, str]] = {}
    demoted: list[str] = []
    if not isinstance(raw, dict):
        return out, demoted
    for key, value in raw.items():
        key = str(key)
        if not isinstance(value, str):
            continue
        match = _REF_RE.match(value)
        if match:
            out[key] = {"ref": match.group(1)}
        elif UNSAFE_FIELD_PATTERN.search(key) and not keep_secrets:
            out[key] = {"ref": key}
            demoted.append(key)
        else:
            out[key] = {"literal": value}
    return out, demoted


# --------------------------------------------------------------------------- #
# Adapter contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class McpAdapter:
    harness: str
    path: str  # repo-relative, or ~-prefixed for user_scope adapters
    fmt: str  # "json" | "toml"
    top_key: str  # provider's server-map key: mcpServers | servers | mcp | mcp_servers
    user_scope: bool
    supports_remote: bool
    env_style: str  # "passthrough" | "expand" | "vscode-inputs"
    to_provider: Callable[[CanonicalServer], dict[str, Any]]
    from_provider: Callable[[str, dict[str, Any]], tuple[CanonicalServer, list[str]]]
    read_file: Callable[[str | None], dict[str, dict[str, Any]]]
    write_file: Callable[[str | None, dict[str, dict[str, Any]], set[str]], str]


# --------------------------------------------------------------------------- #
# JSON file read/merge (generic over top_key)
# --------------------------------------------------------------------------- #


def _dig(doc: dict[str, Any], top_key: str) -> dict[str, Any] | None:
    """Navigate a dotted top_key (e.g. ``mcp.servers``); return the server map or None."""
    node: Any = doc
    for part in top_key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node if isinstance(node, dict) else None


def _json_read_file(text: str | None, top_key: str) -> dict[str, dict[str, Any]]:
    if not text:
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(doc, dict):
        return {}
    section = _dig(doc, top_key)
    if not isinstance(section, dict):
        return {}
    return {str(k): v for k, v in section.items() if isinstance(v, dict)}


def _json_write_file(
    text: str | None,
    owned: dict[str, dict[str, Any]],
    remove: set[str],
    top_key: str,
    *,
    collect_inputs: bool = False,
) -> str:
    doc: dict[str, Any] = {}
    if text:
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                doc = loaded
        except json.JSONDecodeError:
            doc = {}
    # Navigate/create the (possibly nested) server map, preserving every sibling key.
    parts = top_key.split(".")
    node = doc
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    leaf = parts[-1]
    section = node.get(leaf)
    if not isinstance(section, dict):
        section = {}
    for name in remove:
        section.pop(name, None)
    for name, server_dict in owned.items():
        section[name] = server_dict
    node[leaf] = section
    if collect_inputs:
        _merge_vscode_inputs(doc, section)
    # sort_keys=False preserves the order of co-owned files (e.g. ~/.claude.json,
    # ~/.openclaw/openclaw.json) so a sync produces a minimal, readable diff.
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def _merge_vscode_inputs(doc: dict[str, Any], servers: dict[str, Any]) -> None:
    """Ensure VS Code top-level ``inputs`` has a promptString entry per ${input:VAR}."""
    referenced: set[str] = set()
    for server in servers.values():
        env = server.get("env") if isinstance(server, dict) else None
        if isinstance(env, dict):
            for value in env.values():
                match = _REF_RE.match(value) if isinstance(value, str) and value.startswith("${input:") else None
                if match:
                    referenced.add(match.group(1))
    existing = doc.get("inputs")
    inputs = [i for i in existing if isinstance(i, dict)] if isinstance(existing, list) else []
    have = {i.get("id") for i in inputs}
    for var in sorted(referenced):
        if var not in have:
            inputs.append({"id": var, "type": "promptString", "description": f"{var} for MCP", "password": True})
    if inputs:
        doc["inputs"] = inputs


# --------------------------------------------------------------------------- #
# Codex TOML surgical merge
# --------------------------------------------------------------------------- #

_TABLE_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_ARRAY_TABLE_RE = re.compile(r"^\s*\[\[")


def _toml_blocks(text: str) -> tuple[str, list[tuple[str | None, str]]]:
    """Split TOML into (preamble, [(table_path|None, block_text), ...]).

    A block is a standard ``[table]`` header and the lines up to the next header.
    Array-of-tables (``[[...]]``) and non-standard lines map to path=None and are
    preserved verbatim. The preamble holds top-level keys/comments before any table.
    """
    lines = text.splitlines(keepends=True)
    preamble: list[str] = []
    blocks: list[tuple[str | None, list[str]]] = []
    current: list[str] | None = None
    current_path: str | None = None
    for line in lines:
        m = _TABLE_RE.match(line)
        is_array = bool(_ARRAY_TABLE_RE.match(line))
        if m and not is_array:
            if current is not None:
                blocks.append((current_path, current))
            current = [line]
            current_path = m.group(1).strip()
        elif is_array:
            if current is not None:
                blocks.append((current_path, current))
            current = [line]
            current_path = None
        elif current is None:
            preamble.append(line)
        else:
            current.append(line)
    if current is not None:
        blocks.append((current_path, current))
    return "".join(preamble), [(p, "".join(b)) for p, b in blocks]


def _codex_server_name(path: str | None) -> str | None:
    """Return server name if a table path is mcp_servers.<name>[.sub], else None."""
    if not path:
        return None
    parts = [p.strip() for p in path.split(".")]
    if len(parts) >= 2 and parts[0] == "mcp_servers":
        return parts[1].strip('"').strip("'")
    return None


def _codex_render_table(name: str, server_dict: dict[str, Any]) -> str:
    """Render one ``[mcp_servers.<name>]`` table from a provider per-server dict."""
    from .tools_cmd import _format_inline_list, _format_inline_table, _format_toml_key

    key = _format_toml_key(name)
    out = [f"[mcp_servers.{key}]\n"]
    for field_name in ("command", "url"):
        value = server_dict.get(field_name)
        if isinstance(value, str) and value:
            out.append(f"{field_name} = {tomllib.format_toml_value(value)}\n")
    args = server_dict.get("args")
    if isinstance(args, list) and args:
        out.append(f"args = {_format_inline_list([str(a) for a in args])}\n")
    timeout = server_dict.get("timeout")
    if isinstance(timeout, (int, float)):
        out.append(f"timeout = {tomllib.format_toml_value(timeout)}\n")
    env = server_dict.get("env")
    if isinstance(env, dict) and env:
        out.append(f"env = {_format_inline_table({str(k): str(v) for k, v in env.items()})}\n")
    return "".join(out)


def _codex_read_file(text: str | None) -> dict[str, dict[str, Any]]:
    if not text:
        return {}
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    servers = doc.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def _codex_write_file(text: str | None, owned: dict[str, dict[str, Any]], remove: set[str]) -> str:
    preamble, blocks = _toml_blocks(text or "")
    managed = set(owned) | set(remove)
    kept: list[str] = []
    insert_index: int | None = None
    for path, block in blocks:
        name = _codex_server_name(path)
        if name in managed:
            if insert_index is None:
                insert_index = len(kept)
            continue
        kept.append(block)
    rendered = [_codex_render_table(name, owned[name]) for name in sorted(owned)]
    if insert_index is None:
        insert_index = len(kept)
    merged_blocks = kept[:insert_index] + rendered + kept[insert_index:]
    parts = [preamble.rstrip("\n")] if preamble.strip() else []
    parts.extend(b.rstrip("\n") for b in merged_blocks if b.strip())
    result = "\n\n".join(parts)
    return (result + "\n") if result else ""


# --------------------------------------------------------------------------- #
# Per-provider transforms
# --------------------------------------------------------------------------- #


def _mcpservers_to_provider(server: CanonicalServer, env_style: str, *, remote_url_key: str = "url") -> dict[str, Any]:
    """The common JSON ``mcpServers`` per-server shape (Claude, Cursor, Antigravity)."""
    if server.is_remote:
        out: dict[str, Any] = {remote_url_key: server.url}
        if remote_url_key == "url":
            out["type"] = server.transport
        if server.headers:
            out["headers"] = _emit_env(server.headers, env_style)
        return out
    out = {"command": server.command, "args": list(server.args)}
    if server.env:
        out["env"] = _emit_env(server.env, env_style)
    if server.timeout is not None:
        out["timeout"] = server.timeout
    return out


def _mcpservers_from_provider(
    name: str, raw: dict[str, Any], *, remote_url_key: str = "url", keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    url = raw.get(remote_url_key) or raw.get("url") or raw.get("serverUrl")
    if url:
        headers, demoted = _parse_env(raw.get("headers"), keep_secrets=keep_secrets)
        transport = raw.get("type") if raw.get("type") in ("http", "sse") else "http"
        return CanonicalServer(name=name, transport=transport, url=str(url), headers=headers), demoted
    env, demoted = _parse_env(raw.get("env"), keep_secrets=keep_secrets)
    timeout = raw.get("timeout")
    return (
        CanonicalServer(
            name=name,
            transport="stdio",
            command=str(raw["command"]) if raw.get("command") else None,
            args=tuple(str(a) for a in (raw.get("args") or [])),
            env=env,
            timeout=int(timeout) if isinstance(timeout, (int, float)) else None,
        ),
        demoted,
    )


def _vscode_to_provider(server: CanonicalServer) -> dict[str, Any]:
    if server.is_remote:
        return {"type": server.transport, "url": server.url}
    out: dict[str, Any] = {"type": "stdio", "command": server.command, "args": list(server.args)}
    if server.env:
        out["env"] = _emit_env(server.env, "vscode-inputs")
    return out


def _opencode_to_provider(server: CanonicalServer) -> dict[str, Any]:
    if server.is_remote:
        return {"type": "remote", "url": server.url, "enabled": server.enabled}
    command = [server.command, *server.args] if server.command else list(server.args)
    out: dict[str, Any] = {"type": "local", "command": command, "enabled": server.enabled}
    if server.env:
        out["environment"] = _emit_env(server.env, "expand")
    return out


def _opencode_from_provider(
    name: str, raw: dict[str, Any], *, keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    if raw.get("type") == "remote" or raw.get("url"):
        headers, demoted = _parse_env(raw.get("headers"), keep_secrets=keep_secrets)
        return CanonicalServer(name=name, transport="http", url=str(raw.get("url")), headers=headers), demoted
    env, demoted = _parse_env(raw.get("environment"), keep_secrets=keep_secrets)
    command_list = raw.get("command") or []
    command = str(command_list[0]) if command_list else None
    args = tuple(str(a) for a in command_list[1:])
    return CanonicalServer(name=name, transport="stdio", command=command, args=args, env=env), demoted


def _vscode_from_provider(
    name: str, raw: dict[str, Any], *, keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    if raw.get("url"):
        return CanonicalServer(name=name, transport=str(raw.get("type") or "http"), url=str(raw["url"])), []
    env, demoted = _parse_env(raw.get("env"), keep_secrets=keep_secrets)
    return (
        CanonicalServer(
            name=name,
            transport="stdio",
            command=str(raw["command"]) if raw.get("command") else None,
            args=tuple(str(a) for a in (raw.get("args") or [])),
            env=env,
        ),
        demoted,
    )


def _openclaw_to_provider(server: CanonicalServer) -> dict[str, Any]:
    """OpenClaw mcp.servers shape: stdio {command,args,env} (no type); remote {url,transport}."""
    if server.is_remote:
        return {"url": server.url, "transport": server.transport}
    out: dict[str, Any] = {"command": server.command, "args": list(server.args)}
    if server.env:
        out["env"] = _emit_env(server.env, "expand")
    return out


def _openclaw_from_provider(
    name: str, raw: dict[str, Any], *, keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    if raw.get("url"):
        return CanonicalServer(name=name, transport=str(raw.get("transport") or "http"), url=str(raw["url"])), []
    env, demoted = _parse_env(raw.get("env"), keep_secrets=keep_secrets)
    return (
        CanonicalServer(
            name=name,
            transport="stdio",
            command=str(raw["command"]) if raw.get("command") else None,
            args=tuple(str(a) for a in (raw.get("args") or [])),
            env=env,
        ),
        demoted,
    )


# --------------------------------------------------------------------------- #
# Adapter registry
# --------------------------------------------------------------------------- #


def _make_json_mcpservers(
    harness: str, path: str, *, user_scope: bool = False, remote_url_key: str = "url"
) -> McpAdapter:
    env_style = "expand"
    return McpAdapter(
        harness=harness,
        path=path,
        fmt="json",
        top_key="mcpServers",
        user_scope=user_scope,
        supports_remote=True,
        env_style=env_style,
        to_provider=lambda s: _mcpservers_to_provider(s, env_style, remote_url_key=remote_url_key),
        from_provider=lambda n, r, keep_secrets=False: _mcpservers_from_provider(
            n, r, remote_url_key=remote_url_key, keep_secrets=keep_secrets
        ),
        read_file=lambda t: _json_read_file(t, "mcpServers"),
        write_file=lambda t, o, r: _json_write_file(t, o, r, "mcpServers"),
    )


ADAPTERS: dict[str, McpAdapter] = {
    "claude": _make_json_mcpservers("claude", ".mcp.json"),
    "cursor": _make_json_mcpservers("cursor", ".cursor/mcp.json"),
    "codex": McpAdapter(
        harness="codex",
        path=".codex/config.toml",
        fmt="toml",
        top_key="mcp_servers",
        user_scope=False,
        supports_remote=True,
        env_style="passthrough",
        to_provider=lambda s: _mcpservers_to_provider(s, "passthrough"),
        from_provider=lambda n, r, keep_secrets=False: _mcpservers_from_provider(n, r, keep_secrets=keep_secrets),
        read_file=_codex_read_file,
        write_file=_codex_write_file,
    ),
    "vscode": McpAdapter(
        harness="vscode",
        path=".vscode/mcp.json",
        fmt="json",
        top_key="servers",
        user_scope=False,
        supports_remote=True,
        env_style="vscode-inputs",
        to_provider=_vscode_to_provider,
        from_provider=_vscode_from_provider,
        read_file=lambda t: _json_read_file(t, "servers"),
        write_file=lambda t, o, r: _json_write_file(t, o, r, "servers", collect_inputs=True),
    ),
    "antigravity": _make_json_mcpservers(
        "antigravity", "~/.gemini/config/mcp_config.json", user_scope=True, remote_url_key="serverUrl"
    ),
    "opencode": McpAdapter(
        harness="opencode",
        path="opencode.json",
        fmt="json",
        top_key="mcp",
        user_scope=False,
        supports_remote=True,
        env_style="expand",
        to_provider=_opencode_to_provider,
        from_provider=_opencode_from_provider,
        read_file=lambda t: _json_read_file(t, "mcp"),
        write_file=lambda t, o, r: _json_write_file(t, o, r, "mcp"),
    ),
    # User-global scopes: these write the per-user config the tool reads everywhere,
    # not a per-repo file. Gated behind --user-scope. Used to sync a machine's daily tools.
    "claude-user": _make_json_mcpservers("claude-user", "~/.claude.json", user_scope=True),
    "codex-user": McpAdapter(
        harness="codex-user",
        path="~/.codex/config.toml",
        fmt="toml",
        top_key="mcp_servers",
        user_scope=True,
        supports_remote=True,
        env_style="passthrough",
        to_provider=lambda s: _mcpservers_to_provider(s, "passthrough"),
        from_provider=lambda n, r, keep_secrets=False: _mcpservers_from_provider(n, r, keep_secrets=keep_secrets),
        read_file=_codex_read_file,
        write_file=_codex_write_file,
    ),
    "openclaw": McpAdapter(
        harness="openclaw",
        path="~/.openclaw/openclaw.json",
        fmt="json",
        top_key="mcp.servers",
        user_scope=True,
        supports_remote=True,
        env_style="expand",
        to_provider=_openclaw_to_provider,
        from_provider=_openclaw_from_provider,
        read_file=lambda t: _json_read_file(t, "mcp.servers"),
        write_file=lambda t, o, r: _json_write_file(t, o, r, "mcp.servers"),
    ),
}

MCP_TARGETS: tuple[str, ...] = tuple(ADAPTERS)


def adapter_for(harness: str) -> McpAdapter | None:
    return ADAPTERS.get(harness)


def resolve_path(adapter: McpAdapter, target: Path) -> Path:
    """Resolve an adapter's config path against a repo target (or $HOME for user-scope)."""
    if adapter.user_scope:
        return Path(adapter.path).expanduser()
    return target / adapter.path
