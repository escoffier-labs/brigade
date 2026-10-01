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

import copy
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
    # Native Codex auth keeps static headers, whole-header refs and token-only
    # refs separate. Generic headers remain the portable harness representation.
    http_headers: dict[str, str] = field(default_factory=dict)
    env_http_headers: dict[str, str] = field(default_factory=dict)
    bearer_token_env_var: str | None = None
    opencode_native: dict[str, Any] = field(default_factory=dict)

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
            opencode_native=opencode_native(raw.get("opencode_native", {})),
            **codex_native_auth(raw),
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
    if server.opencode_native:
        out["opencode_native"] = opencode_native(server.opencode_native)
    out.update(codex_native_auth(vars(server)))
    return out


_CODEX_AUTH_FIELDS = ("http_headers", "env_http_headers", "bearer_token_env_var")
_CODEX_MODELED_FIELDS = {"command", "url", "type", "args", "timeout", "env", "headers", *_CODEX_AUTH_FIELDS}
_CODEX_STDIO_FIELDS = {"cwd", "env_vars"}
_CODEX_REMOTE_FIELDS = {"oauth_resource"}


def _codex_transport_excluded(is_remote: bool) -> set[str]:
    """Modeled fields plus the options the other transport rejects."""
    return _CODEX_MODELED_FIELDS | (_CODEX_STDIO_FIELDS if is_remote else _CODEX_REMOTE_FIELDS)


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def codex_native_auth(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy native auth without consulting the environment or including values in errors."""
    out: dict[str, Any] = {}
    for key in _CODEX_AUTH_FIELDS:
        value = raw.get(key)
        if not value:
            continue
        if key == "bearer_token_env_var":
            if not isinstance(value, str) or not _ENV_NAME_RE.fullmatch(value):
                raise ValueError("Codex bearer_token_env_var requires an environment variable name")
            out[key] = value
        else:
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError(f"Codex {key} requires a string map")
            if key == "env_http_headers" and any(not _ENV_NAME_RE.fullmatch(v) for v in value.values()):
                raise ValueError(
                    "Codex env_http_headers requires environment variable names; interpolation is unsupported"
                )
            out[key] = dict(value)
    return out


def codex_auth_header_names(auth: dict[str, Any]) -> set[str]:
    """Logical header names for native values or value-free ownership keys."""
    names = {name.lower() for key in ("http_headers", "env_http_headers") for name in auth.get(key, {})}
    if auth.get("bearer_token_env_var"):
        names.add("authorization")
    return names


# --------------------------------------------------------------------------- #
# Validation (reuses the risk patterns from tools_cmd, single source of truth)
# --------------------------------------------------------------------------- #


def _is_high_risk(command: object) -> bool:
    return isinstance(command, str) and any(p.search(command) for p in HIGH_RISK_COMMAND_PATTERNS)


def _sensitive_http_header(name: str) -> bool:
    return name.lower() in {"authorization", "proxy-authorization", "cookie", "set-cookie"} or bool(
        UNSAFE_FIELD_PATTERN.search(name)
    )


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
            if "literal" in value and (
                _sensitive_http_header(key) if scope == "headers" else UNSAFE_FIELD_PATTERN.search(key)
            ):
                issues.append(("warn", f'{server.name}: {scope} {key} is an inlined secret; prefer {{"ref": ...}}'))
    for key in server.http_headers:
        if _sensitive_http_header(key):
            issues.append(("warn", f"{server.name}: http_headers {key} is an inlined secret; prefer env_http_headers"))
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
    reject_invalid_existing: bool = False,
) -> str:
    doc: dict[str, Any] = {}
    if text is not None:
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                doc = loaded
            elif reject_invalid_existing:
                raise ValueError("existing JSON configuration must be an object; refusing to overwrite")
        except json.JSONDecodeError as exc:
            if reject_invalid_existing:
                raise ValueError("existing JSON configuration is invalid; refusing to overwrite") from exc
            doc = {}
    # Navigate/create the (possibly nested) server map, preserving every sibling key.
    parts = top_key.split(".")
    node = doc
    for part in parts[:-1]:
        child = node.get(part)
        if reject_invalid_existing and part in node and not isinstance(child, dict):
            raise ValueError(f"existing {top_key} section must be an object; refusing to overwrite")
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    leaf = parts[-1]
    section = node.get(leaf)
    if reject_invalid_existing and leaf in node and not isinstance(section, dict):
        raise ValueError(f"existing {top_key} section must be an object; refusing to overwrite")
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
        if not isinstance(server, dict):
            continue
        for field_name in ("env", "headers"):
            values = server.get(field_name)
            if isinstance(values, dict):
                for value in values.values():
                    if isinstance(value, str):
                        referenced.update(re.findall(r"\$\{input:([A-Za-z_][A-Za-z0-9_]*)\}", value))
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


def _toml_escape(escape: str, digits: str) -> str:
    if escape == "U":
        return chr(int(digits, 16))
    return json.loads(f'"\\{escape}"')


def _split_toml_path(path: str) -> list[str]:
    """Split a TOML table path on UNQUOTED dots, stripping quotes per segment.

    ``mcp_servers."my.server"`` -> ``["mcp_servers", "my.server"]`` (the dotted name
    stays one segment). Plain ``mcp_servers.github`` -> ``["mcp_servers", "github"]``.
    """
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    chars = iter(path)
    for char in chars:
        if quote == '"' and char == "\\":
            escape = next(chars, "")
            width = {"u": 4, "U": 8}.get(escape, 0)
            digits = "".join(next(chars, "") for _ in range(width))
            try:
                current.append(json.loads(f'"\\{escape}{digits}"') if width == 4 else _toml_escape(escape, digits))
            except ValueError:
                current.append(char + escape + digits)
        elif quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char == ".":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return parts


def _codex_server_name(path: str | None) -> str | None:
    """Return server name if a table path is mcp_servers.<name>[.sub], else None."""
    if not path:
        return None
    parts = _split_toml_path(path.strip())
    if len(parts) >= 2 and parts[0] == "mcp_servers":
        return parts[1]
    return None


def _codex_render_table(name: str, server_dict: dict[str, Any]) -> str:
    """Render one ``[mcp_servers.<name>]`` table from a provider per-server dict."""
    from .tools_cmd import _format_inline_list, _format_inline_table, _format_toml_key

    key = _format_toml_key(name)
    out = [f"[mcp_servers.{key}]\n"]
    for field_name in ("command", "url", "type"):
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
    headers = server_dict.get("headers")
    if isinstance(headers, dict) and headers:
        out.append(f"headers = {_format_inline_table({str(k): str(v) for k, v in headers.items()})}\n")
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


def _codex_write_file(
    text: str | None, owned: dict[str, dict[str, Any]], remove: set[str], *, native: bool = False
) -> str:
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
    render = _codex_render_native_table if native else _codex_render_table
    live = _codex_read_file(text) if native else {}
    rendered = [
        render(
            name,
            {
                **{
                    k: v
                    for k, v in live.get(name, {}).items()
                    if k not in _codex_transport_excluded(bool(owned[name].get("url")))
                },
                **owned[name],
            },
        )
        for name in sorted(owned)
    ]
    if insert_index is None:
        insert_index = len(kept)
    merged_blocks = kept[:insert_index] + rendered + kept[insert_index:]
    parts = [preamble.rstrip("\n")] if preamble.strip() else []
    parts.extend(b.rstrip("\n") for b in merged_blocks if b.strip())
    result = "\n\n".join(parts)
    return (result + "\n") if result else ""


def _native_toml_key(key: str) -> str:
    """Bare key when legal, else a basic string that keeps non-BMP text and escapes DEL."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return json.dumps(key, ensure_ascii=False).replace("\x7f", "\\u007f")


def _native_toml_value(value: Any) -> str:
    """Render preserved native options, including nested OAuth/tool tables."""
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_native_toml_key(k)} = {_native_toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_native_toml_value(v) for v in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    # TOML dates are possible in unrelated options; keep their native type.
    from datetime import date, datetime, time

    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    raise ValueError("unsupported native TOML option type; refusing to overwrite")


def _codex_render_native_table(name: str, server_dict: dict[str, Any]) -> str:
    fields = dict(server_dict)
    if fields.get("url"):
        fields.pop("headers", None)  # obsolete generic remote field
    lines = [f"[mcp_servers.{_native_toml_key(name)}]\n"]
    lines.extend(f"{_native_toml_key(k)} = {_native_toml_value(v)}\n" for k, v in fields.items())
    return "".join(lines)


# --------------------------------------------------------------------------- #
# Per-provider transforms
# --------------------------------------------------------------------------- #


def _codex_to_provider(server: CanonicalServer, *, native_auth: dict[str, Any] | None = None) -> dict[str, Any]:
    if not server.is_remote:
        return _mcpservers_to_provider(server, "passthrough")
    # Canonical native auth replaces adopted auth for the same logical header
    # across all forms. Explicit canonical static/env fallback pairs stay intact.
    native = codex_native_auth(native_auth or {})
    canonical = codex_native_auth(vars(server))
    explicit = codex_auth_header_names(canonical)
    for key in ("http_headers", "env_http_headers"):
        native[key] = {name: value for name, value in native.get(key, {}).items() if name.lower() not in explicit}
    if "authorization" in explicit:
        native.pop("bearer_token_env_var", None)
    for key, value in canonical.items():
        if isinstance(value, dict):
            native[key] = {**native.get(key, {}), **value}
        else:
            native[key] = value
    static: dict[str, str] = {}
    env: dict[str, str] = {}
    bearer: str | None = None
    explicit_headers = codex_auth_header_names(native)
    for key, value in server.headers.items():
        if key.lower() in explicit_headers:
            continue
        if "ref" in value:
            if not _ENV_NAME_RE.fullmatch(value["ref"]):
                raise ValueError("Codex header ref requires an environment variable name; interpolation is unsupported")
            env[key] = value["ref"]
            continue
        literal = value.get("literal", "")
        full_ref = _REF_RE.fullmatch(literal)
        token_ref = (
            _REF_RE.fullmatch(literal[7:]) if key.lower() == "authorization" and literal.startswith("Bearer ") else None
        )
        if full_ref and not literal.startswith("${input:"):
            env[key] = full_ref.group(1)
        elif token_ref and not literal[7:].startswith("${input:"):
            bearer = token_ref.group(1)
        elif "${" in literal:
            raise ValueError(
                "Codex headers contain unsupported interpolation; use native header or bearer environment references"
            )
        else:
            static[key] = literal
    out: dict[str, Any] = {"url": server.url, "type": server.transport}
    for auth_field, generic in (("http_headers", static), ("env_http_headers", env)):
        merged = {**generic, **native.get(auth_field, {})}
        if merged:
            out[auth_field] = merged
    token = native.get("bearer_token_env_var", bearer)
    if token:
        out["bearer_token_env_var"] = token
    return out


def _codex_from_provider(
    name: str, raw: dict[str, Any], *, keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    server, demoted = _mcpservers_from_provider(name, raw, keep_secrets=keep_secrets)
    if not server.is_remote:
        return server, demoted
    # Native static strings are literal, including strings resembling templates.
    # Legacy generic headers are migrated without demoting token templates first.
    headers, _ = _parse_env(raw.get("headers"), keep_secrets=True)
    legacy = CanonicalServer(
        name=name, transport=server.transport, url=server.url, headers=headers, **codex_native_auth(raw)
    )
    native = codex_native_auth(_codex_to_provider(legacy))
    demoted = []
    if not keep_secrets:
        static = native.get("http_headers", {})
        env = native.get("env_http_headers", {})
        for key in list(static):
            if _sensitive_http_header(key):
                del static[key]
                placeholder = re.sub(r"[^A-Za-z0-9_]", "_", key).upper()
                if not placeholder or placeholder[0].isdigit():
                    placeholder = "MCP_" + placeholder
                env.setdefault(key, placeholder)
                demoted.append(key)
        if env:
            native["env_http_headers"] = env
    return CanonicalServer(name=name, transport=server.transport, url=server.url, **codex_native_auth(native)), demoted


def codex_merge_server(
    server: CanonicalServer, existing: dict[str, Any], native_auth: dict[str, Any]
) -> dict[str, Any]:
    """Preserve unmodeled native options while replacing Brigade's modeled fields."""
    excluded = _codex_transport_excluded(server.is_remote)
    preserved = {k: v for k, v in existing.items() if k not in excluded}
    return {**preserved, **_codex_to_provider(server, native_auth=native_auth)}


def _remote_transport(raw: dict[str, Any], *, type_key: str = "type") -> str:
    """Pick http/sse for a remote server; never keep stdio when only a URL is present."""
    for key in (type_key, "transport", "type"):
        value = raw.get(key)
        if value in ("http", "sse"):
            return str(value)
    return "http"


def _looks_like_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _emit_headers(server: CanonicalServer, env_style: str) -> dict[str, str]:
    """Project native auth without env access; refuse native semantics JSON cannot preserve.

    A static fallback plus an env override cannot be represented by one generic
    header. Native static template-looking values must also stay literal.
    """
    native_names = [key.lower() for mapping in (server.http_headers, server.env_http_headers) for key in mapping]
    if server.bearer_token_env_var:
        native_names.append("authorization")
    if len(native_names) != len(set(native_names)) or any("${" in v for v in server.http_headers.values()):
        raise ValueError("cannot preserve Codex native header semantics in this harness; scope the server to Codex")
    generic = {k: v for k, v in server.headers.items() if k.lower() not in native_names}
    headers = _emit_env(generic, env_style)
    headers.update(server.http_headers)
    headers.update(_emit_env({k: {"ref": v} for k, v in server.env_http_headers.items()}, env_style))
    if server.bearer_token_env_var:
        token_ref = _emit_env({"Authorization": {"ref": server.bearer_token_env_var}}, env_style)["Authorization"]
        headers["Authorization"] = "Bearer " + token_ref
    return headers


def _mcpservers_to_provider(server: CanonicalServer, env_style: str, *, remote_url_key: str = "url") -> dict[str, Any]:
    """The common JSON ``mcpServers`` per-server shape (Claude, Cursor, Antigravity).

    Empty ``args`` are omitted so write/read round-trips stay fingerprint-stable
    for formats (Codex/Grok TOML) that drop empty arrays on render.
    """
    if server.is_remote:
        out: dict[str, Any] = {remote_url_key: server.url}
        if remote_url_key == "url":
            out["type"] = server.transport
        headers = _emit_headers(server, env_style)
        if headers:
            out["headers"] = headers
        return out
    out: dict[str, Any] = {"command": server.command}
    if server.args:
        out["args"] = list(server.args)
    if server.env:
        out["env"] = _emit_env(server.env, env_style)
    if server.timeout is not None:
        out["timeout"] = server.timeout
    return out


def _mcpservers_from_provider(
    name: str, raw: dict[str, Any], *, remote_url_key: str = "url", keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    url = raw.get(remote_url_key) or raw.get("url") or raw.get("serverUrl")
    command = raw.get("command")
    # Url-only (or command that is actually a URL) is always remote.
    if not url and _looks_like_url(command) and not raw.get("args"):
        url = command
        command = None
    if url and not command:
        headers, demoted = _parse_env(raw.get("headers"), keep_secrets=keep_secrets)
        transport = _remote_transport(raw)
        return CanonicalServer(name=name, transport=transport, url=str(url), headers=headers), demoted
    if url:
        headers, demoted = _parse_env(raw.get("headers"), keep_secrets=keep_secrets)
        transport = _remote_transport(raw)
        return CanonicalServer(name=name, transport=transport, url=str(url), headers=headers), demoted
    env, demoted = _parse_env(raw.get("env"), keep_secrets=keep_secrets)
    timeout = raw.get("timeout")
    return (
        CanonicalServer(
            name=name,
            transport="stdio",
            command=str(command) if command else None,
            args=tuple(str(a) for a in (raw.get("args") or [])),
            env=env,
            timeout=int(timeout) if isinstance(timeout, (int, float)) else None,
        ),
        demoted,
    )


def _vscode_to_provider(server: CanonicalServer) -> dict[str, Any]:
    if server.is_remote:
        out: dict[str, Any] = {"type": server.transport, "url": server.url}
        headers = _emit_headers(server, "vscode-inputs")
        if headers:
            out["headers"] = headers
        return out
    out: dict[str, Any] = {"type": "stdio", "command": server.command}
    if server.args:
        out["args"] = list(server.args)
    if server.env:
        out["env"] = _emit_env(server.env, "vscode-inputs")
    return out


@dataclass(frozen=True)
class OpenCodeConfig:
    servers: dict[str, dict[str, Any]]
    locations: dict[str, str]
    new_server_location: str
    duplicates: tuple[str, ...]
    warnings: tuple[str, ...]


class OpenCodeLayoutError(ValueError):
    """Value-free layout diagnostics shared by sync and profiles."""

    def __init__(self, message: str, duplicates: tuple[str, ...] = ()):
        super().__init__(message)
        self.fields = {
            "reason": "duplicate_layout" if duplicates else "malformed_layout",
            "layout_conflicts": [{"server": n, "locations": ["flat", "nested"]} for n in duplicates],
        }


def _opencode_document(text: str | None) -> dict[str, Any]:
    try:
        doc = {} if text is None else json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenCodeLayoutError("existing OpenCode config is malformed") from exc
    if not isinstance(doc, dict) or ("mcp" in doc and not isinstance(doc["mcp"], dict)):
        raise OpenCodeLayoutError("existing OpenCode config is malformed")
    return doc


def _opencode_direct(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") in ("local", "remote")


def inspect_opencode_config(text: str | None, *, for_mutation: bool = False) -> OpenCodeConfig:
    """Inspect pinned v2 normalization, retaining legacy storage locations."""
    section = _opencode_document(text).get("mcp", {})
    nested = "servers" in section and not _opencode_direct(section["servers"])
    if nested and not isinstance(section["servers"], dict):
        raise OpenCodeLayoutError("existing OpenCode mcp.servers is malformed")
    if nested and any(not isinstance(entry, dict) for entry in section["servers"].values()):
        raise OpenCodeLayoutError("existing OpenCode mcp.servers entry is malformed")
    flat = {
        name: copy.deepcopy(value)
        for name, value in section.items()
        if isinstance(value, dict)
        and not (name in ("servers", "timeout") and not _opencode_direct(value))
        and (_opencode_direct(value) or isinstance(value.get("command"), list) or isinstance(value.get("url"), str))
    }
    native = (
        {name: copy.deepcopy(value) for name, value in section.get("servers", {}).items() if isinstance(value, dict)}
        if nested
        else {}
    )
    duplicates = tuple(sorted(set(flat) & set(native)))
    if for_mutation and duplicates:
        raise OpenCodeLayoutError("duplicate OpenCode server locations; refusing mutation", duplicates)
    return OpenCodeConfig(
        {**flat, **native},
        {**dict.fromkeys(flat, "flat"), **dict.fromkeys(native, "nested")},
        "nested" if nested else "flat",
        duplicates,
        tuple(
            f"{name}: duplicate flat/nested locations; nested entry takes whole-entry precedence" for name in duplicates
        ),
    )


_OPENCODE_MODELED = {"type", "url", "command", "environment", "headers", "enabled", "disabled", "timeout", "oauth"}


def opencode_native(raw: Any, *, keep_secrets: bool = True) -> dict[str, Any]:
    """Copy only supported canonical native fields, never arbitrary options."""
    if not isinstance(raw, dict):
        raise ValueError("OpenCode native metadata requires an object")
    out: dict[str, Any] = {}
    for key in ("enabled", "disabled", "timeout", "oauth"):
        if key not in raw:
            continue
        value = raw[key]
        if key in ("enabled", "disabled"):
            if not isinstance(value, bool):
                raise ValueError(f"OpenCode {key} requires a boolean")
        elif key == "timeout":
            values = value.values() if isinstance(value, dict) else [value]
            if isinstance(value, dict) and set(value) - {"startup", "catalog", "execution"}:
                raise ValueError("OpenCode timeout has unsupported fields")
            if any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in values):
                raise ValueError("OpenCode timeout requires positive integer milliseconds")
        elif value is not False:
            if not isinstance(value, dict):
                raise ValueError("OpenCode oauth requires false or an object")
            fields = {"clientId", "scope"} | ({"clientSecret"} if keep_secrets else set())
            value = {k: v for k, v in value.items() if k in fields}
            if any(not isinstance(v, str) for v in value.values()):
                raise ValueError("OpenCode oauth fields require strings")
        out[key] = copy.deepcopy(value)
    return out


def _opencode_timeout(value: Any, location: str) -> Any:
    if location == "nested" and isinstance(value, int):
        return {"catalog": value, "execution": value}
    if location == "flat" and isinstance(value, dict):
        if set(value) != {"catalog", "execution"} or value["catalog"] != value["execution"]:
            raise ValueError("OpenCode timeout cannot be preserved in flat layout")
        return value["catalog"]
    return copy.deepcopy(value)


def _opencode_transport_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Drop known incompatible fields after every native preservation merge."""
    if entry.get("type") == "remote":
        incompatible = {"command", "environment", "cwd"}
    elif entry.get("type") == "local":
        incompatible = {"url", "headers", "oauth"}
    else:
        incompatible = set()
    return {key: value for key, value in entry.items() if key not in incompatible}


def opencode_merge_server(
    server: CanonicalServer, existing: dict[str, Any], *, location: str, native_keys: dict[str, Any]
) -> dict[str, Any]:
    out = {k: copy.deepcopy(v) for k, v in existing.items() if k not in _OPENCODE_MODELED}
    native: dict[str, Any] = {}
    for key, names in native_keys.items():
        if key not in existing:
            continue
        value = existing[key]
        if isinstance(names, list) and isinstance(value, dict):
            retained = {k: copy.deepcopy(v) for k, v in value.items() if k in names}
            if retained:
                native[key] = retained
        elif names is True:
            native[key] = copy.deepcopy(value)
    explicit_native = opencode_native(server.opencode_native)
    if isinstance(explicit_native.get("oauth"), dict) and isinstance(native.get("oauth"), dict):
        explicit_native["oauth"] = {**native["oauth"], **explicit_native["oauth"]}
    native.update(explicit_native)
    if server.is_remote:
        out.update(type="remote", url=server.url)
    else:
        out.update(type="local", command=[server.command, *server.args] if server.command else list(server.args))
        if server.env:
            out["environment"] = _emit_env(server.env, "expand")
    activation, other = ("disabled", "enabled") if location == "nested" else ("enabled", "disabled")
    out[activation] = native.pop(activation, not native.pop(other) if other in native else activation == "enabled")
    if "timeout" in native and (location == "nested" or server.timeout is None):
        native["timeout"] = _opencode_timeout(native["timeout"], location)
    if server.timeout is not None:
        milliseconds = server.timeout * 1000
        if location == "nested":
            timeout = native.get("timeout", {})
            native["timeout"] = {**timeout, "catalog": milliseconds, "execution": milliseconds}
        else:
            native["timeout"] = milliseconds
    headers = native.pop("headers", {}) if server.is_remote else {}
    canonical_headers = _emit_headers(server, "expand") if server.is_remote else {}
    explicit = {k.lower() for k in canonical_headers}
    headers = {k: v for k, v in headers.items() if k.lower() not in explicit}
    headers.update(canonical_headers)
    if headers:
        out["headers"] = headers
    out.update(native)
    return _opencode_transport_entry(out)


def _opencode_write_file(text: str | None, owned: dict[str, dict[str, Any]], remove: set[str]) -> str:
    view = inspect_opencode_config(text, for_mutation=True)
    doc = _opencode_document(text)
    section = doc.setdefault("mcp", {})
    for name in remove:
        location = view.locations.get(name)
        if location:
            (section["servers"] if location == "nested" else section).pop(name, None)
    for name, entry in owned.items():
        location = view.locations.get(name, view.new_server_location)
        if location == "flat" and name in section and name not in view.servers:
            raise ValueError("OpenCode server would overwrite an existing setting/container")
        destination = section["servers"] if location == "nested" else section
        preserved = {k: v for k, v in view.servers.get(name, {}).items() if k not in _OPENCODE_MODELED}
        destination[name] = _opencode_transport_entry({**preserved, **copy.deepcopy(entry)})
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _opencode_to_provider(server: CanonicalServer) -> dict[str, Any]:
    return opencode_merge_server(server, {}, location="flat", native_keys={})


def _opencode_from_provider(
    name: str, raw: dict[str, Any], *, keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    native = opencode_native(raw, keep_secrets=keep_secrets)
    dropped = [key for key in raw if key not in _OPENCODE_MODELED and UNSAFE_FIELD_PATTERN.search(key)]
    oauth = raw.get("oauth")
    if isinstance(oauth, dict):
        dropped.extend(f"oauth.{key}" for key in oauth if key not in native.get("oauth", {}))
    if raw.get("type") == "remote" or raw.get("url"):
        headers, demoted = _parse_env(raw.get("headers"), keep_secrets=True)
        for key, value in list(headers.items()):
            if not keep_secrets and _sensitive_http_header(key) and "literal" in value:
                headers.pop(key)
                dropped.append(key)
        return CanonicalServer(
            name=name, transport="http", url=str(raw.get("url")), headers=headers, opencode_native=native
        ), demoted + dropped
    env, demoted = _parse_env(raw.get("environment"), keep_secrets=keep_secrets)
    command_list = raw.get("command") or []
    command = str(command_list[0]) if command_list else None
    args = tuple(str(a) for a in command_list[1:])
    return CanonicalServer(
        name=name, transport="stdio", command=command, args=args, env=env, opencode_native=native
    ), demoted + dropped


def _vscode_from_provider(
    name: str, raw: dict[str, Any], *, keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    if raw.get("url"):
        headers, demoted = _parse_env(raw.get("headers"), keep_secrets=keep_secrets)
        return (
            CanonicalServer(name=name, transport=str(raw.get("type") or "http"), url=str(raw["url"]), headers=headers),
            demoted,
        )
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


def _yaml_scalar(value: object) -> str:
    """Render a simple YAML scalar without a full YAML library (Brigade is zero-dep)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    # Quote when needed so bare :, #, spaces do not break YAML.
    if (
        any(ch in text for ch in (":", "#", "{", "}", "[", "]", ",", "&", "*", "!", "|", ">", "'", '"', "%", "@", "`"))
        or text.strip() != text
        or text
        in (
            "true",
            "false",
            "null",
            "yes",
            "no",
            "on",
            "off",
        )
    ):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if text[0].isdigit() or text.startswith(("-", ".")):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _yaml_parse_scalar(raw: str) -> object:
    text = raw.strip()
    if not text or text == "null" or text == "~":
        return None
    if text in ("true", "True", "yes", "on"):
        return True
    if text in ("false", "False", "no", "off"):
        return False
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        inner = text[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _hermes_to_provider(server: CanonicalServer) -> dict[str, Any]:
    """Hermes mcp_servers entry: YAML under ~/.hermes/config.yaml."""
    if server.is_remote:
        remote: dict[str, Any] = {"url": server.url}
        if server.transport and server.transport != "http":
            remote["transport"] = server.transport
        headers = _emit_headers(server, "passthrough")
        if headers:
            remote["headers"] = headers
        if server.timeout is not None:
            remote["timeout"] = server.timeout
        return remote
    out: dict[str, Any] = {"command": server.command}
    if server.args:
        out["args"] = list(server.args)
    if server.env:
        out["env"] = _emit_env(server.env, "passthrough")
    if server.timeout is not None:
        out["timeout"] = server.timeout
    return out


def _hermes_from_provider(
    name: str, raw: dict[str, Any], *, keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    url = raw.get("url")
    command = raw.get("command")
    if not url and _looks_like_url(command) and not raw.get("args"):
        url = command
        command = None
    if url and not command:
        headers, demoted = _parse_env(raw.get("headers"), keep_secrets=keep_secrets)
        transport = _remote_transport(raw, type_key="transport")
        timeout = raw.get("timeout")
        return (
            CanonicalServer(
                name=name,
                transport=transport,
                url=str(url),
                headers=headers,
                timeout=int(timeout) if isinstance(timeout, (int, float)) else None,
            ),
            demoted,
        )
    env, demoted = _parse_env(raw.get("env"), keep_secrets=keep_secrets)
    timeout = raw.get("timeout")
    return (
        CanonicalServer(
            name=name,
            transport="stdio",
            command=str(command) if command else None,
            args=tuple(str(a) for a in (raw.get("args") or [])),
            env=env,
            timeout=int(timeout) if isinstance(timeout, (int, float)) else None,
        ),
        demoted,
    )


def _hermes_render_servers(servers: dict[str, dict[str, Any]]) -> str:
    """Render a top-level mcp_servers mapping as indented YAML."""
    lines = ["mcp_servers:"]
    if not servers:
        lines.append("  {}")
        return "\n".join(lines) + "\n"
    for name in sorted(servers):
        body = servers[name]
        lines.append(f"  {_yaml_scalar(name)}:")
        if not body:
            lines.append("    {}")
            continue
        for key in ("command", "url", "transport", "timeout", "connect_timeout"):
            if key not in body or body[key] is None:
                continue
            lines.append(f"    {key}: {_yaml_scalar(body[key])}")
        args = body.get("args")
        if isinstance(args, list) and args:
            lines.append("    args:")
            for item in args:
                lines.append(f"      - {_yaml_scalar(item)}")
        for map_key in ("env", "headers"):
            mapping = body.get(map_key)
            if not isinstance(mapping, dict) or not mapping:
                continue
            lines.append(f"    {map_key}:")
            for mk, mv in mapping.items():
                lines.append(f"      {_yaml_scalar(mk)}: {_yaml_scalar(mv)}")
    return "\n".join(lines) + "\n"


def _hermes_parse_mcp_servers(text: str) -> dict[str, dict[str, Any]]:
    """Parse only the top-level mcp_servers mapping from a Hermes config.yaml.

    Supports the flat stdio/remote shape Hermes documents (command/args/env/url/headers/timeout).
    Nested maps deeper than env/headers are not required for Brigade's projection.
    """
    lines = text.splitlines()
    start = None
    base_indent = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.rstrip(":") == "mcp_servers" or stripped.startswith("mcp_servers:"):
            start = index
            base_indent = len(line) - len(line.lstrip(" "))
            break
    if start is None:
        return {}
    # Inline empty map: mcp_servers: {}
    first = lines[start].strip()
    if first.endswith(": {}") or first.endswith(":{}") or first == "mcp_servers: {}":
        return {}
    servers: dict[str, dict[str, Any]] = {}
    current: str | None = None
    current_map: str | None = None  # env | headers | args
    for line in lines[start + 1 :]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= base_indent and line.lstrip() and not line.lstrip().startswith("#"):
            break  # next top-level key
        stripped = line.strip()
        if indent == base_indent + 2 and stripped.endswith(":") and not stripped.startswith("-"):
            name = stripped[:-1].strip().strip("\"'")
            current = name
            servers[current] = {}
            current_map = None
            continue
        if current is None:
            continue
        if indent == base_indent + 4 and stripped.endswith(":") and stripped[:-1] in ("env", "headers", "args"):
            current_map = stripped[:-1]
            if current_map in ("env", "headers"):
                servers[current][current_map] = {}
            elif current_map == "args":
                servers[current]["args"] = []
            continue
        if current_map == "args" and stripped.startswith("- "):
            servers[current].setdefault("args", []).append(_yaml_parse_scalar(stripped[2:]))
            continue
        if current_map in ("env", "headers") and ":" in stripped and indent >= base_indent + 6:
            key, _, val = stripped.partition(":")
            servers[current].setdefault(current_map, {})[key.strip().strip("\"'")] = _yaml_parse_scalar(val)
            continue
        if indent == base_indent + 4 and ":" in stripped and not stripped.startswith("-"):
            current_map = None
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                servers[current][key] = _yaml_parse_scalar(val)
            continue
    return {k: v for k, v in servers.items() if isinstance(v, dict)}


def _hermes_read_file(text: str | None) -> dict[str, dict[str, Any]]:
    if not text:
        return {}
    return _hermes_parse_mcp_servers(text)


def _hermes_write_file(text: str | None, owned: dict[str, dict[str, Any]], remove: set[str]) -> str:
    """Replace or append the top-level mcp_servers block; preserve the rest of config.yaml."""
    existing_text = text or ""
    live = _hermes_parse_mcp_servers(existing_text)
    for name in remove:
        live.pop(name, None)
    live.update(owned)
    block = _hermes_render_servers(live)
    lines = existing_text.splitlines(keepends=True)
    if not lines:
        return block
    start = None
    base_indent = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.rstrip(":") == "mcp_servers" or stripped.startswith("mcp_servers:"):
            start = index
            base_indent = len(line) - len(line.lstrip(" "))
            break
    if start is None:
        body = existing_text.rstrip("\n")
        return (body + "\n\n" + block) if body else block
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= base_indent:
            end = index
            break
    new_lines = lines[:start] + [block if block.endswith("\n") else block + "\n"]
    if end < len(lines) and not lines[end].startswith("\n") and lines[end].strip():
        # ensure blank line before next top-level key when missing
        if new_lines and not new_lines[-1].endswith("\n\n"):
            pass
    new_lines.extend(lines[end:])
    result = "".join(new_lines)
    if not result.endswith("\n"):
        result += "\n"
    return result


def _openclaw_to_provider(server: CanonicalServer) -> dict[str, Any]:
    """OpenClaw mcp.servers shape: stdio {command,args,env} (no type); remote {url,transport}."""
    if server.is_remote:
        remote: dict[str, Any] = {"url": server.url, "transport": server.transport}
        headers = _emit_headers(server, "expand")
        if headers:
            remote["headers"] = headers
        return remote
    out: dict[str, Any] = {"command": server.command}
    if server.args:
        out["args"] = list(server.args)
    if server.env:
        out["env"] = _emit_env(server.env, "expand")
    return out


def _openclaw_from_provider(
    name: str, raw: dict[str, Any], *, keep_secrets: bool = False
) -> tuple[CanonicalServer, list[str]]:
    url = raw.get("url")
    command = raw.get("command")
    # A bare URL in the command field is a remote endpoint, not a stdio command.
    if not url and _looks_like_url(command) and not raw.get("args"):
        url = command
        command = None
    if url:
        headers, demoted = _parse_env(raw.get("headers"), keep_secrets=keep_secrets)
        # Never keep transport=stdio for a URL-bearing entry: OpenClaw stamps a
        # default transport that would otherwise emit an invalid stdio+url server.
        transport = _remote_transport(raw, type_key="transport")
        return (
            CanonicalServer(name=name, transport=transport, url=str(url), headers=headers),
            demoted,
        )
    env, demoted = _parse_env(raw.get("env"), keep_secrets=keep_secrets)
    return (
        CanonicalServer(
            name=name,
            transport="stdio",
            command=str(command) if command else None,
            args=tuple(str(a) for a in (raw.get("args") or [])),
            env=env,
        ),
        demoted,
    )


# --------------------------------------------------------------------------- #
# Adapter registry
# --------------------------------------------------------------------------- #


def _make_json_mcpservers(
    harness: str,
    path: str,
    *,
    user_scope: bool = False,
    remote_url_key: str = "url",
    reject_invalid_existing: bool = False,
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
        write_file=lambda t, o, r: _json_write_file(
            t,
            o,
            r,
            "mcpServers",
            reject_invalid_existing=reject_invalid_existing,
        ),
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
        to_provider=_codex_to_provider,
        from_provider=_codex_from_provider,
        read_file=_codex_read_file,
        write_file=lambda t, o, r: _codex_write_file(t, o, r, native=True),
    ),
    "grok": McpAdapter(
        harness="grok",
        path=".grok/config.toml",
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
        read_file=lambda t: inspect_opencode_config(t).servers,
        write_file=_opencode_write_file,
    ),
    # User-global scopes: these write the per-user config the tool reads everywhere,
    # not a per-repo file. Gated behind --user-scope. Used to sync a machine's daily tools.
    "claude-user": _make_json_mcpservers("claude-user", "~/.claude.json", user_scope=True),
    "cursor-user": _make_json_mcpservers(
        "cursor-user",
        "~/.cursor/mcp.json",
        user_scope=True,
        reject_invalid_existing=True,
    ),
    # Kimi Code: mcpServers JSON. The static path is the legacy root; the user
    # profile layer overrides it when the capability probe selects ~/.kimi.
    "kimi-user": _make_json_mcpservers(
        "kimi-user",
        "~/.kimi-code/mcp.json",
        user_scope=True,
        reject_invalid_existing=True,
    ),
    "opencode-user": McpAdapter(
        harness="opencode-user",
        path="~/.config/opencode/opencode.json",
        fmt="json",
        top_key="mcp",
        user_scope=True,
        supports_remote=True,
        env_style="expand",
        to_provider=_opencode_to_provider,
        from_provider=_opencode_from_provider,
        read_file=lambda t: inspect_opencode_config(t).servers,
        write_file=_opencode_write_file,
    ),
    "codex-user": McpAdapter(
        harness="codex-user",
        path="~/.codex/config.toml",
        fmt="toml",
        top_key="mcp_servers",
        user_scope=True,
        supports_remote=True,
        env_style="passthrough",
        to_provider=_codex_to_provider,
        from_provider=_codex_from_provider,
        read_file=_codex_read_file,
        write_file=lambda t, o, r: _codex_write_file(t, o, r, native=True),
    ),
    "grok-user": McpAdapter(
        harness="grok-user",
        path="~/.grok/config.toml",
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
    # Hermes: ~/.hermes/config.yaml under mcp_servers (YAML). User-scoped only;
    # profiles may set HERMES_HOME, but the default home is the machine catalog.
    "hermes": McpAdapter(
        harness="hermes",
        path="~/.hermes/config.yaml",
        fmt="yaml",
        top_key="mcp_servers",
        user_scope=True,
        supports_remote=True,
        env_style="passthrough",
        to_provider=_hermes_to_provider,
        from_provider=_hermes_from_provider,
        read_file=_hermes_read_file,
        write_file=_hermes_write_file,
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
