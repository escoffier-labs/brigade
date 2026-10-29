"""Settings > PR rules: one place for review and CI policy that people edit and agents read.

Presentation and form building only. Rules live in the ``pr_rules`` section of
the fleet policy document, so every edit goes through the policy page's draft,
preview, confirm, rollback, CSRF, and revision flow (``fleet_policy_page.apply``).
Phase 1 does not touch GitHub and enforces nothing. Every stored string is
escaped, and plain-language sentences are generated from structured fields
only.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import fleet_admin_explorer as explorer
from . import fleet_command_deck as deck
from . import fleet_policy_pr_rules as rules_mod
from .fleet_policy_form import NEW_TARGET, Submission
from .fleet_policy_page import Plan, _confirm_form, csrf_value
from .ui_theme import empty_state

__all__ = ["csrf_value", "render", "FORM_ACTION", "READ_ONLY_NOTICE"]

FORM_ACTION = "/deck/pr-rules"
READ_ONLY_NOTICE = "Access: read-only. You can inspect PR rules; editing requires an authorized admin request."
AGENT_COMMAND = "brigade fleet policy pr-rules"
EMPTY_TEXT = "No PR rules yet"
EMPTY_HINT = "PR rules decide how pull requests are handled, and each rule appears in this list once added."
ACTION_SLOTS = 3
CONFIRM_HINT = (
    "Saves exactly the change previewed above as a new policy revision. If the policy changed since the preview, "
    "the save is refused. Nothing is applied to GitHub."
)
_esc = html.escape

CSS = """
.rules-page .access-note { margin:0 0 12px; color:var(--muted); font-size:12px; }
.rules-page .callout { margin:0 0 12px; padding:0; border:0; background:none; font-size:13px; color:var(--ink); }
.rules-page .callout[role="alert"] { color:var(--bad); }
.rules-bar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin:0 0 12px; }
.rules-bar p { margin:0; color:var(--muted); font-size:13px; }
.rules-page button, .rules-page .rules-button { display:inline-flex; align-items:center; min-height:32px; padding:0 14px; border:1px solid var(--line); border-radius:8px; background:transparent; color:var(--ink); font:inherit; font-size:13px; cursor:pointer; }
.rules-page button:hover { background:var(--surface-raised); }
.rules-page button.primary { background:var(--ink); color:var(--canvas); border-color:var(--ink); font-weight:500; }
.rules-page button.primary:hover { background:var(--ink); }
.rules-page button.danger { color:var(--bad); }
.rules-page button:focus-visible, .rules-page summary:focus-visible, .rules-page input:focus-visible, .rules-page select:focus-visible { outline:2px solid var(--signal); outline-offset:2px; }
.explorer-list a .rule-meta { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-top:4px; }
.explorer-list a .rule-meta .pill { display:inline-block; margin:0; font-size:11px; padding:0 7px; }
.rules-page .rule-sentence { margin:0 0 8px; max-width:70ch; font-size:15px; line-height:1.5; }
.rules-page .rule-why { margin:0 0 16px; max-width:70ch; color:var(--muted); font-size:13px; }
.rules-page .rule-tools { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:16px 0; }
.rules-page .rule-tools form { margin:0; }
.rules-page details.rule-edit, .rules-page details.rule-add { margin:0 0 16px; border:1px solid var(--line); border-radius:8px; }
.rules-page details.rule-edit > summary, .rules-page details.rule-add > summary { padding:8px 14px; cursor:pointer; font-size:13px; }
.rules-page details.rule-add { margin:0 0 12px; }
.rules-page form.rule-form { display:grid; gap:12px; max-width:860px; padding:4px 14px 14px; }
.rules-page .rule-form label { display:grid; gap:4px; color:var(--muted); font-size:12px; }
.rules-page .rule-form input[type="text"], .rules-page .rule-form select { min-height:32px; padding:0 8px; border:1px solid var(--line); border-radius:6px; background:var(--canvas); color:var(--ink); font:inherit; font-size:13px; }
.rules-page .rule-form fieldset { margin:0; padding:10px 12px; border:1px solid var(--line-quiet); border-radius:6px; display:grid; gap:10px; }
.rules-page .rule-form legend { padding:0 4px; color:var(--muted); font-size:12px; }
.rules-page .rule-action-row { display:grid; grid-template-columns:minmax(150px,1.2fr) minmax(110px,1fr) minmax(130px,1.5fr) 72px 90px; gap:8px; align-items:end; }
.rules-page .rule-action-row select, .rules-page .rule-action-row input { width:100%; min-width:0; box-sizing:border-box; }
.rules-page .rule-form label.rule-check { display:flex; align-items:center; gap:8px; color:var(--ink); font-size:13px; }
.rules-page .rule-form .hint { margin:0; color:var(--muted); font-size:12px; }
.rules-page .rule-form-actions { display:flex; flex-wrap:wrap; align-items:end; gap:10px; }
.rules-page .rule-form-actions label { flex:1 1 220px; }
.rules-page .rule-form[data-enhanced] .cond-param { display:none; }
.rules-page .rule-form[data-enhanced][data-cond="docs_only"] .cond-docs_only,
.rules-page .rule-form[data-enhanced][data-cond="review_quota_exhausted"] .cond-review_quota_exhausted,
.rules-page .rule-form[data-enhanced][data-cond="label_present"] .cond-label_present,
.rules-page .rule-form[data-enhanced][data-cond="repository"] .cond-repository { display:grid; }
.rules-page .rule-form[data-enhanced] .act-part { display:none; }
.rules-page .rule-form[data-enhanced] .rule-action-row[data-kind="skip_label"] .act-value,
.rules-page .rule-form[data-enhanced] .rule-action-row[data-kind="require_label"] .act-value,
.rules-page .rule-form[data-enhanced] .rule-action-row[data-kind="note"] .act-value,
.rules-page .rule-form[data-enhanced] .rule-action-row[data-kind="use_reviewer"] .act-reviewer,
.rules-page .rule-form[data-enhanced] .rule-action-row[data-kind="no_paid_credits"] .act-reviewer,
.rules-page .rule-form[data-enhanced] .rule-action-row[data-kind="limit_reviewer"] .act-reviewer,
.rules-page .rule-form[data-enhanced] .rule-action-row[data-kind="limit_reviewer"] .act-count,
.rules-page .rule-form[data-enhanced] .rule-action-row[data-kind="limit_reviewer"] .act-period { display:grid; }
.rules-plan { margin:0 0 16px; border:1px solid var(--line); border-radius:8px; }
.rules-plan > header { padding:10px 14px; border-bottom:1px solid var(--line); }
.rules-plan > header h2 { margin:0; font-size:14px; font-weight:600; }
.rules-plan > header p { margin:2px 0 0; color:var(--muted); font-size:12px; }
.rules-plan ul { list-style:none; margin:0; padding:0; }
.rules-plan li { padding:8px 14px; border-top:1px solid var(--line-quiet); font-size:13px; }
.rules-plan li:first-child { border-top:0; }
.rules-plan .change-kind { display:inline-block; min-width:64px; color:var(--muted); font-size:12px; }
.rules-plan form { padding:10px 14px 14px; }
.rules-plan .hint { margin:8px 0 0; color:var(--muted); font-size:12px; }
.rules-page pre.rule-json { margin:0 0 12px; padding:10px 12px; border:1px solid var(--line); border-radius:6px; overflow-x:auto; font-size:12px; }
.rules-page .agent-command { display:block; margin:0 0 12px; font-size:12px; overflow-wrap:anywhere; }
@media (max-width:700px) {
 .rules-page .rule-action-row { grid-template-columns:1fr 1fr; }
 .rules-page .rule-action-row .act-kind { grid-column:1 / -1; }
}
"""

SCRIPT = """(function(){function start(){document.querySelectorAll('form.rule-form').forEach(function(form){
form.setAttribute('data-enhanced','');
var cond=form.querySelector('select[name="field.condition"]');
function setCond(){form.setAttribute('data-cond',cond?cond.value:'');}
if(cond){cond.addEventListener('change',setCond);setCond();}
form.querySelectorAll('.rule-action-row').forEach(function(row){
var kind=row.querySelector('select.act-kind-select');
function set(){row.setAttribute('data-kind',kind?kind.value:'');}
if(kind){kind.addEventListener('change',set);set();}});});}
(typeof window!=='undefined'&&window.fleetOnInit||function(f){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',f);else f();})(start);})();"""


def _hidden(scope_target: str, revision: int, csrf: str) -> str:
    return (
        '<input type="hidden" name="scope" value="pr_rules">'
        f'<input type="hidden" name="target" value="{_esc(scope_target, quote=True)}">'
        f'<input type="hidden" name="expected_version" value="{revision}">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf, quote=True)}">'
    )


def _pill_scope(rule: Mapping[str, Any]) -> str:
    return explorer.pill(rules_mod.scope_text(rule), "muted")


def _summary(rule: Mapping[str, Any]) -> str:
    off = "" if rule["enabled"] else explorer.pill("Off", "muted")
    return f'<span class="rule-meta">{_pill_scope(rule)}{off}</span>'


def _rule_json(rule: Mapping[str, Any]) -> str:
    return json.dumps(rule, indent=2, sort_keys=True)


def _preview_row(reason: str = "") -> str:
    return (
        '<div class="rule-form-actions"><label>Reason (optional)'
        f'<input type="text" name="reason" maxlength="240" value="{_esc(reason, quote=True)}"></label>'
        '<button type="submit" name="action" value="preview" class="primary">Preview change</button></div>'
        '<p class="hint">Preview shows what changes. Nothing is saved until you confirm.</p>'
    )


def _draft_field(draft: Submission | None, target: str, name: str, fallback: str) -> str:
    if draft is not None and draft.scope == "pr_rules" and draft.target == target and name in draft.fields:
        return draft.fields[name]
    return fallback


def _select(name: str, options: Sequence[tuple[str, str]], current: str, *, blank: str | None = None) -> str:
    rows = [f'<option value="">{_esc(blank)}</option>'] if blank is not None else []
    rows.extend(
        f'<option value="{_esc(value, quote=True)}"{" selected" if value == current else ""}>{_esc(label)}</option>'
        for value, label in options
    )
    return f'<select name="{_esc(name, quote=True)}">{"".join(rows)}</select>'


def _action_parts(action: str) -> dict[str, str]:
    kind, _, argument = action.partition(":")
    parts = {"kind": kind, "reviewer": "", "value": "", "count": "", "period": "day"}
    if kind in ("skip_label", "require_label", "note"):
        parts["value"] = argument
    elif kind in ("use_reviewer", "no_paid_credits"):
        parts["reviewer"] = argument
    elif kind == "limit_reviewer":
        reviewer, _, rate = argument.partition(":")
        count, _, period = rate.partition("_per_")
        parts.update(reviewer=reviewer, count=count, period=period or "day")
    return parts


def _action_rows(rule: Mapping[str, Any] | None, draft: Submission | None, target: str) -> str:
    current = [_action_parts(action) for action in (rule["then"] if rule else [])]
    slots = min(rules_mod.MAX_THEN, max(len(current) + 1, ACTION_SLOTS))
    kinds = [(kind, rules_mod.ACTION_LABELS[kind]) for kind in rules_mod.ACTION_KINDS]
    reviewers = [(name, rules_mod.REVIEWER_LABELS[name]) for name in rules_mod.REVIEWERS]
    periods = [("day", "per day"), ("week", "per week")]
    rows = []
    for index in range(slots):
        base = current[index] if index < len(current) else {}
        values = {
            key: _draft_field(draft, target, f"act_{key}_{index}", base.get(key, "day" if key == "period" else ""))
            for key in ("kind", "reviewer", "value", "count", "period")
        }
        rows.append(
            f'<div class="rule-action-row" data-kind="{_esc(values["kind"], quote=True)}">'
            f'<label class="act-kind">Action {index + 1}'
            + _select(f"field.act_kind_{index}", kinds, values["kind"], blank="None").replace(
                "<select ", '<select class="act-kind-select" ', 1
            )
            + "</label>"
            '<label class="act-part act-reviewer">Reviewer'
            + _select(f"field.act_reviewer_{index}", reviewers, values["reviewer"], blank="Choose")
            + "</label>"
            '<label class="act-part act-value">Label or note text'
            f'<input type="text" name="field.act_value_{index}" maxlength="{rules_mod.MAX_NOTE}" '
            f'value="{_esc(values["value"], quote=True)}"></label>'
            '<label class="act-part act-count">Limit'
            f'<input type="text" inputmode="numeric" name="field.act_count_{index}" maxlength="3" '
            f'value="{_esc(values["count"], quote=True)}"></label>'
            '<label class="act-part act-period">Per'
            + _select(f"field.act_period_{index}", periods, values["period"])
            + "</label></div>"
        )
    return "".join(rows)


def _when_defaults(rule: Mapping[str, Any] | None) -> dict[str, str]:
    when = rule["when"] if rule else {"condition": "docs_only"}
    return {
        "condition": str(when["condition"]),
        "paths": " ".join(when.get("paths", [])),
        "reviewer": str(when.get("reviewer", "")),
        "label": str(when.get("label", "")),
        "repos": " ".join(when.get("repos", [])),
    }


def _rule_form(
    rule: Mapping[str, Any] | None, *, revision: int, csrf: str, draft: Submission | None, button_class: str = ""
) -> str:
    target = NEW_TARGET if rule is None else str(rule["id"])
    when = _when_defaults(rule)
    get = lambda name, fallback: _draft_field(draft, target, name, fallback)  # noqa: E731
    conditions = [(name, rules_mod.CONDITION_LABELS[name]) for name in rules_mod.CONDITIONS]
    reviewers = [(name, rules_mod.REVIEWER_LABELS[name]) for name in rules_mod.REVIEWERS]
    enabled = rule["enabled"] if rule else True
    if draft is not None and draft.scope == "pr_rules" and draft.target == target:
        enabled = "enabled" in draft.fields
    identity = (
        "<label>Rule id (lowercase letters, digits, dashes)"
        f'<input type="text" name="field.id" maxlength="64" pattern="[a-z0-9][a-z0-9\\-]*" required '
        f'value="{_esc(get("id", ""), quote=True)}"></label>'
        if rule is None
        else ""
    )
    return (
        f'<form method="post" action="{FORM_ACTION}" class="rule-form" data-cond="{_esc(get("condition", when["condition"]), quote=True)}">'
        + _hidden(target, revision, csrf)
        + identity
        + "<label>Title (what a person reads)"
        f'<input type="text" name="field.title" maxlength="{rules_mod.MAX_TITLE}" required '
        f'value="{_esc(get("title", rule["title"] if rule else ""), quote=True)}"></label>'
        "<fieldset><legend>When</legend>"
        "<label>Condition" + _select("field.condition", conditions, get("condition", when["condition"])) + "</label>"
        '<label class="cond-param cond-docs_only">Doc paths, space separated (blank uses docs/** and *.md)'
        f'<input type="text" name="field.when_paths" maxlength="600" value="{_esc(get("when_paths", when["paths"]), quote=True)}"></label>'
        '<label class="cond-param cond-review_quota_exhausted">Reviewer that is out of reviews'
        + _select("field.when_reviewer", reviewers, get("when_reviewer", when["reviewer"]), blank="Choose")
        + "</label>"
        '<label class="cond-param cond-label_present">PR label'
        f'<input type="text" name="field.when_label" maxlength="64" value="{_esc(get("when_label", when["label"]), quote=True)}"></label>'
        '<label class="cond-param cond-repository">Repositories (owner/name, space separated)'
        f'<input type="text" name="field.when_repos" maxlength="2000" value="{_esc(get("when_repos", when["repos"]), quote=True)}"></label>'
        "</fieldset>"
        "<fieldset><legend>Then</legend>" + _action_rows(rule, draft, target) + "</fieldset>"
        "<label>Only these repositories (blank means every fleet repository)"
        f'<input type="text" name="field.scope_repos" maxlength="2000" '
        f'value="{_esc(get("scope_repos", " ".join(rule["scope"]["repos"]) if rule else ""), quote=True)}"></label>'
        f'<label>Why (optional)<input type="text" name="field.why" maxlength="{rules_mod.MAX_WHY}" '
        f'value="{_esc(get("why", (rule["why"] or "") if rule else ""), quote=True)}"></label>'
        f'<label class="rule-check"><input type="checkbox" name="field.enabled" value="1"{" checked" if enabled else ""}> Rule is on</label>'
        + _preview_row(_draft_reason(draft, target))
        + "</form>"
    )


def _draft_reason(draft: Submission | None, target: str) -> str:
    if draft is not None and draft.scope == "pr_rules" and draft.target == target:
        return draft.reason
    return ""


def _one_button_form(
    rule: Mapping[str, Any], field: tuple[str, str], label: str, *, revision: int, csrf: str, danger: bool = False
) -> str:
    danger_class = ' class="danger"' if danger else ""
    return (
        f'<form method="post" action="{FORM_ACTION}">'
        + _hidden(str(rule["id"]), revision, csrf)
        + f'<input type="hidden" name="field.{field[0]}" value="{_esc(field[1], quote=True)}">'
        f'<button type="submit" name="action" value="preview"{danger_class}>{_esc(label)}</button></form>'
    )


def _agent_block(rule: Mapping[str, Any], revision: int) -> str:
    return explorer.agent(
        explorer.facts(
            {"Rule id": rule["id"], "Policy revision": revision, "Command": f"{AGENT_COMMAND} --json"},
        )
        + f'<pre class="rule-json"><code>{_esc(_rule_json(rule))}</code></pre>'
    )


def _detail(rule: Mapping[str, Any], *, revision: int, csrf: str, editable: bool, draft: Submission | None) -> str:
    repos = rule["scope"]["repos"]
    parts = [
        explorer.hero(str(rule["title"]), "On" if rule["enabled"] else "Off", "ok" if rule["enabled"] else "muted"),
        f'<p class="rule-sentence">{_esc(rules_mod.sentence(rule))}</p>',
    ]
    if rule["why"]:
        parts.append(f'<p class="rule-why">Why: {_esc(rule["why"])}</p>')
    parts.append(explorer.facts({"Applies to": ", ".join(repos) if repos else "All fleet repositories"}))
    if editable:
        target = str(rule["id"])
        opened = " open" if draft is not None and draft.scope == "pr_rules" and draft.target == target else ""
        toggle = ("toggle", "disable" if rule["enabled"] else "enable")
        parts.append(
            '<div class="rule-tools">'
            + _one_button_form(rule, toggle, "Turn off" if rule["enabled"] else "Turn on", revision=revision, csrf=csrf)
            + _one_button_form(rule, ("delete", "1"), "Delete rule", revision=revision, csrf=csrf, danger=True)
            + "</div>"
            f'<details class="rule-edit"{opened}><summary>Edit rule</summary>'
            + _rule_form(rule, revision=revision, csrf=csrf, draft=draft)
            + "</details>"
        )
    parts.append(_agent_block(rule, revision))
    return "".join(parts)


def _change_rows(current: Sequence[Mapping[str, Any]], proposed: Sequence[Mapping[str, Any]]) -> list[str]:
    before = rules_mod.by_id(current)
    after = rules_mod.by_id(proposed)
    rows = []
    for rule_id, rule in after.items():
        if rule_id not in before:
            rows.append(("Added", rule))
        elif before[rule_id] != rule:
            rows.append(("Changed", rule))
    rows.extend(("Removed", rule) for rule_id, rule in before.items() if rule_id not in after)
    return [
        f'<li><span class="change-kind">{kind}</span> <strong>{_esc(rule["title"])}</strong>'
        f'<br><span class="muted">{_esc(rules_mod.sentence(rule))}</span></li>'
        for kind, rule in rows
    ]


def _plan_panel(
    plan: Plan | None, current: Sequence[Mapping[str, Any]], *, draft: Submission | None, review: str | None
) -> str:
    # A refused plan carries an unvalidated draft document; only a valid one is summarised.
    if plan is None or plan.document is None or not plan.ok:
        return ""
    changes = _change_rows(current, plan.document.get("pr_rules") or [])
    body = "".join(changes) if changes else '<li class="muted">No change to any rule.</li>'
    confirm = _confirm_form(draft, review, action=FORM_ACTION, hint=CONFIRM_HINT) if review and changes else ""
    return (
        '<section class="rules-plan" aria-labelledby="rules-plan"><header><h2 id="rules-plan">Proposed change</h2>'
        f"<p>Policy revision {plan.current_revision} to {plan.next_revision}. Nothing is saved yet.</p></header>"
        f"<ul>{body}</ul>{confirm}</section>"
    )


def _add_panel(*, revision: int, csrf: str, draft: Submission | None) -> str:
    opened = " open" if draft is not None and draft.scope == "pr_rules" and draft.target == NEW_TARGET else ""
    return (
        f'<details class="rule-add" id="add-rule"{opened}><summary>Add rule</summary>'
        + _rule_form(None, revision=revision, csrf=csrf, draft=draft)
        + "</details>"
    )


def _agent_summary(revision: int, count: int) -> str:
    return explorer.agent(
        explorer.facts(
            {
                "Policy revision": revision,
                "Rules stored": count,
                "Read the enabled rules for a repository": f"{AGENT_COMMAND} --repo owner/name",
                "Machine readable": f"{AGENT_COMMAND} --repo owner/name --json",
                "Enforcement": "none in this phase; rules are published for agents to read and follow",
            }
        )
    )


def render(
    rules: Sequence[Mapping[str, Any]],
    *,
    revision: int,
    nonce: str,
    now: datetime,
    csrf: str,
    editable: bool,
    banner: str | None = None,
    error: str | None = None,
    plan: Plan | None = None,
    draft: Submission | None = None,
    review: str | None = None,
) -> str:
    top = ""
    if banner:
        top += f'<p class="callout" role="status">{_esc(banner)}</p>'
    if error:
        top += f'<p class="callout" role="alert">{_esc(error)}</p>'
    if not editable:
        top += f'<p class="access-note">{_esc(READ_ONLY_NOTICE)}</p>'
    top += _plan_panel(plan, rules, draft=draft, review=review) if editable else ""
    count = len(rules)
    if count == 0:
        body = top + f'<section class="panel"><h2>PR rules</h2>{empty_state(EMPTY_TEXT, EMPTY_HINT)}</section>'
        if editable:
            body += _add_panel(revision=revision, csrf=csrf, draft=draft)
        body += _agent_summary(revision, 0)
    else:
        entries = [
            explorer.Entry(
                "rule:" + str(rule["id"]),
                str(rule["title"]),
                rules_mod.CONDITION_LABELS[rule["when"]["condition"]],
                _detail(rule, revision=revision, csrf=csrf, editable=editable, draft=draft),
                _summary(rule),
            )
            for rule in rules
        ]
        enabled = sum(1 for rule in rules if rule["enabled"])
        bar = f'<div class="rules-bar"><p>{enabled} of {count} rule{"" if count == 1 else "s"} on</p></div>'
        body = (
            top
            + bar
            + (_add_panel(revision=revision, csrf=csrf, draft=draft) if editable else "")
            + explorer.render(entries, label="rules", empty="No rules match")
            + _agent_summary(revision, count)
        )
    return deck._document(
        f'<main class="deck-shell admin-page rules-page">{body}</main>',
        nonce=nonce,
        now=now,
        title="PR rules",
        active="pr-rules",
        view=None,
        refresh=False,
        extra_css=explorer.CSS + CSS,
        script=explorer.SCRIPT + SCRIPT,
    )
