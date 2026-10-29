"""Policy list/detail composition over the existing scoped form renderers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import re

from . import fleet_admin_explorer as explorer, fleet_command_deck
from . import fleet_policy_page as page
from . import fleet_policy_page_sessions as sessions
from . import fleet_investigation as investigation


def render(
    view: page.PolicyView,
    *,
    nonce: str,
    now: datetime,
    csrf: str,
    editable: bool,
    banner: str | None = None,
    error: str | None = None,
    plan: page.Plan | None = None,
    draft: page.Submission | None = None,
    review: str | None = None,
    query: str = "",
) -> str:
    """The policy page. ``draft`` re-renders what the operator sent on a refusal.

    ``review`` is the server-minted token for the previewed document; it is
    what turns the proposed-change panel into a confirm-save control.
    """
    error_prompt = (
        investigation.render(
            "Policy validation",
            route="/deck/policy",
            observation=now,
            facts={"Reported error": error, "Revision": view.revision},
            limits="Rendered error only; submitted document and credentials omitted.",
        )
        if error
        else ""
    )
    parts = ['<main class="deck-shell admin-page policy-page">']
    if banner:
        parts.append(f'<p class="banner">{page._esc(banner)}</p>')
    if error:
        parts.append(f'<p class="banner banner--error">{page._esc(error)}</p>' + error_prompt)
    if not editable:
        parts.append(
            '<p class="policy-access">Access: read-only. You can inspect saved settings; editing requires an authorized admin request.</p>'
        )
    parts.append(page._activation_panel(view))
    parts.append(page._plan_panel(plan, activation=view.activation, draft=draft, review=review if editable else None))
    entries = []
    draft_scope = draft.scope if editable and draft is not None else None
    opened = " open" if draft_scope is not None else ""
    defaults = page._assignment_summary(view)
    if editable:
        defaults += page._limits_summary(view)
        defaults += f'<details id="policy-advanced"{opened}><summary>Advanced details</summary>'
        defaults += explorer.agent("<h3>Document identity and inventory</h3>" + page._identity_panel(view))
        defaults += (
            "<details"
            + (" open" if draft_scope == "defaults" else "")
            + "><summary>Edit saved fleet defaults</summary>"
            + page._defaults_form(view, editable=True, csrf=csrf, draft=draft)
            + "</details></details>"
        )
    else:
        defaults += page._inventory_note(view.inventory)
    entries.append(explorer.Entry("defaults", "Fleet defaults", "Saved assignments", defaults))

    def expanded(content: str) -> str:
        return re.sub(r"(<details\b(?![^>]*\bopen\b)[^>]*)(>)", r"\1 open\2", content, count=1)

    for row in view.seats:
        content = explorer.hero(row.name, row.model)
        content += explorer.facts({"Provider": row.provider, "Model": row.model, "Reasoning": row.effort})
        if editable:
            content += (
                "<details open><summary>Seat matrix and model settings</summary>"
                + expanded(page._seat_form(view, row, csrf=csrf, draft=draft))
                + "</details>"
            )
        entries.append(explorer.Entry("seat:" + row.name, row.name, row.model, content))
    if editable:
        for consumer in view.consumers:
            content = page._consumer_section(
                replace(view, consumers=(consumer,)), editable=True, csrf=csrf, draft=draft
            )
            content = expanded(content)
            content = (
                "<details open><summary>Edit consumer integrations and overrides</summary>" + content + "</details>"
            )
            entries.append(explorer.Entry("consumer:" + consumer.name, consumer.name, "Consumer", content))
        for repository in view.repositories:
            content = page._repository_section(
                replace(view, repositories=(repository,)), editable=True, csrf=csrf, draft=draft
            )
            content = expanded(content)
            content = "<details open><summary>Edit repository settings and overrides</summary>" + content + "</details>"
            entries.append(
                explorer.Entry("repository:" + repository.identity, repository.identity, "Repository", content)
            )
        entries.append(explorer.Entry("seats", "Seat matrix", "Saved model settings", page._seat_table(view)))
        entries.append(explorer.Entry("sessions", "Sessions", "Acknowledgements", sessions.sessions_section(view)))
        entries.append(
            explorer.Entry(
                "rollback",
                "Audit history",
                "Revisions and rollback",
                "<details open><summary>Session acknowledgements, audit history and rollback</summary>"
                + sessions.history_section(view, editable=True, csrf=csrf)
                + "</details>",
            )
        )
    if draft_scope is not None and draft is not None:
        from urllib.parse import urlencode

        key = draft_scope + (":" + draft.target if draft_scope in {"seat", "consumer", "repository"} else "")
        params = explorer.query_params(query)
        query = urlencode({**params, "selected": [key]}, doseq=True)
    parts.append(explorer.render(entries, label="policy", query=query, searchable=editable))
    parts.append("</main>")
    return fleet_command_deck._document(
        "\n".join(parts),
        nonce=nonce,
        now=now,
        title="Model assignments" if editable else "Saved assignments",
        refresh=False,
        active="policy",
        extra_css=page._POLICY_CSS + explorer.CSS,
        script=explorer.SCRIPT if editable else "",
    )
