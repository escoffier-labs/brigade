"""Offline owner-boundary regressions for passive configuration diagnosis."""

import builtins
import json
import socket
import subprocess
from dataclasses import replace

import pytest

from brigade.claude_actions_diagnostics import (
    Bounds,
    Definition,
    Inventory,
    Observation,
    Provenance,
    Scope,
    diagnose,
)
from brigade.claude_actions_evidence import ConfirmedWorkflow, normalize_run

REPO = "example/project"
SHA = "a" * 40
OTHER = "b" * 40
TIME = "2026-10-06T12:00:00Z"
PATH = ".github/workflows/automation.yml"


def provenance(path=PATH, revision=SHA, repository=REPO, authority="caller-normalized-definition"):
    scope = Scope(repository, path, revision)
    return Provenance(authority, scope, f"https://github.com/{repository}/blob/{revision}/{path}", TIME)


def definition(uses="anthropics/claude-code-action@v1", inputs=None, **kwargs):
    body = {
        "name": "renamed automation",
        "on": {"issue_comment": {"types": ["created"]}},
        "permissions": {"contents": "read", "id-token": "write"},
        "jobs": {"arbitrary": {"steps": [{"uses": uses, "with": inputs or {}}]}},
    }
    return Definition(provenance(), body, **kwargs)


def report(definitions=(), *, complete=False, observations=(), runs=(), **kwargs):
    inventory = Inventory(provenance(path=".github/workflows"), definitions, complete)
    return diagnose(inventory, observations=observations, runs=runs, **kwargs)


def facts(result, facet):
    return [fact for fact in result["facts"] if fact["facet"] == facet]


def test_app_only_report_never_implies_configuration_auth_or_review():
    app = Observation("app-installation", "operator-reported", "installed", provenance(authority="operator-report"))
    result = report(observations=[app])
    assert facts(result, "app-installation")[0]["state"] == "operator-reported"
    for facet in ("app-access", "workflow-discovery", "execution", "managed-review"):
        assert facts(result, facet)[0]["state"] == "unknown"
    assert facts(result, "cloud-lifecycle")[0]["state"] == "unsupported"
    assert json.loads(json.dumps(result)) == result
    for fact in result["facts"]:
        assert set(fact["provenance"]) == {"authority", "scope", "source_url", "observed_at"}


@pytest.mark.parametrize("ref", ["v1", "v1.2.3", "c" * 40])
def test_actual_action_recognized_independently_of_names_and_auth_is_missing(ref):
    result = report([definition(f"anthropics/claude-code-action@{ref}")], complete=True)
    assert facts(result, "workflow-discovery")[0]["state"] == "confirmed"
    assert facts(result, "auth-declaration")[0]["state"] == "absent-in-complete-scope"
    assert facts(result, "auth-readiness")[0]["state"] == "unknown"
    assert facts(result, "workflow-triggers")[0]["value"] == ["issue_comment"]
    assert facts(result, "workflow-permissions")[0]["value"]["id-token"] == "write"


@pytest.mark.parametrize(
    ("inputs", "mode"),
    [
        ({"anthropic_api_key": "${{ secrets.API_KEY }}"}, "api-key"),
        ({"claude_code_oauth_token": "${{ secrets.OAUTH_TOKEN }}"}, "subscription-oauth"),
        ({"anthropic_federation_rule_id": "fdrl_example", "anthropic_organization_id": "org_example"}, "federation"),
        ({"use_bedrock": "true"}, "bedrock"),
        ({"use_vertex": True}, "vertex"),
        ({"use_foundry": "true"}, "foundry"),
    ],
)
def test_auth_declarations_do_not_prove_secret_availability_or_readiness(inputs, mode):
    result = report([definition(inputs=inputs)])
    declared = facts(result, "auth-declaration")[0]
    assert declared["state"] == "confirmed"
    assert declared["value"]["modes"] == [mode]
    assert facts(result, "secret-availability")[0]["state"] == "unknown"
    assert facts(result, "auth-readiness")[0]["state"] == "unknown"
    assert "fdrl_example" not in json.dumps(result)


def test_literal_credentials_redacted_and_no_io_or_mutations(monkeypatch):
    item = definition(inputs={"anthropic_api_key": "example-private-value", "prompt": "example-private-prompt"})

    def forbidden(*args, **kwargs):
        raise AssertionError("passive diagnosis attempted IO or an external action")

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(subprocess, "run", forbidden)
        guard.setattr(subprocess, "Popen", forbidden)
        result = report([item])
    assert "example-private" not in json.dumps(result)
    assert facts(result, "auth-declaration")[0]["value"]["references"] == {"anthropic_api_key": "literal-redacted"}


@pytest.mark.parametrize("body", ["jobs: &anchor {}", {"jobs": "${{ inputs.jobs }}"}, None])
def test_unsupported_or_inaccessible_definition_cannot_establish_absence(body):
    result = report([Definition(provenance(), body)], complete=True)
    assert facts(result, "workflow-discovery")[0]["state"] == "unknown"


@pytest.mark.parametrize("uses", ["anthropics/claude-code-action@main", "${{ inputs.action }}"])
def test_dynamic_or_moving_action_refs_are_uncertain(uses):
    result = report([definition(uses)], complete=True)
    assert facts(result, "workflow-discovery")[0]["state"] == "unknown"


def test_false_positive_names_and_explicit_inventory_completeness():
    item = definition("actions/checkout@v4")
    item.body["name"] = "Claude review"
    assert facts(report([item]), "workflow-discovery")[0]["state"] == "unknown"
    assert facts(report([item], complete=True), "workflow-discovery")[0]["state"] == "absent-in-complete-scope"
    assert facts(report(complete=True), "workflow-discovery")[0]["state"] == "absent-in-complete-scope"


@pytest.mark.parametrize(("status", "state"), [(403, "access-denied"), (404, "unknown"), (429, "unknown")])
def test_access_statuses_and_disabled_metadata_remain_separate(status, state):
    result = report([Definition(provenance(), None, http_status=status)], complete=True)
    assert facts(result, "definition-access")[0]["state"] == state
    assert facts(result, "workflow-discovery")[0]["state"] != "absent-in-complete-scope"
    metadata = Observation(
        "workflow-enabled", "confirmed", "disabled", provenance(authority="supported-authorized-metadata")
    )
    result = report([definition()], observations=[metadata])
    assert facts(result, "workflow-enabled")[0]["value"] == "disabled"
    assert facts(result, "workflow-discovery")[0]["state"] == "confirmed"


@pytest.mark.parametrize("kind", ["workflow", "composite"])
def test_exact_supplied_reusable_and_composite_definitions(kind):
    target_path = ".github/workflows/shared.yml" if kind == "workflow" else "action.yml"
    ref = f"example/shared/{target_path}@{OTHER}" if kind == "workflow" else f"example/shared@{OTHER}"
    root = definition()
    root.body["jobs"] = {"delegate": {"uses": ref}} if kind == "workflow" else {"delegate": {"steps": [{"uses": ref}]}}
    child_body = definition(inputs={"claude_code_oauth_token": "${{ inputs.token }}"}).body
    if kind == "composite":
        child_body = {"runs": {"using": "composite", "steps": child_body["jobs"]["arbitrary"]["steps"]}}
    child = Definition(provenance(target_path, OTHER, "example/shared"), child_body, kind=kind)
    result = report([root, child], complete=True)
    assert facts(result, "workflow-discovery")[0]["state"] == "confirmed"
    auth = facts(result, "auth-declaration")[0]
    assert auth["provenance"]["scope"]["revision"] == OTHER
    assert auth["value"]["references"]["claude_code_oauth_token"] == "input-reference"
    assert facts(report([root], complete=True), "workflow-discovery")[0]["state"] == "unknown"
    wrong_revision = replace(child, provenance=provenance(target_path, SHA, "example/shared"))
    assert facts(report([root, wrong_revision], complete=True), "workflow-discovery")[0]["state"] == "unknown"


def test_cycles_depth_and_file_truncation_cannot_establish_absence():
    root = definition()
    root.body["jobs"] = {"cycle": {"uses": f"./{PATH}"}}
    result = report([root], complete=True)
    assert result["partial"] is True
    assert facts(result, "workflow-discovery")[0]["state"] == "unknown"
    for bounds in (Bounds(files=1), Bounds(depth=0), Bounds(nodes=1)):
        result = report(
            [definition(), replace(definition(), provenance=provenance(".github/workflows/other.yml"))],
            complete=True,
            bounds=bounds,
        )
        assert result["partial"] is True
    result = report([definition()], default_revision=OTHER)
    assert facts(result, "revision-relation")[0]["value"] == "non-default-revision"
    assert facts(result, "default-configuration")[0]["state"] == "unknown"


@pytest.mark.parametrize("path", ["shared.yml", ".github/workflows/nested/shared.yml", ".github/workflows/shared.txt"])
@pytest.mark.parametrize("external", [False, True])
def test_reusable_workflow_references_require_direct_workflows_yaml_path(path, external):
    root = definition()
    uses = f"example/shared/{path}@{OTHER}" if external else f"./{path}"
    root.body["jobs"] = {"delegate": {"uses": uses}}
    child = Definition(
        provenance(path, OTHER if external else SHA, "example/shared" if external else REPO), definition().body
    )
    result = report([root, child], complete=True)
    assert facts(result, "workflow-discovery")[0]["state"] == "unknown"
    assert result["partial"] is True


@pytest.mark.parametrize("organization", ["${{ secrets.ORG_A || secrets.ORG_B }}", {}, [], True, 12])
def test_federation_organization_requires_supported_literal_or_simple_reference(organization):
    result = report(
        [definition(inputs={"anthropic_federation_rule_id": "fdrl_example", "anthropic_organization_id": organization})]
    )
    assert facts(result, "auth-declaration")[0]["state"] == "unknown"


@pytest.mark.parametrize("status", [[], {}, True, "200", 99, 600])
def test_definition_rejects_malformed_http_status_before_diagnosis(status):
    with pytest.raises(ValueError, match="HTTP status"):
        Definition(provenance(), {}, http_status=status)


def test_confirmed_execution_and_independent_review_do_not_promote_each_other():
    run = normalize_run(
        {
            "repository": {"full_name": REPO},
            "workflow_id": 1,
            "id": 7,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
        },
        workflow=ConfirmedWorkflow(REPO, 1, SHA),
        observed_at=TIME,
    )
    result = report([definition()], runs=[run])
    assert facts(result, "execution")[0]["value"]["conclusion"] == "success"
    assert facts(result, "managed-review")[0]["state"] == "unknown"
    review = Observation(
        "managed-review", "confirmed", "enabled", provenance(authority="supported-authorized-metadata")
    )
    result = report(observations=[review], runs=[run])
    assert facts(result, "workflow-discovery")[0]["state"] == "unknown"
    assert facts(result, "managed-review")[0]["value"] == "enabled"
    assert facts(result, "cloud-lifecycle")[0]["state"] == "unsupported"
    assert result["merge_readiness"] == "unknown"
    assert result["findings_state"] == "unknown"


def test_provenance_and_observation_authority_reject_scope_and_value_leaks():
    with pytest.raises(ValueError, match="provenance"):
        replace(provenance(), source_url=f"https://github.com/{REPO}?token=example-private-value")
    with pytest.raises(ValueError, match="observation"):
        Observation("app-access", "confirmed", "accessible", provenance(authority="operator-report"))
    with pytest.raises(ValueError, match="observation"):
        Observation(
            "managed-review",
            "confirmed",
            "example-private-value",
            provenance(authority="supported-authorized-metadata"),
        )
    with pytest.raises(ValueError, match="complete"):
        Observation(
            "app-access", "absent-in-complete-scope", None, provenance(authority="supported-authorized-metadata")
        )
    unrelated = Observation(
        "managed-review",
        "confirmed",
        "enabled",
        provenance(repository="example/unrelated", authority="supported-authorized-metadata"),
    )
    assert facts(report(observations=[unrelated]), "managed-review")[0]["state"] == "unknown"
