"""Node side of the Fleet Hub MCP servers page: a bounded summary of local MCP config.

The summary reuses ``brigade mcp`` for the catalog and its per-harness plan
statuses, and the adapters' own readers for servers present natively but absent
from the catalog. It carries server names, harness names, transports, status
codes, and the hostname of a remote server. It never carries env values, header
values, arguments, commands, full URLs, tokens, or absolute paths.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__

SCHEMA_VERSION = 1
MAX_ROWS = 300
MAX_TEXT = 120
ACCOUNT_TIMEOUT = 60
ACCOUNT_STATUSES = ("connected", "needs_auth", "failed", "unknown")
# `claude mcp list` prints `claude.ai <Name>: <url> - <marker> <status text>`. Only the name, the url's
# hostname, and the first status word are read; the rest of the line (errors, paths, ids) is dropped.
_ACCOUNT_LINE = re.compile(r"^(claude\.ai .+?):\s+(\S+)\s+-\s+(.*)$")
_STATUS_WORD = {"connected": "connected", "needs": "needs_auth", "failed": "failed"}
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")


def _text(value: Any) -> str:
    return "".join(ch for ch in str(value) if ch.isprintable())[:MAX_TEXT]


def remote_host(url: Any) -> str | None:
    """Hostname only: no scheme, userinfo, port, path, or query."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower() if parts.scheme in {"http", "https", "ws", "wss"} else ""
    except ValueError:
        return None
    return host if host and len(host) <= 253 and _HOST.fullmatch(host) else None


def _split_harness(name: str, user_scope: bool) -> tuple[str, str]:
    """``codex-user`` is the user scope of ``codex``; other adapters keep their name and the adapter's own scope."""
    if name.endswith("-user"):
        return name[: -len("-user")], "user"
    return name, "user" if user_scope else "project"


def _native_row(adapter: Any, name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Transport, enabled flag, and host of a server found natively. Nothing else is read out of *raw*."""
    transport, host = "unknown", None
    try:
        server, _demoted = adapter.from_provider(name, raw)
        transport = server.transport
        host = remote_host(server.url) if server.is_remote else None
    except Exception:  # an adapter that cannot read a foreign entry must not stop the report
        url = raw.get("url") or raw.get("serverUrl")
        if url:
            transport, host = "http", remote_host(url)
        elif raw.get("command"):
            transport = "stdio"
    enabled = raw.get("enabled") is not False and raw.get("disabled") is not True
    return {"transport": transport, "host": host, "enabled": enabled}


def build_servers(target: Path) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(catalog_present, server rows, harness rows)`` for every adapter this machine can read."""
    from . import mcp_adapters, mcp_cmd

    servers, errors, _warnings = mcp_cmd.load_canonical(target)
    catalog_present = not errors
    state = mcp_cmd._load_state(target)
    project, _notes = mcp_cmd.active_targets(target, harness=None, user_scope=False)
    # Sync targets decide where Brigade writes, not what exists. User configs (a
    # default sync never writes them) and project configs outside the targets are
    # still inventoried, without catalog-derived missing or disabled rows.
    inventory_only = {
        n
        for n, a in mcp_adapters.ADAPTERS.items()
        if n not in project and (a.user_scope or mcp_adapters.resolve_path(a, target).is_file())
    }
    names = list(project) + [
        n for n, a in mcp_adapters.ADAPTERS.items() if n not in project and (a.user_scope or n in inventory_only)
    ]
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    harnesses: dict[tuple[str, str], dict[str, Any]] = {}
    for adapter_name in names:
        adapter = mcp_adapters.ADAPTERS[adapter_name]
        harness, scope = _split_harness(adapter_name, adapter.user_scope)
        path = mcp_adapters.resolve_path(adapter, target)
        found = path.is_file()
        readable = True
        live: dict[str, dict[str, Any]] = {}
        try:
            text = path.read_text() if found else None
            # Validate like the sync plan does: some adapters read malformed text as
            # an empty mapping, and inventory-only configs never reach that plan.
            mcp_cmd._validate_native_config(adapter_name, text)
            live = adapter.read_file(text)
        except (OSError, ValueError, UnicodeError):
            readable = False
        slot = harnesses.setdefault(
            (harness, scope), {"harness": harness, "scope": scope, "config_found": False, "readable": True}
        )
        slot["config_found"] = slot["config_found"] or found
        slot["readable"] = slot["readable"] and readable
        if not readable:
            continue
        items = (
            []
            if adapter_name in inventory_only
            else mcp_cmd._plan_for_harness(target, adapter_name, servers, state, force=False, prune=False, adopt=False)
        )
        if len(items) == 1 and items[0]["server"] == "*":  # the plan could not read the file either
            slot["readable"] = False
            continue
        seen = set()
        for item in items:
            name = str(item["server"])
            seen.add(name)
            catalog = servers.get(name)
            native = _native_row(adapter, name, live[name]) if name in live else None
            # Transport and host come from one source, so a stdio catalog entry
            # never pairs with the host of a same-named native HTTP server.
            if native is not None and native["transport"]:
                transport, host = native["transport"], native["host"]
            else:
                transport = catalog.transport if catalog is not None else None
                host = remote_host(catalog.url) if catalog is not None and catalog.url else None
            rows[(name, harness, scope)] = {
                "name": _text(name),
                "harness": harness,
                "scope": scope,
                "transport": transport or "unknown",
                "enabled": bool(native["enabled"]) if native else True,
                "status": _text(item["status"]),
                "managed": catalog is not None
                or name in state.get("ownership", {}).get(adapter_name, {}).get(adapter.path, {}),
                "remote_host": host,
            }
        for name, raw in sorted(live.items()):
            if name in seen:
                continue
            native = _native_row(adapter, name, raw)
            rows[(name, harness, scope)] = {
                "name": _text(name),
                "harness": harness,
                "scope": scope,
                "transport": native["transport"],
                "enabled": native["enabled"],
                "status": "unmanaged",
                "managed": False,
                "remote_host": native["host"],
            }
        for name, catalog in sorted(servers.items()):
            if adapter_name in inventory_only:
                break
            if catalog.enabled or (name, harness, scope) in rows:
                continue
            if not mcp_cmd._server_targets_harness(catalog, adapter_name):
                continue
            rows[(name, harness, scope)] = {
                "name": _text(name),
                "harness": harness,
                "scope": scope,
                "transport": catalog.transport,
                "enabled": False,
                "status": "disabled",
                "managed": True,
                "remote_host": remote_host(catalog.url),
            }
    ordered = [rows[key] for key in sorted(rows)]
    return catalog_present, ordered, [harnesses[key] for key in sorted(harnesses)]


def account_connectors_enabled() -> bool:
    """The opt-in node setting: ``[fleet.mcp] include_account_connectors = true`` in ``~/.brigade/fleet.toml``."""
    from .fleet_client import FLEET_CONFIG_REL_PATH, _read_fleet_section, brigade_home

    section = _read_fleet_section(brigade_home() / FLEET_CONFIG_REL_PATH.name).get("mcp")
    return isinstance(section, dict) and section.get("include_account_connectors") is True


def parse_account_connectors(text: str) -> list[dict[str, Any]]:
    """Account connector rows from ``claude mcp list`` output; every other line is ignored."""
    rows: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        match = _ACCOUNT_LINE.match(line.strip())
        if match is None:
            continue
        name, url, tail = match.groups()
        words = re.sub(r"^[^A-Za-z0-9]+", "", tail).split(None, 1)
        status = _STATUS_WORD.get(words[0].lower().rstrip(".,:") if words else "", "unknown")
        name = _text(name)
        rows[name] = {
            "name": name,
            "harness": "claude",
            "scope": "account",
            "transport": "http",
            "enabled": True,
            "status": status,
            "managed": False,
            "remote_host": remote_host(url),
        }
    return [rows[name] for name in sorted(rows)]


def collect_account_connectors(run: Any = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(rows, harness row)``. A missing or failing ``claude`` is recorded, never reported as zero connectors."""
    marker = {"harness": "claude", "scope": "account", "config_found": True, "readable": True}
    try:
        result = (run or subprocess.run)(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=ACCOUNT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return [], {**marker, "config_found": False}
    except (subprocess.TimeoutExpired, OSError):
        return [], {**marker, "readable": False}
    if result.returncode != 0:
        return [], {**marker, "readable": False}
    return parse_account_connectors(result.stdout or ""), marker


def build_report(
    target: Path,
    *,
    now: dt.datetime | None = None,
    include_account_connectors: bool = False,
    run: Any = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    catalog_present, rows, harnesses = build_servers(target)
    if include_account_connectors:
        account_rows, account_marker = collect_account_connectors(run)
        rows = sorted([*rows, *account_rows], key=lambda r: (r["name"], r["harness"], r["scope"]))
        harnesses = [*harnesses, account_marker]
    return {
        "schema_version": SCHEMA_VERSION,
        "brigade_version": _text(__version__),
        "observed_at": now.replace(microsecond=0).isoformat(),
        "catalog_present": catalog_present,
        "truncated": len(rows) > MAX_ROWS,
        "servers": rows[:MAX_ROWS],
        "harnesses": harnesses,
    }
