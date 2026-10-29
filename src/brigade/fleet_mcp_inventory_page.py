"""Settings > MCP servers: which servers each machine and harness has, and where they drift.

Presentation only and read-only. Every reported string is escaped; raw status
codes, scopes, transports, hosts, and ISO times are agent data behind the shared
Admin/Agent switch.
"""

from __future__ import annotations

import html
from datetime import datetime

from . import fleet_admin_explorer as explorer
from . import fleet_command_deck as deck
from . import fleet_mcp_inventory as inventory
from .fleet_mcp_inventory import HarnessConfig, Machine, McpView, Row, Server
from .fleet_skills_page import SCRIPT as COPY_SCRIPT

_esc = html.escape
_KIND = {
    "ok": "ok",
    "connected": "ok",
    "off": "muted",
    "unknown": "muted",
    "drift": "warn",
    "missing": "warn",
    "unmanaged": "warn",
    "needs auth": "warn",
    "failed": "warn",
}

CSS = """
.explorer-list nav.mcp-chips { display:flex; flex-direction:row; flex-wrap:wrap; gap:4px; margin:0 0 8px; padding:0 2px; }
.explorer-list nav.mcp-chips a { display:inline-block; min-height:0; margin:0; padding:1px 9px; border:1px solid var(--line); border-radius:999px; color:var(--muted); font-size:12px; line-height:1.7; text-decoration:none; }
.explorer-list nav.mcp-chips a:hover { background:var(--surface-raised); color:var(--ink); }
.explorer-list nav.mcp-chips a[aria-current="true"] { background:var(--surface-raised); color:var(--ink); border-color:var(--muted); }
.explorer-list nav.mcp-group { display:flex; flex-direction:row; justify-content:flex-start; align-items:center; margin:0 0 8px; padding:0 2px; }
.explorer-list nav.mcp-group .segmented { padding:2px; }
.explorer-list nav.mcp-group a { display:block; min-height:24px; margin:0; padding:0 12px; border-radius:6px; color:var(--muted); font-size:12px; line-height:24px; text-decoration:none; }
.explorer-list nav.mcp-group a[aria-current="true"] { background:var(--canvas); color:var(--ink); box-shadow:0 0 0 1px var(--line); }
.mcp-missing { margin:0 0 8px; padding:0 8px; color:var(--muted); font-size:12px; }
.explorer-list a .mcp-meta { display:flex; align-items:center; gap:8px; margin-top:4px; }
.explorer-list a .mcp-meta .pill { display:inline-block; margin:0; font-size:11px; padding:0 7px; }
.explorer-list a .mcp-meta .pill--warn { color:var(--signal); }
.explorer-list a .mcp-meta .pill--ok { color:var(--ok); }
.mcp-page .mcp-verdict { margin:0 0 16px; color:var(--ink); font-size:15px; max-width:70ch; }
.mcp-page .segmented { display:inline-flex; flex-wrap:wrap; border:1px solid var(--line); border-radius:8px; background:var(--surface-raised); }
.mcp-page .table-wrap { margin:0 0 16px; overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
.mcp-page .table-wrap table { width:100%; min-width:440px; border-collapse:collapse; font-size:13px; }
.mcp-page .table-wrap th { white-space:nowrap; padding:8px 14px; text-align:left; color:var(--muted); font-weight:500; font-size:12px; }
.mcp-page .table-wrap td { padding:9px 14px; border-top:1px solid var(--line-quiet); vertical-align:middle; }
.mcp-page .table-wrap td code { font-size:12px; overflow-wrap:anywhere; }
.mcp-page .table-wrap .pill { margin:0 4px 2px 0; }
.mcp-page table.mcp-matrix { table-layout:fixed; min-width:0; }
.mcp-page .table-wrap th:nth-child(n) { width:auto; }
.mcp-page table.mcp-matrix th:first-child { width:170px; }
.mcp-page table.mcp-matrix td, .mcp-page table.mcp-matrix th { overflow-wrap:anywhere; }
.mcp-page table.mcp-matrix td .pill { white-space:nowrap; }
.mcp-page .none { color:var(--muted); }
.mcp-publish { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin:8px 0 0; }
.mcp-publish code { padding:6px 10px; border:1px solid var(--line); border-radius:6px; font-size:12px; }
.mcp-page .copy-command { display:inline-flex; align-items:center; min-height:32px; padding:0 14px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--ink); font:inherit; font-size:13px; cursor:pointer; }
.mcp-page .copy-command:hover { background:var(--surface-raised); }
"""


def anchor_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _copy_button(command: str) -> str:
    return (
        f'<button type="button" class="copy-command" data-command="{_esc(command, quote=True)}" '
        f'aria-label="Copy command">Copy</button>'
    )


def _table(headers: tuple[str, ...], rows: list[str], *, css: str = "") -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    mark = f' class="{css}"' if css else ""
    return f'<div class="table-wrap"><table{mark}><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _status_pill(row: Row) -> str:
    return explorer.pill(row.label, _KIND[row.label])


def _matrix(server: Server, view: McpView) -> str:
    columns = view.columns
    rows = []
    for machine in view.machines:
        if not machine.reported:
            rows.append(
                f'<tr><td>{_esc(machine.label)}</td><td class="none" colspan="{max(len(columns), 1)}">No MCP report</td></tr>'
            )
            continue
        cells = []
        for harness in columns:
            mine = [r for r in server.rows if r.node_id == machine.node_id and r.column == harness]
            top = inventory.worst(mine)
            configs = [c for c in machine.harnesses if c.column == harness]
            if top is not None:
                cells.append(f"<td>{_status_pill(top)}</td>")
            elif configs and any(not c.readable for c in configs):
                cells.append(f"<td>{explorer.pill('unreadable', 'warn')}</td>")
            else:
                cells.append('<td class="none">-</td>')
        rows.append(f"<tr><td>{_esc(machine.label)}</td>{''.join(cells)}</tr>")
    return _table(("Machine", *(h.capitalize() for h in columns)), rows, css="mcp-matrix")


def _server_agent(server: Server, view: McpView) -> str:
    rows = [
        f"<tr><td>{_esc(r.machine)}</td><td>{_esc(r.harness)}</td><td><code>{_esc(r.scope)}</code></td>"
        f"<td><code>{_esc(r.transport)}</code></td><td><code>{_esc(r.remote_host or 'none')}</code></td>"
        f"<td>{'yes' if r.managed else 'no'}</td><td>{'yes' if r.enabled else 'no'}</td>"
        f"<td><code>{_esc(r.status)}</code></td></tr>"
        for r in sorted(server.rows, key=lambda r: (r.machine, r.harness, r.scope))
    ]
    node_ids = {row.node_id for row in server.rows}
    reports = [
        f"<tr><td>{_esc(m.label)}</td><td>{_esc(m.brigade_version)}</td><td>{_esc(m.observed_at)}</td>"
        f"<td>{'yes' if m.catalog_present else 'no'}</td><td>{_esc(_configs(m))}</td></tr>"
        for m in view.machines
        if m.reported and m.node_id in node_ids
    ]
    return explorer.agent(
        _table(("Machine", "Harness", "Scope", "Transport", "Remote host", "Managed", "Enabled", "Status code"), rows)
        + (_table(("Machine", "Brigade", "Reported", "Catalog present", "Harness configs"), reports) if reports else "")
        + (
            "<p>Account connectors come from <code>claude mcp list</code> on that machine.</p>"
            if any(r.scope == "account" for r in server.rows)
            else ""
        )
        + f"<p>Check with <code>{_esc(inventory.SYNC_COMMAND)}</code> after <code>{_esc(inventory.PUBLISH_COMMAND)}</code>.</p>"
    )


def _configs(machine: Machine) -> str:
    return ", ".join(
        f"{c.harness}/{c.scope}: {'found' if c.config_found else 'not found'}, {'readable' if c.readable else 'unreadable'}"
        for c in machine.harnesses
    )


def _server_detail(server: Server, view: McpView) -> str:
    return (
        explorer.hero(server.name, server.issue, server.issue_kind)
        + f'<p class="mcp-verdict">{_esc(inventory.verdict(server))}</p>'
        + _matrix(server, view)
        + _server_agent(server, view)
    )


def _harness_cell(machine: Machine, config: HarnessConfig) -> str:
    if not config.readable:
        return explorer.pill("unreadable", "warn")
    if not config.config_found:
        return '<span class="none">No config found</span>'
    rows = [r for r in machine.rows if r.harness == config.harness and r.scope == config.scope]
    if not rows:
        return '<span class="none">No servers</span>'
    return "".join(
        explorer.pill(r.name if r.label in inventory.HEALTHY else f"{r.name} ({r.label})", _KIND[r.label])
        for r in sorted(rows, key=lambda r: r.name)
    )


def _machine_agent(machine: Machine) -> str:
    rows = [
        f"<tr><td>{_esc(r.name)}</td><td>{_esc(r.harness)}</td><td><code>{_esc(r.scope)}</code></td>"
        f"<td><code>{_esc(r.transport)}</code></td><td><code>{_esc(r.remote_host or 'none')}</code></td>"
        f"<td>{'yes' if r.managed else 'no'}</td><td>{'yes' if r.enabled else 'no'}</td>"
        f"<td><code>{_esc(r.status)}</code></td></tr>"
        for r in sorted(machine.rows, key=lambda r: (r.name, r.harness, r.scope))
    ]
    configs = [
        f"<tr><td>{_esc(c.harness)}</td><td><code>{_esc(c.scope)}</code></td>"
        f"<td>{'yes' if c.config_found else 'no'}</td><td>{'yes' if c.readable else 'no'}</td></tr>"
        for c in machine.harnesses
    ]
    return explorer.agent(
        explorer.facts(
            {
                "Brigade": machine.brigade_version,
                "Reported": machine.observed_at,
                "Catalog present": "yes" if machine.catalog_present else "no",
                "Truncated": "yes" if machine.truncated else "",
            }
        )
        + _table(("Harness", "Scope", "Config found", "Readable"), configs)
        + (
            _table(
                ("Server", "Harness", "Scope", "Transport", "Remote host", "Managed", "Enabled", "Status code"), rows
            )
            if rows
            else ""
        )
    )


def _machine_detail(machine: Machine) -> str:
    if not machine.reported:
        return (
            explorer.hero(machine.label, "No MCP report", "warn")
            + f'<p class="mcp-verdict">{_esc(inventory.machine_verdict(machine))}</p>'
            + _publish_hint()
        )
    kind = "warn" if machine.attention or machine.unreadable or not machine.catalog_present else "ok"
    status = _health_status(machine)
    body = _table(
        ("Harness", "Servers"),
        [
            f'<tr><td>{_esc(c.harness.capitalize())} <span class="none">({_esc(c.scope)})</span></td>'
            f"<td>{_harness_cell(machine, c)}</td></tr>"
            for c in machine.harnesses
        ],
    )
    return (
        explorer.hero(machine.label, status, kind)
        + f'<p class="mcp-verdict">{_esc(inventory.machine_verdict(machine))}</p>'
        + body
        + _machine_agent(machine)
    )


def _publish_hint() -> str:
    return (
        '<div class="mcp-publish"><span>No MCP reports yet. Publish one from a machine with</span>'
        f"<code>{_esc(inventory.PUBLISH_COMMAND)}</code>{_copy_button(inventory.PUBLISH_COMMAND)}</div>"
    )


def _group_switch(group: str) -> str:
    current = ' aria-current="true"'
    links = "".join(
        f'<a href="/deck/mcp?group={key}"{current if key == group else ""}>{_esc(label)}</a>'
        for key, label in inventory.GROUPS
    )
    return f'<nav class="mcp-group" aria-label="Group by"><div class="segmented">{links}</div></nav>'


def _chips(view: McpView, active: str) -> str:
    links = []
    for key, label in inventory.FILTERS:
        count = sum(1 for server in view.servers if inventory.matches(server, key))
        current = ' aria-current="true"' if key == active else ""
        links.append(f'<a href="/deck/mcp?group=server&amp;filter={key}"{current}>{_esc(label)} {count}</a>')
    return f'<nav class="mcp-chips" aria-label="Server filters">{"".join(links)}</nav>'


def _page(content: str, *, nonce: str, now: datetime) -> str:
    return deck._document(
        f'<main class="deck-shell admin-page mcp-page">{content}</main>',
        nonce=nonce,
        now=now,
        title="MCP servers",
        active="mcp",
        view=None,
        refresh=False,
        extra_css=explorer.CSS + CSS,
        script=explorer.SCRIPT + COPY_SCRIPT,
    )


def render(view: McpView, *, nonce: str, now: datetime, group: str = "server", filter_key: str = "needs") -> str:
    if group not in dict(inventory.GROUPS):
        group = "server"
    if filter_key not in dict(inventory.FILTERS):
        filter_key = "needs"
    scope_note = explorer.agent(
        "<p>Only locally configured MCP servers are shown. Account-level connectors are not in local config files. "
        "claude.ai connectors appear only for machines that report them with "
        "<code>brigade fleet mcp report --include-account-connectors</code>, read from "
        "<code>claude mcp list</code> on that machine. ChatGPT apps are not included.</p>"
    )
    if view.reports == 0:
        body = '<section class="panel"><h2>MCP servers</h2>' + _publish_hint() + "</section>" + scope_note
        return _page(body, nonce=nonce, now=now)
    missing = [machine.label for machine in view.machines if not machine.reported]
    note = f'<p class="mcp-missing">No MCP report: {_esc(", ".join(missing))}</p>' if missing else ""
    if group == "machine":
        entries = [
            explorer.Entry(
                anchor_key("machine", machine.node_id),
                machine.label,
                _machine_status(machine),
                _machine_detail(machine),
                f'<span class="mcp-meta">{_machine_pill(machine)}</span>',
            )
            for machine in view.machines
        ]
        toolbar = _group_switch(group)
        label, empty = "machines", "No machines are enrolled."
    else:
        shown = [server for server in view.servers if inventory.matches(server, filter_key)]
        entries = [
            explorer.Entry(
                anchor_key("server", server.name),
                server.name,
                inventory.summary(server),
                _server_detail(server, view),
                f'<span class="mcp-meta">{_account_pill(server)}{explorer.pill(server.issue, server.issue_kind)}</span>',
            )
            for server in shown
        ]
        toolbar = _group_switch(group) + _chips(view, filter_key) + note
        label = "servers"
        empty = "Nothing needs attention." if view.servers else "No MCP servers are configured on reporting machines."
    listing = explorer.render(entries, label=label, empty=empty, toolbar=toolbar)
    return _page(listing + scope_note, nonce=nonce, now=now)


def _account_pill(server: Server) -> str:
    return explorer.pill("Account", "muted") if any(r.scope == "account" for r in server.rows) else ""


def _machine_status(machine: Machine) -> str:
    if not machine.reported:
        return "No MCP report"
    servers = len({row.name for row in machine.rows})
    harnesses = len({row.harness for row in machine.rows})
    return f"{servers} server{'' if servers == 1 else 's'}, {harnesses} harness{'' if harnesses == 1 else 'es'}"


def _health_status(machine: Machine) -> str:
    if machine.attention:
        return inventory.attention_text(machine.attention)
    return "unreadable config" if machine.unreadable else "ok"


def _machine_pill(machine: Machine) -> str:
    if not machine.reported:
        return explorer.pill("No MCP report", "warn")
    status = _health_status(machine)
    return explorer.pill(status, "ok" if status == "ok" else "warn")
