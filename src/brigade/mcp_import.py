"""`brigade mcp import`: read a tool's native MCP config into the canonical catalog.

Split out of ``mcp_cmd`` to keep that module under the size ceiling. ``mcp_cmd`` re-exports
``import_servers``; this module reaches back into it lazily to avoid an import cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import mcp_adapters, mcp_native_auth
from .mcp_adapters import ADAPTERS, CanonicalServer
from .render import emit as _emit


def import_servers(
    *,
    target: Path,
    harness: str,
    merge: bool = False,
    user_scope: bool = False,
    keep_secrets: bool = False,
    json_output: bool = False,
) -> int:
    from . import mcp_cmd

    target = target.expanduser().resolve()
    harness = mcp_cmd._scoped_harness(harness, user_scope=user_scope)
    adapter = ADAPTERS.get(harness)
    if adapter is None:
        return _emit(
            {"errors": [f"unknown MCP target {harness!r}"]}, json_output, [f"error: unknown target {harness!r}"], 2
        )
    if adapter.user_scope and not user_scope:
        return _emit(
            {"errors": [f"{harness} is user-scoped; pass --user-scope"]},
            json_output,
            [f"error: {harness} is user-scoped; pass --user-scope"],
            2,
        )
    path = mcp_adapters.resolve_path(adapter, target)
    if not path.is_file():
        return _emit({"errors": [f"{path}: not found"]}, json_output, [f"error: {path} not found"], 2)
    warnings: list[str] = []
    layout_conflicts: list[dict[str, Any]] = []
    try:
        text = path.read_text()
        live = adapter.read_file(text)
        if harness in ("opencode", "opencode-user"):
            view = mcp_adapters.inspect_opencode_config(text)
            warnings = list(view.warnings)
            layout_conflicts = [{"server": n, "locations": ["flat", "nested"]} for n in view.duplicates]
    except mcp_adapters.OpenCodeLayoutError as exc:
        return _emit({"errors": [str(exc)], **exc.fields}, json_output, [f"error: {exc}"], 2)
    existing, errors, _ = mcp_cmd.load_canonical(target)
    if errors and merge:
        return _emit({"errors": errors}, json_output, [f"error: {e}" for e in errors], 2)
    discovered: list[str] = []
    secrets_demoted: list[str] = []
    secrets_dropped: list[str] = []
    skipped_existing: list[str] = []
    native_auth_merged: dict[str, CanonicalServer] = {}
    to_add: dict[str, CanonicalServer] = {}
    for srv_name, raw in sorted(live.items()):
        try:
            server, demoted = adapter.from_provider(srv_name, raw, keep_secrets=keep_secrets)
        except ValueError as exc:
            return _emit({"errors": [f"{srv_name}: {exc}"]}, json_output, [f"error: {srv_name}: {exc}"], 2)
        discovered.append(srv_name)
        for field_name in demoted:
            destination = (
                secrets_dropped
                if harness in ("opencode", "opencode-user") and field_name not in (server.headers | server.env)
                else secrets_demoted
            )
            destination.append(f"{srv_name}.{field_name}")
        if srv_name in existing:
            # A second tool's view of a server already in the catalog may add the native
            # auth the first view could not express. Same name, same URL, no auth yet.
            merged = mcp_native_auth.merge_imported_native_auth(existing[srv_name], server)
            if merged is not None:
                native_auth_merged[srv_name] = merged
            else:
                skipped_existing.append(srv_name)
            continue
        to_add[srv_name] = server
    if merge:
        existing.update(native_auth_merged)
        existing.update(to_add)
        mcp_cmd._write_canonical(target, existing)
    payload = {
        "harness": harness,
        "merged": merge,
        "discovered": discovered,
        "added": sorted(to_add),
        "native_auth_merged": sorted(native_auth_merged),
        "skipped_existing": skipped_existing,
        "secrets_demoted": secrets_demoted,
        "secrets_dropped": secrets_dropped,
        "warnings": warnings,
        "layout_conflicts": layout_conflicts,
    }
    lines = [f"brigade mcp import {harness}: {len(discovered)} discovered, {len(to_add)} new"]
    lines += [f"warning: {warning}" for warning in warnings]
    lines += [f"+ {n}" for n in sorted(to_add)]
    lines += [f"native auth merged into existing entry: {n}" for n in sorted(native_auth_merged)]
    lines += [f"secret demoted to ref: {s}" for s in secrets_demoted]
    lines += [f"field dropped from canonical: {s}" for s in secrets_dropped]
    if not merge:
        lines.append("(preview only; pass --merge to write into .brigade/mcp.json)")
    return _emit(payload, json_output, lines, 0)
