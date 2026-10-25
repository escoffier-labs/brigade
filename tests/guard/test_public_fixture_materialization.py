"""Public examples stay neutral while generated private paths remain guarded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import component_paths
from brigade.guard.engine import scan_text
from brigade.guard.policy import load_policy
from brigade.research.doctor import doctor_payload
from tests.test_research_doctor import doctor_roster

_CHECKOUT_PLACEHOLDER = "{{PRIVATE_CHECKOUT_PATH}}"
_REPO = Path(__file__).resolve().parents[2]


def _synthetic_home(*parts: str) -> str:
    return "/".join(("", "home", "example-user", *parts))


@pytest.mark.parametrize("policy_name", ["pr-draft", "public-repo"])
def test_materialized_draft_fixture_still_blocks_and_redacts_home_path(policy_name):
    template = (_REPO / "tests/guard/fixtures/real_world/pr_draft_leak.md").read_text()
    assert template.count(_CHECKOUT_PLACEHOLDER) == 1
    policy = load_policy(_REPO / f"src/brigade/guard/policies/{policy_name}.json")
    assert not scan_text(template, policy=policy).blocked

    private_path = _synthetic_home("work", "private-repo")
    materialized = template.replace(_CHECKOUT_PLACEHOLDER, private_path)
    result = scan_text(materialized, policy=policy)
    blocking = [finding for finding in result.findings if finding.blocks]
    assert [(finding.rule_id, finding.category, finding.match) for finding in blocking] == [
        ("home-path", "pii", private_path)
    ]
    finding = blocking[0]
    assert materialized[finding.start : finding.end] == private_path
    assert result.blocked
    assert result.redacted_text == materialized.replace(private_path, "<PRIVATE_HOME_PATH>")
    assert private_path not in result.redacted_text
    assert not scan_text(result.redacted_text, policy=policy).blocked


def test_generated_probe_detail_still_keeps_profile_paths_private(monkeypatch, tmp_path):
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("brigade.research.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    profile = _synthetic_home(".config", "browser")
    payload = doctor_payload(
        tmp_path,
        roster=doctor_roster(),
        probe=lambda _agent: {
            "auth_status": "authenticated",
            "detail": f"profile={profile} secret-cookie-value Bearer abcdef",
        },
    )
    serialized = json.dumps(payload)
    assert profile not in serialized
    assert "/" + "home" + "/" not in serialized
    assert "secret-cookie-value" not in serialized
    assert "Bearer " not in serialized
    assert payload["schema"] == "brigade.research.doctor.v1"


def test_documented_neutral_home_retains_component_state_layout():
    data = component_paths.data_root(env={"HOME": "/tmp/brigade-example-home"}, system="linux")
    path = component_paths.installed_previous_state_path(data)
    assert path.endswith("brigade/installed.previous.json")
