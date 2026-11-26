"""Editable fleet defaults form for the policy page."""

from __future__ import annotations

from . import fleet_policy_page as page
from .fleet_policy_form import DEFAULTS_FORM, POLICY_ROLES, Submission


def render(view: page.PolicyView, *, csrf: str, draft: Submission | None) -> str:
    """Render the defaults editor for the admin-only policy layout."""
    values = page._draft_for(draft, "defaults")
    roles = view.document["defaults"].get("roles", {})
    data = view.document["defaults"].get("data", {})
    execution = view.document["defaults"].get("execution", {})

    def role_control(role: str) -> str:
        stored = roles.get(role) or ""
        current = values.get(f"role_{role}", stored)
        label = page.ROLE_LABELS.get(role, role)
        seat_obj = next((s for s in view.seats if s.name == current), None)
        cost_txt = f" · cost {seat_obj.cost_class}" if seat_obj else ""
        hint = f"policy role key: {role}{cost_txt}"
        control = page._seat_options(
            f"role_{role}",
            current,
            view.seats,
            editable=True,
            stored=stored,
            retired_seats=view.retired_seats.get("", ()),
        )
        return page._labelled(label, control, hint=hint, form=DEFAULTS_FORM)

    role_cells = "".join(role_control(role) for role in POLICY_ROLES)
    training = values.get("data_allow_training", "yes" if data.get("allow_training") else "no")
    allow_free = values.get("data_allow_free", page._yes_no(data.get("allow_free")))
    body = (
        f'<div class="roster-grid">{role_cells}</div>'
        '<div class="roster-grid">'
        + page._labelled(
            "Allow contributor training",
            page._enum_select("data_allow_training", training, ("yes", "no"), editable=True, blank="(inherit)"),
            hint="policy key: data.allow_training",
            form=DEFAULTS_FORM,
        )
        + page._labelled(
            "Allow free models",
            page._enum_select("data_allow_free", allow_free, ("yes", "no"), editable=True, blank="(inherit)"),
            hint="policy key: data.allow_free",
            form=DEFAULTS_FORM,
        )
        + page._labelled(
            "Data retention",
            page._input("data_retention", values.get("data_retention", data.get("retention") or ""), editable=True),
            hint="policy key: data.retention",
            form=DEFAULTS_FORM,
        )
        + page._labelled(
            "Default machine",
            page._input(
                "execution_machine",
                values.get("execution_machine", execution.get("machine") or ""),
                editable=True,
            ),
            hint="policy key: execution.machine",
            form=DEFAULTS_FORM,
        )
        + page._labelled(
            "Maximum concurrent workers",
            page._input(
                "execution_concurrency",
                values.get(
                    "execution_concurrency",
                    execution.get("concurrency") if execution.get("concurrency") is not None else "",
                ),
                editable=True,
            ),
            hint="policy key: execution.concurrency",
            form=DEFAULTS_FORM,
        )
        + page._labelled(
            "Worker timeout (seconds)",
            page._input(
                "execution_timeout_seconds",
                values.get(
                    "execution_timeout_seconds",
                    execution.get("timeout_seconds") if execution.get("timeout_seconds") is not None else "",
                ),
                editable=True,
            ),
            hint="policy key: execution.timeout_seconds",
            form=DEFAULTS_FORM,
        )
        + "</div>"
        '<p class="hint">Free-model eligibility and contributor training are configured in the data section '
        "(allow_free and allow_training). A quota pool name is a label, not a price or cost classification. "
        "Quota pools and cost classes stay separate.</p>"
    )
    return (
        '<section class="panel" aria-labelledby="defaults"><header><h2 id="defaults">Fleet defaults and roles</h2>'
        '</header><form method="post" action="/deck/policy" class="roster-form">'
        + page._hidden("defaults", "", view.revision, csrf)
        + body
        + page._actions(page._reason_for(draft, "defaults"))
        + "</form></section>"
    )
