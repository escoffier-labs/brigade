"""Every Fleet Hub form field says whether it is required, from one declaration.

The renderer, the HTML ``required`` attribute and the request validator all read the
per-form declaration, so these tests render each form and check that what the page shows
is exactly what the declaration says, then check the validator refuses what it declares.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest

from brigade import fleet_action_page, fleet_dashboard, fleet_form_fields as ff, fleet_hub_actions
from brigade import fleet_hub_roster_page, fleet_policy, fleet_policy_form as forms, fleet_skills_review as review
from brigade import fleet_policy_page
from brigade.fleet_action_auth import ActionError
from tests.test_fleet_policy_page import _fresh_inventory, _render as render_policy, _seed, conn  # noqa: F401
from tests.test_fleet_pr_rules import _render as render_rules
from tests.test_fleet_roster_page import _hub, _page as roster_page, _seed as seed_roster
from tests.test_fleet_roster_page import _seed_policy
from tests.test_fleet_skills_review import _render as render_skills
from tests.test_fleet_skills_review import seeded, view_for
from tests.fixtures.fleet_pr_rules import sample_rules


class Control:
    def __init__(self, scope: str, name: str, required: bool, marker: str | None) -> None:
        self.scope, self.name, self.required, self.marker = scope, name, required, marker


class _Controls(HTMLParser):
    """Visible named controls with the requirement marker from their label or group."""

    def __init__(self) -> None:
        super().__init__()
        self.controls: list[Control] = []
        self._scope = ""
        self._pending: list[Control] = []
        self._in_label = False
        self._marker: list[str] | None = None
        self._label_marker = ""
        self._decision_markers: dict[str, str] | None = None
        self._decision_heading: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "form":
            self._scope = ""
            self._decision_markers = {} if "decision" in classes else None
        elif self._decision_markers is not None and tag in {"h3", "th"}:
            self._decision_heading = tag
        elif tag == "label":
            self._in_label, self._label_marker, self._pending = True, "", []
        elif tag == "span" and "field-req" in classes:
            self._marker = []
        elif tag in {"input", "select", "textarea"}:
            name = attributes.get("name") or ""
            if tag == "input" and attributes.get("type") == "hidden":
                if name == "scope":
                    self._scope = attributes.get("value") or ""
                return
            if tag == "input" and attributes.get("type") in {"submit", "button"}:
                return
            if name:
                marker = None
                if self._decision_markers is not None:
                    field = "ov" if name.startswith("ov:") else name
                    marker = self._decision_markers.get(field)
                control = Control(self._scope, name, "required" in attributes, marker)
                self.controls.append(control)
                if self._in_label:
                    self._pending.append(control)

    def handle_data(self, data):
        if self._marker is not None:
            self._marker.append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self._marker is not None:
            self._label_marker = "".join(self._marker)
            self._marker = None
            if self._decision_markers is not None and self._decision_heading is not None:
                field = "decision" if self._decision_heading == "h3" else "ov"
                self._decision_markers[field] = self._label_marker
        elif tag == self._decision_heading:
            self._decision_heading = None
        elif tag == "form":
            self._decision_markers = None
        elif tag == "label":
            for control in self._pending:
                control.marker = self._label_marker or control.marker
            self._in_label, self._pending = False, []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def controls(page: str) -> list[Control]:
    parser = _Controls()
    parser.feed(page)
    return parser.controls


def expected_marker(field: ff.Field) -> str:
    if field.is_required:
        return "Required"
    return f"Required {field.needed_when}" if field.needed_when else "Optional"


def field_name(name: str) -> str:
    """``field.act_kind_2`` and ``act_kind_2`` both name the ``act_kind`` declaration."""
    name = name.removeprefix("field.")
    head, _, tail = name.rpartition("_")
    return head if head and tail.isdigit() else name


def assert_matches(page: str, specs: dict[str, ff.Form]) -> set[str]:
    """Every visible control matches its form's declaration, marker and attribute. Returns names seen."""
    seen: set[str] = set()
    found = controls(page)
    assert found, "the page rendered no form controls"
    for control in found:
        spec = specs.get(control.scope)
        if spec is None:
            continue
        field = spec.get(field_name(control.name))
        assert field is not None, f"{control.scope} form control {control.name!r} has no declaration"
        assert control.marker == expected_marker(field), (control.scope, control.name, control.marker)
        assert control.required is field.is_required, (control.scope, control.name)
        seen.add(field_name(control.name))
    return seen


# --- the shared declaration ---------------------------------------------------------


def test_marker_text_covers_required_optional_and_conditional():
    assert ">Required<" in ff.marker(ff.required("a"))
    assert ">Optional<" in ff.marker(ff.optional("a"))
    assert ">Required for label actions<" in ff.marker(ff.conditional("a", "for label actions"))
    assert "field-req--required" in ff.marker(ff.required("a"))


def test_only_a_required_field_carries_the_html_attribute():
    assert ff.attr(ff.required("a")) == " required"
    assert ff.attr(ff.optional("a")) == ""
    assert ff.attr(ff.conditional("a", "for x")) == ""


def test_missing_reports_blank_absent_and_whitespace_required_fields_in_order():
    spec = ff.form(ff.required("a", "need a"), ff.optional("b"), ff.required("c", "need c"), ff.conditional("d", "x"))
    assert [field.error for field in ff.missing(spec, {})] == ["need a", "need c"]
    assert [field.name for field in ff.missing(spec, {"a": "  ", "c": "ok"})] == ["a"]
    assert ff.missing(spec, {"a": "x", "c": 5}) == []


def test_caption_escapes_the_label_text():
    assert "&lt;b&gt;" in ff.caption("<b>", ff.optional("a"))


# --- PR rules -----------------------------------------------------------------------


def test_pr_rule_forms_show_the_declared_marker_and_attribute_on_every_field():
    page = render_rules(sample_rules())
    seen = assert_matches(page, {"pr_rules": forms.PR_RULE_FORM})
    # The add form and every edit form render; the id field exists only on the add form.
    assert {"id", "title", "condition", "why", "when_label", "act_value", "reason", "enabled"} <= seen
    by_name = {c.name: c for c in controls(page)}
    assert by_name["field.id"].required and by_name["field.id"].marker == "Required"
    assert by_name["field.title"].required
    assert by_name["field.why"].marker == "Optional" and not by_name["field.why"].required
    label_marker = by_name["field.when_label"].marker
    assert label_marker is not None and "A PR has a label" in label_marker and not by_name["field.when_label"].required
    assert by_name["field.act_kind_0"].marker == "Required for the first action"
    assert not by_name["field.act_kind_0"].required


_SCRIPT_BLOCK = re.compile(r"<script\b.*?</script>", re.DOTALL | re.IGNORECASE)


def test_pr_rule_form_works_without_javascript_and_names_the_requirement_in_words():
    page = render_rules(sample_rules())
    static = _SCRIPT_BLOCK.sub("", page)
    assert "<script" not in static
    assert static.count("Required for a limit action") >= 3
    assert "Required for the first action" in static
    assert "Optional</span>" in static


def _rule_submission(**fields):
    body = {"scope": "pr_rules", "action": "preview", "expected_version": "1", "csrf": "t", "target": "@new"}
    body.update({f"field.{name}": value for name, value in fields.items()})
    return forms.parse_form(urlencode(body).encode("utf-8"))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"title": "T", "condition": "always", "act_kind_0": "skip_ci"}, "a PR rule needs an id"),
        ({"id": "r", "title": "  ", "condition": "always", "act_kind_0": "skip_ci"}, "a PR rule needs a title"),
        ({"id": "r", "title": "T", "act_kind_0": "skip_ci"}, "a PR rule needs a condition"),
    ],
)
def test_a_blank_required_pr_rule_field_is_refused_from_the_same_declaration(fields, message):
    with pytest.raises(forms.DocumentError, match=message):
        forms.build_document(fleet_policy.empty_document(), _rule_submission(**fields))


def test_an_optional_pr_rule_field_may_be_blank_and_a_toggle_needs_no_title():
    document = forms.build_document(
        fleet_policy.empty_document(),
        _rule_submission(id="r", title="T", condition="always", act_kind_0="skip_ci", why="", scope_repos=""),
    )
    assert document["pr_rules"][0]["why"] is None
    body = {"scope": "pr_rules", "action": "preview", "expected_version": "1", "csrf": "t", "target": "r"}
    toggled = forms.parse_form(urlencode({**body, "field.toggle": "disable"}).encode("utf-8"))
    assert forms.build_document(document, toggled)["pr_rules"][0]["enabled"] is False


# --- policy and roster --------------------------------------------------------------

POLICY_FORMS = {
    "defaults": forms.DEFAULTS_FORM,
    "seat": forms.SEAT_FORM,
    "consumer": forms.CONSUMER_FORM,
    "repository": forms.REPOSITORY_FORM,
    "rollback": forms.ROLLBACK_FORM,
}


def test_every_policy_form_field_shows_the_declared_marker_and_attribute(conn):  # noqa: F811
    _seed(conn)
    _seed(conn, reason="second revision so a rollback target exists")
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = render_policy(view, editable=True)
    found = {c.scope for c in controls(page)}
    assert set(POLICY_FORMS) <= found
    seen = assert_matches(page, POLICY_FORMS)
    assert {"provider", "model", "effort", "to_revision", "reason", "role_impl", "privacy"} <= seen
    seat = {c.name: c for c in controls(page) if c.scope == "seat"}
    assert seat["field.provider"].required and seat["field.model"].required
    assert seat["field.effort"].marker == "Optional" and not seat["field.effort"].required
    rollback = next(c for c in controls(page) if c.scope == "rollback" and c.name == "field.to_revision")
    assert rollback.required and rollback.marker == "Required"


def test_read_only_policy_shows_saved_defaults_without_form_markers(conn):  # noqa: F811
    _seed(conn)
    view = fleet_policy_page.load_view(conn, inventory=_fresh_inventory())
    page = render_policy(view, editable=False)

    assert "Fleet defaults" in page and "seat-alpha" in page
    assert 'name="scope" value="defaults"' not in page
    assert "field-req--" not in re.sub(r"<style\b[^>]*>.*?</style>", "", page, flags=re.S)


def test_a_seat_without_a_provider_or_model_is_refused_with_the_declared_message():
    def submit(**fields):
        body = {"scope": "seat", "action": "preview", "expected_version": "1", "csrf": "t", "target": "seat-x"}
        return forms.parse_form(urlencode({**body, **{f"field.{k}": v for k, v in fields.items()}}).encode("utf-8"))

    empty = fleet_policy.empty_document()
    with pytest.raises(forms.DocumentError, match="a seat needs a provider"):
        forms.build_document(empty, submit(provider=" ", model="m"))
    with pytest.raises(forms.DocumentError, match="a seat needs an exact model id"):
        forms.build_document(empty, submit(provider="p", model=""))
    built = forms.build_document(empty, submit(provider="p", model="m", effort="", notes=""))
    assert built["seats"]["seat-x"]["provider"] == "p"


def test_a_blank_rollback_revision_is_refused_with_the_declared_message():
    with pytest.raises(forms.DocumentError, match="rollback needs an integer revision"):
        fleet_policy_page._rollback_target(
            forms.parse_form(
                urlencode(
                    {"scope": "rollback", "action": "preview", "expected_version": "1", "field.to_revision": ""}
                ).encode()
            )
        )


def test_the_rollback_declaration_decides_the_blank_revision_message(monkeypatch):
    monkeypatch.setattr(
        fleet_policy_page,
        "ROLLBACK_FORM",
        ff.form(ff.required("to_revision", "rollback needs a revision from the form")),
    )
    submission = forms.parse_form(
        urlencode(
            {"scope": "rollback", "action": "preview", "expected_version": "1", "field.to_revision": " "}
        ).encode()
    )
    with pytest.raises(forms.DocumentError, match="rollback needs a revision from the form"):
        fleet_policy_page._rollback_target(submission)


def test_every_roster_control_is_optional_and_the_authoritative_forms_use_the_policy_declarations(tmp_path):
    with _hub(tmp_path) as (hub, db):
        seed_roster(hub)
        legacy = controls(roster_page(db))
        assert legacy and all(c.marker == "Optional" and not c.required for c in legacy)
        assert {c.name.split(".")[0] for c in legacy} >= {"role", "notes", "seat", "cloud", "default"}
        _seed_policy(db)
        active = fleet_policy_page.Activation("active", "fixture")
        page = roster_page(db, activation=active)
        seen = assert_matches(page, {"defaults": forms.DEFAULTS_FORM, "consumer": forms.CONSUMER_FORM})
        assert {"role_impl", "role_admission_default"} <= seen
    assert set(fleet_hub_roster_page.ROSTER_FORM) == {"role", "seat", "cloud", "default", "notes"}
    assert not [f for f in fleet_hub_roster_page.ROSTER_FORM.values() if f.is_required]


# --- action, dashboard, skills ------------------------------------------------------


def _action_snapshot():
    return {
        "source": {"kind": "run", "node_id": "fixture-node", "run_id": "run-1"},
        "revision": "rev",
        "evidence": {"state": "failed", "evidence_at": "2000-01-01T00:00:00Z", "ttl_seconds": 60},
    }


def test_the_repair_form_marks_the_destination_and_prompt_required_and_the_rest_optional():
    target = {"family": "openclaw", "target_id": "repair", "label": "Fixture", "models": []}
    page = fleet_action_page.compose(_action_snapshot(), [target], nonce="n", csrf="c")
    by_name = {c.name: c for c in controls(page)}
    assert by_name["target_id"].required and by_name["target_id"].marker == "Required"
    for name in ("family", "model_id"):
        assert by_name[name].marker == "Optional" and not by_name[name].required
    assert by_name["prompt"].required
    assert (
        'for="repair-prompt"><span class="field-label">Repair prompt <span class="field-req field-req--required">'
        in page
    )


def test_a_blank_repair_prompt_is_refused_with_the_declared_code():
    raw = {
        "source": _action_snapshot()["source"],
        "observed_revision": "rev",
        "target_id": "repair",
        "model_id": "",
        "intent": "repair",
        "prompt": "   ",
        "client_id": "click_" + "a" * 32,
    }
    with pytest.raises(ActionError) as caught:
        fleet_hub_actions.submit(None, "grant", raw)  # type: ignore[arg-type]
    assert caught.value.reason == fleet_hub_actions.ACTION_FORM["prompt"].error == "invalid-prompt"


def test_dashboard_filters_do_not_show_requirement_markers():
    page = fleet_dashboard._controls(fleet_dashboard.DashboardQuery())
    found = controls(page)
    assert {c.name for c in found} == {"node", "repo", "seat", "state", "sort", "attention", "all"}
    assert all(c.marker is None and not c.required for c in found)


def test_skills_decision_controls_match_the_form_declaration(tmp_path):
    seen = set()
    for control in controls(render_skills(tmp_path)):
        if control.name == "decision":
            name = "decision"
        elif control.name.startswith("ov:"):
            name = "ov"
        else:
            continue
        field = review.DECISION_FORM[name]
        assert control.marker == expected_marker(field), control.name
        assert control.required is field.is_required, control.name
        seen.add(name)
    assert seen == set(review.DECISION_FORM)


def test_a_blank_skill_decision_is_refused_with_the_declared_message(tmp_path):
    db, _path, _tokens = seeded(tmp_path)
    with pytest.raises(review.DecisionError, match="choose a decision"):
        review.save_decision(db, view_for(db), "sample-lint", "", {})
