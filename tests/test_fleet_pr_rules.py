"""PR rules: schema, plain-language rendering, page, edit flow, and the agent CLI."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import StringIO
from urllib.parse import urlencode

import pytest

from brigade import cli, fleet_hub, fleet_hub_policy, fleet_policy
from brigade import fleet_policy_pr_rules as rules_mod
from brigade import fleet_pr_rules_page as page
from tests.fixtures.fleet_pr_rules import sample_document, sample_rules
from tests.test_fleet_work_page import TOKEN, _bearer, _cookie, _hub, _request

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
HOSTILE = "<img src=x onerror=alert(1)>\"'&"
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"}


def _document(rules):
    document = fleet_policy.empty_document()
    document["pr_rules"] = rules
    return document


def _rule(**changes):
    rule = {"id": "one", "title": "One", "when": {"condition": "always"}, "then": ["skip_ci"]}
    rule.update(changes)
    return rule


def _parse(rules):
    return fleet_policy.parse_document(_document(rules))


# --- schema -------------------------------------------------------------------


def test_sample_rules_are_accepted_normalized_and_idempotent():
    parsed = _parse(sample_rules())
    first = parsed["pr_rules"][0]
    assert first["when"] == {"condition": "docs_only", "paths": ["docs/**", "*.md"]}
    assert first["scope"] == {"repos": []} and first["enabled"] is True
    assert fleet_policy.parse_document(parsed) == parsed
    assert fleet_policy.document_digest(parsed) == fleet_policy.document_digest(_document(sample_rules()))


def test_an_empty_section_leaves_the_document_and_digest_unchanged():
    plain = fleet_policy.empty_document()
    assert "pr_rules" not in fleet_policy.parse_document(plain)
    assert "pr_rules" not in fleet_policy.parse_document(_document([]))
    assert fleet_policy.document_digest(plain) == fleet_policy.document_digest(_document([]))


@pytest.mark.parametrize(
    "rule, message",
    [
        (_rule(when={"condition": "weekend"}), "condition"),
        (_rule(when={}), "condition"),
        (_rule(then=["merge_it"]), "unknown action"),
        (_rule(then=["skip_ci:now"]), "no argument"),
        (_rule(then=["skip_label"]), "needs an argument"),
        (_rule(then=["use_reviewer:grok"]), "one of"),
        (_rule(then=["limit_reviewer:greptile:0_per_day"]), "limit_reviewer"),
        (_rule(then=["limit_reviewer:greptile:101_per_day"]), "limit_reviewer"),
        (_rule(then=["limit_reviewer:greptile:3_per_month"]), "limit_reviewer"),
        (_rule(then=["note:" + "x" * 201]), "at most 200"),
        (_rule(then=["note:"]), "note must not be empty"),
        (_rule(then=["note:bad\nline"]), "control characters"),
        (_rule(then=[]), "non-empty"),
        (_rule(then=["skip_ci"] * 2), "repeat"),
        (_rule(then=[f"note:{n}" for n in range(11)]), "at most 10"),
        (_rule(then=["skip_label:a", "require_label:a"]), "both skips and requires"),
        (_rule(then=["skip_label:has space"]), "must match"),
        (_rule(when={"condition": "always", "reviewer": "codex"}), "unknown"),
        (_rule(when={"condition": "docs_only", "paths": []}), "non-empty"),
        (_rule(when={"condition": "docs_only", "paths": ["../etc;rm"]}), "must match"),
        (_rule(when={"condition": "review_quota_exhausted"}), "one of"),
        (_rule(when={"condition": "review_quota_exhausted", "reviewer": "other"}), "one of"),
        (_rule(when={"condition": "label_present", "label": ""}), "must match"),
        (_rule(when={"condition": "repository", "repos": []}), "at least one"),
        (_rule(when={"condition": "repository", "repos": ["not-a-repo"]}), "owner/name"),
        (_rule(scope={"repos": ["a/b", "a/b"]}), "repeats"),
        (_rule(scope={"repos": ["a/b"], "extra": 1}), "unknown"),
        (_rule(title=""), "must not be empty"),
        (_rule(title="x" * 121), "at most 120"),
        (_rule(title=5), "string"),
        (_rule(why="x" * 301), "at most 300"),
        (_rule(enabled="yes"), "boolean"),
        (_rule(surprise=True), "unknown"),
        (_rule(id="Not A Slug"), "slug"),
        ("not-an-object", "JSON object"),
    ],
)
def test_invalid_rules_are_rejected_not_ignored(rule, message):
    with pytest.raises(fleet_policy.FleetPolicyError, match=message):
        _parse([rule])


def test_rule_count_and_id_uniqueness_are_enforced():
    _parse([_rule(id=f"rule-{n}") for n in range(50)])
    with pytest.raises(fleet_policy.FleetPolicyError, match="at most 50"):
        _parse([_rule(id=f"rule-{n}") for n in range(51)])
    with pytest.raises(fleet_policy.FleetPolicyError, match="repeats rule id"):
        _parse([_rule(), _rule()])
    with pytest.raises(fleet_policy.FleetPolicyError, match="must be a list"):
        fleet_policy.parse_document({**fleet_policy.empty_document(), "pr_rules": {"one": _rule()}})


def test_bounds_at_the_limit_are_accepted():
    rule = _rule(
        title="t" * 120,
        why="w" * 300,
        then=["note:" + "n" * 200, "limit_reviewer:codex:100_per_week"],
        when={"condition": "docs_only", "paths": [f"d{n}/**" for n in range(20)]},
        scope={"repos": [f"o/r{n}" for n in range(20)]},
    )
    assert _parse([rule])["pr_rules"][0]["title"] == "t" * 120


def test_diff_names_the_rule_that_changed():
    before = _document(sample_rules())
    after = copy.deepcopy(before)
    after["pr_rules"][2]["enabled"] = False
    diff = fleet_policy.diff_documents(before, after)
    assert [item["path"] for item in diff["changed"]] == ["pr_rules.rate-limited-is-not-approval.enabled"]
    assert diff["added"] == [] and diff["removed"] == []


def test_save_and_rollback_round_trip(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        base = fleet_hub_policy.current_policy(conn)
        saved = fleet_hub_policy.save_policy(
            conn, sample_document(), expected_version=base["revision"], actor="test", reason="add rules"
        )
        current = fleet_hub_policy.current_policy(conn)
        assert [rule["id"] for rule in current["document"]["pr_rules"]][0] == "docs-only-skip-review"
        cleared = fleet_hub_policy.save_policy(
            conn, fleet_policy.empty_document(), expected_version=saved["revision"], actor="test", reason="clear"
        )
        assert "pr_rules" not in fleet_hub_policy.current_policy(conn)["document"]
        fleet_hub_policy.rollback_policy(
            conn, to_revision=saved["revision"], expected_version=cleared["revision"], actor="test", reason="back"
        )
        restored = fleet_hub_policy.current_policy(conn)["document"]["pr_rules"]
        assert restored == current["document"]["pr_rules"]
    finally:
        conn.close()


# --- plain language and agent output ------------------------------------------


def test_sentences_are_generated_from_structured_fields():
    rules = _parse(sample_rules())["pr_rules"]
    assert rules_mod.sentence(rules[0]) == (
        "When a PR only changes docs (docs/**, *.md): do not add the coderabbit-review label. Skip CI."
    )
    assert rules_mod.sentence(rules[1]) == (
        "When CodeRabbit is out of reviews: use Greptile for review. "
        "Use Greptile at most 3 times per day. Never spend paid Greptile credits."
    )
    assert rules_mod.sentence(rules[2]) == ("For every PR: Check the reviewed commit before relying on an approval.")
    other = _parse(
        [
            _rule(
                when={"condition": "repository", "repos": ["a/b", "c/d"]},
                then=["require_label:needs-review", "limit_reviewer:codex:1_per_week"],
            ),
            _rule(id="two", when={"condition": "label_present", "label": "hotfix"}),
        ]
    )["pr_rules"]
    assert rules_mod.sentence(other[0]).startswith("When a PR is in a/b, c/d: add the needs-review label. Use Codex")
    assert "1 time per week" in rules_mod.sentence(other[0])
    assert rules_mod.sentence(other[1]) == "When a PR has the label hotfix: skip CI."


def test_scope_text_and_repository_filtering():
    rules = _parse(
        sample_rules()
        + [
            _rule(id="only-lib", when={"condition": "repository", "repos": ["sample-org/sample-lib"]}, enabled=True),
            _rule(id="off", enabled=False),
        ]
    )["pr_rules"]
    assert [rules_mod.scope_text(rule) for rule in rules[:2]] == ["All repos", "2 repos"]
    ids = lambda repo: [rule["id"] for rule in rules_mod.rules_for(rules, repo)]  # noqa: E731
    assert ids(None) == [
        "docs-only-skip-review",
        "coderabbit-dry-use-greptile",
        "rate-limited-is-not-approval",
        "only-lib",
    ]
    assert ids("sample-org/sample-app") == [
        "docs-only-skip-review",
        "coderabbit-dry-use-greptile",
        "rate-limited-is-not-approval",
    ]
    assert "only-lib" in ids("Sample-Org/Sample-Lib")
    assert ids("sample-org/other") == ["docs-only-skip-review", "rate-limited-is-not-approval"]


# --- CLI ----------------------------------------------------------------------


def _run(monkeypatch, argv):
    out, err = StringIO(), StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    try:
        code = cli.main(["fleet", "policy", *argv])
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue()


def _serve_policy(monkeypatch, document):
    monkeypatch.setattr(
        "brigade.fleet_client_policy.show_policy",
        lambda **kwargs: {
            "schema": fleet_policy.POLICY_SCHEMA,
            "version": 7,
            "digest": "sha256:x",
            "document": document,
        },
    )


def test_cli_prints_a_compact_stable_form_for_one_repository(monkeypatch):
    _serve_policy(monkeypatch, _parse(sample_rules()))
    code, out = _run(monkeypatch, ["pr-rules", "--repo", "sample-org/other"])
    assert code == 0
    assert out.splitlines() == [
        "pr-rules revision=7 repo=sample-org/other rules=2",
        "docs-only-skip-review",
        "  when docs_only(docs/**,*.md)",
        "  then skip_label:coderabbit-review",
        "  then skip_ci",
        "rate-limited-is-not-approval",
        "  when always",
        "  then note:Check the reviewed commit before relying on an approval.",
    ]
    _, scoped = _run(monkeypatch, ["pr-rules", "--repo", "sample-org/sample-app"])
    assert "coderabbit-dry-use-greptile" in scoped and "rules=3" in scoped.splitlines()[0]
    assert "  when review_quota_exhausted(coderabbit)" in scoped


def test_cli_json_is_stable_and_skips_disabled_rules(monkeypatch):
    rules = sample_rules()
    rules[0]["enabled"] = False
    _serve_policy(monkeypatch, _parse(rules))
    code, out = _run(monkeypatch, ["pr-rules", "--json"])
    payload = json.loads(out)
    assert code == 0 and list(payload) == ["repo", "revision", "rules", "schema"]
    assert payload["schema"] == "brigade.fleet_pr_rules.v1" and payload["repo"] is None and payload["revision"] == 7
    assert [rule["id"] for rule in payload["rules"]] == ["coderabbit-dry-use-greptile", "rate-limited-is-not-approval"]
    assert list(payload["rules"][0]) == ["enabled", "id", "scope", "then", "title", "when", "why"]
    assert _run(monkeypatch, ["pr-rules", "--json"])[1] == out


def test_cli_reports_no_rules_and_rejects_a_bad_repository(monkeypatch):
    _serve_policy(monkeypatch, fleet_policy.empty_document())
    code, out = _run(monkeypatch, ["pr-rules", "--repo", "sample-org/sample-app"])
    assert code == 0 and out.splitlines() == [
        "pr-rules revision=7 repo=sample-org/sample-app rules=0",
        "no enabled PR rules apply",
    ]
    code, out = _run(monkeypatch, ["pr-rules", "--repo", "not a repo"])
    assert code == 2 and json.loads(out)["error"]["code"] == "invalid-request"


def test_policy_show_json_carries_pr_rules():
    from brigade import fleet_hub_policy_api

    assert "pr_rules" in _parse(sample_rules())
    # the JSON the hub serves is the stored document, which includes the section
    assert fleet_hub_policy_api.read_policy.__name__ == "read_policy"


# --- page ---------------------------------------------------------------------


def _render(rules, *, editable=True, **extra):
    parsed = _parse(rules)["pr_rules"] if rules else []
    return page.render(
        parsed, revision=4, nonce="n", now=NOW, csrf="csrf-token" if editable else "", editable=editable, **extra
    )


def test_page_lists_rules_with_scope_and_off_pills_and_a_plain_sentence():
    rules = sample_rules()
    rules[2]["enabled"] = False
    html = _render(rules)
    assert "data-explorer-count>3<" in html
    assert ">All repos<" in html and ">2 repos<" in html and ">Off<" in html
    assert "When a PR only changes docs (docs/**, *.md): do not add the coderabbit-review label. Skip CI." in html
    assert "Why: Documentation changes do not need" in html
    assert 'aria-current="page">PR rules</a>' in html
    assert "—" not in re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)


class _Audience(HTMLParser):
    """Collects text by whether it sits inside a data-audience="agent" element."""

    def __init__(self, markup):
        super().__init__()
        self.stack, self.agent, self.admin = [], [], []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag not in {"br", "input", "meta", "link", "hr", "img", "path", "circle"}:
            self.stack.append((tag, dict(attrs).get("data-audience") == "agent"))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        (self.agent if any(flag for _, flag in self.stack) else self.admin).append(data)


def test_page_puts_ids_json_revision_and_command_in_agent_blocks():
    parts = _Audience(_render(sample_rules()).split("<main", 1)[1])
    agent, admin = "".join(parts.agent), "".join(parts.admin)
    assert "docs-only-skip-review" in agent and '"condition": "docs_only"' in agent
    assert "brigade fleet policy pr-rules" in agent and "Policy revision" in agent
    for text in ("docs-only-skip-review", "brigade fleet policy pr-rules", '"condition"', "Policy revision"):
        assert text not in admin


def test_empty_state_and_add_button_for_admins_only():
    admin = _render([])
    assert "No PR rules yet." in admin and "Add rule" in admin and 'name="csrf"' in admin
    reader = _render([], editable=False)
    assert "No PR rules yet." in reader and "Add rule" not in reader and 'name="csrf"' not in reader
    assert page.READ_ONLY_NOTICE in reader


def test_read_only_page_has_no_forms_tokens_or_buttons():
    html = _render(sample_rules(), editable=False)
    body = html.split("<main", 1)[1]
    assert "<form" not in body and "csrf" not in body and "Edit rule" not in body and "Delete rule" not in body


def test_hostile_text_is_escaped_everywhere():
    rules = sample_rules()
    rules[0]["title"] = HOSTILE[:100]
    rules[0]["why"] = HOSTILE
    rules[2]["then"] = ["note:" + HOSTILE[:150]]
    html = _render(rules)
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_admin_forms_use_selects_not_raw_json_and_carry_the_revision():
    html = _render(sample_rules())
    assert 'name="field.condition"' in html and 'name="field.act_kind_0"' in html
    assert 'name="scope" value="pr_rules"' in html and 'name="expected_version" value="4"' in html
    assert 'name="target" value="@new"' in html
    assert "<textarea" not in html.split("<main", 1)[1]
    for label in ("Do not add a label", "Skip CI", "Never spend paid credits"):
        assert label in html


# --- edit flow over HTTP ------------------------------------------------------


def _post(hub, fields, headers=None):
    return _request(
        hub,
        "/deck/pr-rules",
        method="POST",
        headers={**(_bearer() if headers is None else headers), **FORM},
        body=urlencode(fields),
    )


def _form(hub, **fields):
    from brigade import fleet_policy_page

    return {
        "scope": "pr_rules",
        "expected_version": str(fields.pop("expected_version", 1)),
        "csrf": fleet_policy_page.csrf_value(TOKEN),
        **fields,
    }


def _revision(db_path):
    conn = fleet_hub.open_db(db_path)
    try:
        return fleet_hub_policy.current_policy(conn)
    finally:
        conn.close()


def _add_fields(hub, **extra):
    return _form(
        hub,
        target="@new",
        action="preview",
        **{
            "field.id": "sample-rule",
            "field.title": "Sample rule",
            "field.condition": "label_present",
            "field.when_label": "hotfix",
            "field.act_kind_0": "skip_ci",
            "field.act_kind_1": "limit_reviewer",
            "field.act_reviewer_1": "codex",
            "field.act_count_1": "2",
            "field.act_period_1": "week",
            "field.why": "Because.",
            "field.enabled": "1",
            **extra,
        },
    )


def test_admin_adds_edits_toggles_and_deletes_through_preview_and_confirm(tmp_path):
    with _hub(tmp_path) as (hub, db):
        revision = _revision(db)["revision"]
        status, _, body = _request(hub, "/deck/pr-rules", headers=_bearer())
        assert status == 200 and "No PR rules yet." in body and 'name="csrf"' in body
        # preview writes nothing and offers a confirm form with a review token
        status, _, body = _post(hub, _add_fields(hub, expected_version=revision))
        assert status == 200 and "Preview only" in body and "Sample rule" in body
        assert _revision(db)["revision"] == revision
        token = re.search(r'name="review" value="([^"]+)"', body).group(1)
        assert 'action="/deck/pr-rules" class="roster-form confirm-save"' in body
        # a save without the token is refused
        status, _, refused = _post(hub, {**_add_fields(hub, expected_version=revision), "action": "save"})
        assert status == 422 and "not confirmed" in refused and _revision(db)["revision"] == revision
        saved = {**_add_fields(hub, expected_version=revision), "action": "save", "review": token}
        status, headers, _ = _post(hub, saved)
        assert status == 303 and headers["Location"] == f"/deck/pr-rules?saved={revision + 1}"
        current = _revision(db)
        assert current["document"]["pr_rules"][0]["then"] == ["skip_ci", "limit_reviewer:codex:2_per_week"]
        status, _, listing = _request(hub, headers["Location"], headers=_bearer())
        assert (
            "Saved as revision" in listing and "nothing is enforced on GitHub" in listing and "Sample rule" in listing
        )
        # toggle off, then delete, each through preview and confirm
        for fields, check in (
            ({"field.toggle": "disable"}, lambda r: r[0]["enabled"] is False),
            ({"field.delete": "1"}, lambda r: r == []),
        ):
            version = _revision(db)["revision"]
            base = _form(hub, target="sample-rule", expected_version=version, **fields)
            _, _, preview = _post(hub, {**base, "action": "preview"})
            review = re.search(r'name="review" value="([^"]+)"', preview).group(1)
            status, _, _ = _post(hub, {**base, "action": "save", "review": review})
            assert status == 303
            assert check(_revision(db)["document"].get("pr_rules", []))


def test_invalid_edit_is_refused_with_a_message_and_keeps_the_draft(tmp_path):
    with _hub(tmp_path) as (hub, db):
        revision = _revision(db)["revision"]
        bad = _add_fields(
            hub, expected_version=revision, **{"field.act_kind_2": "merge_it", "field.title": HOSTILE[:60]}
        )
        status, _, body = _post(hub, bad)
        assert status == 422 and "unknown action" in body and "&lt;img" in body and "<img" not in body
        assert _revision(db)["revision"] == revision
        assert 'value="sample-rule"' in body  # the draft stays in the form


def test_a_rule_with_a_missing_parameter_is_refused_not_a_server_error(tmp_path):
    with _hub(tmp_path) as (hub, db):
        revision = _revision(db)["revision"]
        fields = _add_fields(hub, expected_version=revision, **{"field.condition": "review_quota_exhausted"})
        status, _, body = _post(hub, fields)
        assert status == 422 and "reviewer" in body and "Proposed change" not in body
        assert _revision(db)["revision"] == revision


def test_stale_revision_conflicts_and_duplicate_ids_are_refused(tmp_path):
    with _hub(tmp_path) as (hub, db):
        revision = _revision(db)["revision"]
        status, _, body = _post(hub, _add_fields(hub, expected_version=revision + 5))
        assert status == 409 and "changed underneath you" in body
        conn = fleet_hub.open_db(db)
        try:
            fleet_hub_policy.save_policy(
                conn, sample_document(), expected_version=revision, actor="test", reason="seed"
            )
        finally:
            conn.close()
        dup = _add_fields(hub, expected_version=revision + 1, **{"field.id": "docs-only-skip-review"})
        status, _, body = _post(hub, dup)
        assert status == 422 and "already exists" in body


def test_form_posts_need_the_admin_bearer_a_matching_csrf_and_the_right_scope(tmp_path):
    with _hub(tmp_path) as (hub, db):
        revision = _revision(db)["revision"]
        fields = _add_fields(hub, expected_version=revision)
        assert _post(hub, fields, headers={})[0] == 401
        status, _, body = _post(hub, fields, headers=_cookie(hub))
        assert status == 403 and "admin bearer" in body
        assert _post(hub, {**fields, "csrf": "wrong"})[0] == 403
        assert _post(hub, {k: v for k, v in fields.items() if k != "csrf"})[0] == 403
        assert _post(hub, {**fields, "scope": "seat"})[0] == 400
        cross = _request(
            hub,
            "/deck/pr-rules",
            method="POST",
            headers={**_bearer(), **FORM, "Sec-Fetch-Site": "cross-site"},
            body=urlencode(fields),
        )
        assert cross[0] == 403
        assert _revision(db)["revision"] == revision


def test_browser_identity_sees_the_list_read_only(tmp_path):
    with _hub(tmp_path) as (hub, db):
        conn = fleet_hub.open_db(db)
        try:
            fleet_hub_policy.save_policy(
                conn, sample_document(), expected_version=_revision(db)["revision"], actor="test", reason="seed"
            )
        finally:
            conn.close()
        assert _request(hub, "/deck/pr-rules")[0] == 401
        status, _, body = _request(hub, "/deck/pr-rules", headers=_cookie(hub))
        assert status == 200 and page.READ_ONLY_NOTICE in body
        assert "Doc-only PRs skip CodeRabbit and CI" in body
        assert "csrf" not in body.split("<main", 1)[1] and "<form" not in body.split("<main", 1)[1]


def test_policy_page_post_cannot_be_used_to_skip_the_pr_rules_form(tmp_path):
    """The shared policy form path accepts the new scope but still needs preview and a review token."""
    with _hub(tmp_path) as (hub, db):
        revision = _revision(db)["revision"]
        fields = _add_fields(hub, expected_version=revision)
        status, _, _ = _request(
            hub,
            "/deck/policy",
            method="POST",
            headers={**_bearer(), **FORM},
            body=urlencode({**fields, "action": "save"}),
        )
        assert status == 422 and _revision(db)["revision"] == revision
