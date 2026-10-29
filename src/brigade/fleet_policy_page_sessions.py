"""Sessions and audit history tables for the policy page."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .fleet_policy_page import (
    SNAPSHOT_CONTRACT_UNAVAILABLE,
    UNKNOWN_TEXT,
    PolicyView,
    SessionRow,
    _actions,
    _esc,
    _hidden,
    _labelled,
    _layer_text,
    _text,
)


def _acknowledged_cell(value: object) -> str:
    """A persisted acknowledgement field, or an explicit unknown. Never derived."""
    text = "" if value is None else str(value)
    return _esc(text) if text else f'<span class="unknown">{UNKNOWN_TEXT}</span>'


def _effective_settings_cell(row: SessionRow) -> str:
    if (
        row.effective is None
        or (row.loaded_snapshot and row.loaded_snapshot.get("status") == "unknown")
        or row.context_hash is None
    ):
        return f'<span class="unknown">{UNKNOWN_TEXT}</span>'

    leaves = []
    sources = row.sources or {}
    for section_name, section_body in sorted(row.effective.items()):
        if isinstance(section_body, Mapping):
            for k, v in sorted(section_body.items()):
                path = f"{section_name}.{k}"
                src = sources.get(path)
                layer = _layer_text(src) if isinstance(src, Mapping) else (str(src) if src else "unknown")
                leaves.append(f"<li><code>{_esc(path)}</code>: {_esc(str(v))} &middot; {_esc(layer)}</li>")
        else:
            path = section_name
            src = sources.get(path)
            layer = _layer_text(src) if isinstance(src, Mapping) else (str(src) if src else "unknown")
            leaves.append(f"<li><code>{_esc(path)}</code>: {_esc(str(section_body))} &middot; {_esc(layer)}</li>")

    if row.overrides:
        overrides_desc = f"overrides: {_esc(json.dumps(row.overrides, sort_keys=True))}"
        if row.override_reason:
            overrides_desc += f" ({_esc(row.override_reason)})"
        leaves.append(f"<li><em>{overrides_desc}</em></li>")

    leaf_html = f'<ul class="attention-list">{"".join(leaves)}</ul>'
    hash_txt = f"<div><code>hash {_esc(row.context_hash)}</code></div>"
    return f"{hash_txt}<details><summary>acknowledged settings ({len(leaves)})</summary>{leaf_html}</details>"


def _pending_cell(row: SessionRow) -> str:
    if row.pending_snapshot is None:
        return f'<span class="unknown">{UNKNOWN_TEXT}</span>'
    p_rev = row.pending_snapshot.get("revision")
    p_hash = row.pending_snapshot.get("context_hash")
    p_status = "pending apply required" if row.pending_apply_required else "pending prepared"
    hash_str = f"<div><code>{_esc(p_hash or UNKNOWN_TEXT)}</code></div>"
    return f"<div><strong>rev {_text(p_rev)}</strong> ({_esc(p_status)})</div>{hash_str}"


def _session_id_cell(row: SessionRow) -> str:
    parts = [_esc(row.session_id)]
    if row.external_session_id and row.external_session_id != row.session_id:
        parts.append(f"<br><small>ext: {_esc(row.external_session_id)}</small>")
    if row.receipt_key and row.receipt_key != row.session_id and row.receipt_key != row.external_session_id:
        parts.append(f"<br><small>key: {_esc(row.receipt_key)}</small>")
    return "".join(parts)


def _origin_cell(row: SessionRow) -> str:
    """Render origin, or UNKNOWN. Source is a separate fact and is never origin."""
    origin_html = _acknowledged_cell(row.origin)
    if row.source:
        return f"{origin_html} <small>(src: {_esc(row.source)})</small>"
    return origin_html


def sessions_section(view: PolicyView) -> str:
    """Two separated groups of facts: acknowledged snapshot vs current policy & pending refresh."""
    rows = "".join(
        f"<tr><td>{_session_id_cell(row)}</td><td>{_esc(row.consumer)}</td><td>{_text(row.repository)}</td>"
        f"<td>{_acknowledged_cell(row.owner_node)}</td><td>{_origin_cell(row)}</td>"
        f"<td>{_acknowledged_cell(row.loaded_at)}</td><td>{_acknowledged_cell(row.revision)}</td>"
        f"<td>{_acknowledged_cell(row.digest)}</td>"
        f"<td>{_effective_settings_cell(row)}</td>"
        f"<td>{_acknowledged_cell(row.current_revision)}</td>"
        f"<td>{_esc(row.state)}</td><td>{_pending_cell(row)}</td><td>{_text(row.reload)}</td>"
        f"<td>{_esc(row.refresh_state)}</td><td>{_acknowledged_cell(row.refresh_requested_at)}</td></tr>"
        for row in view.sessions
    )
    return (
        '<section class="panel" aria-labelledby="sessions"><header><h2 id="sessions">Sessions</h2>'
        f'<p class="panel-count">{len(view.sessions)} session(s)</p></header>'
        + (
            '<div class="table-wrap"><table class="roster-table"><thead>'
            '<tr><th colspan="9">Acknowledged by the session</th>'
            '<th colspan="6">Current policy and pending refresh</th></tr>'
            "<tr><th>Session</th><th>Consumer</th><th>Repository</th><th>Owner node</th><th>Origin</th>"
            "<th>Loaded at</th><th>Revision</th><th>Digest</th>"
            "<th>Loaded effective settings</th>"
            "<th>Current revision</th><th>State now</th><th>Pending snapshot</th><th>Reload</th><th>Refresh</th><th>Refresh requested</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
            if rows
            else '<p class="empty">No session receipts.</p>'
        )
        + f'<p class="hint">{_esc(SNAPSHOT_CONTRACT_UNAVAILABLE)}.</p>'
        + "</section>"
    )


def history_section(view: PolicyView, *, editable: bool, csrf: str) -> str:
    rows = "".join(
        f"<tr><td>{row['revision']}</td><td>{_text(row['created_at'])}</td><td>{_text(row['actor'])}</td>"
        f"<td>{_text(row['reason'])}</td><td>{_text(row['digest'])}</td></tr>"
        for row in view.revisions
    )
    options = "".join(
        f'<option value="{row["revision"]}">{row["revision"]} &middot; {_esc(row["reason"] or "")}</option>'
        for row in view.revisions
        if int(row["revision"]) != view.revision
    )
    form = ""
    if editable and options:
        form = (
            '<form method="post" action="/deck/policy" class="roster-form">'
            + _hidden("rollback", "", view.revision, csrf)
            + _labelled("roll back to revision", f'<select name="field.to_revision">{options}</select>')
            + _actions("rollback")
            + "</form>"
        )
    return (
        '<section class="panel" aria-labelledby="history"><header><h2 id="history">Audit history</h2>'
        f'<p class="panel-count">{len(view.revisions)} revision(s)</p></header>'
        '<div class="table-wrap"><table class="roster-table"><thead><tr><th>Revision</th><th>Created</th>'
        f"<th>Actor</th><th>Reason</th><th>Digest</th></tr></thead><tbody>{rows}</tbody></table></div>{form}</section>"
    )
