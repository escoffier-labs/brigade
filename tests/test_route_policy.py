"""Route-level exploration policy and band classifier (#573)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import aboyeur, outcome_cmd, route_policy, scorecard, verify_manifest, verify_trial
from brigade.roster import Agent, Roster
from brigade.route_receipts import route_decision_payload, write_route_decision

from tests.run_test_helpers import run_aboyeur_guarded
from tests.test_scorecard import (
    _SCORECARD_MANIFEST_ID,
    _effectiveness_command,
    _ensure_scorecard_manifest,
    _fixture_binding,
    _track_workspace_manifest,
    _write_verify_receipt,
)
from tests.test_verify_trial import _write_skill_subject

pytest_plugins = ["tests.test_scorecard"]


def _plan_mode_roster(orchestrator_cli: str) -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", orchestrator_cli, "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
        max_workers=1,
    )


def _write_skill_for(target: Path, artifact_id: str) -> None:
    _write_skill_subject(target, rel=f"skills/{artifact_id}/SKILL.md", content=f"# {artifact_id}\n")
    subprocess = __import__("subprocess")
    subprocess.run(["git", "add", f"skills/{artifact_id}"], cwd=target, check=True, stdout=subprocess.DEVNULL)


def _write_ineligible_receipt(target: Path, run_id: str, *, artifact_id: str, manifest_id: str) -> None:
    fingerprint = _skill_fingerprint(target, artifact_id)
    binding = _fixture_binding(
        fingerprint=fingerprint,
        target=target,
        manifest_id=manifest_id,
    )
    run_dir = target / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = {
        "schema": "brigade.work.verify_run.v1",
        "schema_version": 1,
        "status": "failed",
        "target": str(target),
        "subject_binding": {
            "artifact_kind": "skill",
            "artifact_id": artifact_id,
            "content_fingerprint": fingerprint,
        },
        "verify_manifest": binding,
        "commands": [],
    }
    (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def _run_receipt(*, skill_candidates: list[str] | None = None, routing_budget: dict | None = None) -> dict:
    route: dict = {
        "attached": True,
        "route": ["implement", "verify"],
        "size": "S",
        "signals": ["code"],
        "confidence": "high",
        "template_version": "vertical-slice.v1",
    }
    if skill_candidates is not None:
        route["skill_candidates"] = skill_candidates
    if routing_budget is not None:
        route["routing_budget"] = routing_budget
    return {
        "status": "ok",
        "started_at": "2026-07-20T12:00:00+00:00",
        "route": route,
    }


def _route_brief():
    return aboyeur.route_brief("implement helper", template="vertical-slice")


def _write_prior_decision(
    runs_dir: Path,
    run_id: str,
    *,
    route_class: str,
    assignments: list[dict],
    started_at: str = "2026-07-19T12:00:00+00:00",
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "started_at": started_at,
                "route": {"attached": True, "route": ["implement"], "size": "S", "signals": ["code"]},
            }
        )
        + "\n"
    )
    decision = {
        "schema_version": "brigade.route-decision.v1",
        "decided_at": started_at,
        "exploration": {"route_class": route_class},
        "skill_assignments": assignments,
    }
    path = run_dir / "route-decision.json"
    path.write_text(json.dumps(decision, indent=2) + "\n")
    return path


def _card_with_effectiveness(
    *,
    artifact_id: str = "brigade-work",
    helped: int,
    hurt: int = 0,
    trials: int | None = None,
    wilson: float = 0.2,
):
    trials = trials if trials is not None else helped + hurt
    return scorecard.SubjectScorecard(
        subject=scorecard.SubjectRef("skill", artifact_id, "fp"),
        dimensions={
            "effectiveness": {"helped": helped, "hurt": hurt, "wilson": wilson, "trials": trials},
            "verifier_cost": {},
            "retry_stability": {},
            "evidence_integrity": {},
        },
        utility_guardrails={
            "required_check_ids": ["guardrail.tests-green"],
            "passing_trials": 0,
            "required_trials": 2,
            "per_check": {},
        },
    )


def _write_route_opt_in_manifest(
    target: Path,
    *,
    manifest_id: str = _SCORECARD_MANIFEST_ID,
    artifact_id: str = "brigade-work",
    route_paths: list[str] | None = None,
    route_classes: list[str] | None = None,
    scope_globs: list[str] | None = None,
    binding_mode: str | None = None,
    fixture: dict | None = None,
) -> dict:
    _ensure_scorecard_manifest(target, manifest_id=manifest_id)
    path = target / "verify" / "manifests" / f"{manifest_id}.json"
    payload = verify_manifest._load_manifest_file(path)
    assert payload is not None
    payload = copy.deepcopy(payload)
    subject = payload.setdefault("subject", {})
    subject["artifact_id"] = artifact_id
    subject["artifact_kind"] = "skill"
    subject["subject_path"] = f"skills/{artifact_id}/SKILL.md"
    if route_paths is not None:
        payload["route_paths"] = route_paths
    if route_classes is not None:
        payload["route_classes"] = route_classes
    if scope_globs is not None:
        payload["scope_globs"] = scope_globs
    if binding_mode is not None:
        payload["binding_mode"] = binding_mode
    if fixture is not None:
        payload["fixture"] = fixture
    verify_manifest.write_workspace_manifest(target, payload)
    _track_workspace_manifest(target, manifest_id)
    return payload


def _skill_fingerprint(target: Path, artifact_id: str = "brigade-work") -> str:
    skill = target / "skills" / artifact_id / "SKILL.md"
    return verify_trial.subject_hash_for_path(target, str(skill.relative_to(target)))


def _prepare_candidate_skill(scoreable_target: Path, *, scope_globs: list[str] | None = None) -> None:
    _write_route_opt_in_manifest(scoreable_target, route_paths=["code"], scope_globs=scope_globs)
    _write_skill_subject(scoreable_target)
    fingerprint = _skill_fingerprint(scoreable_target)
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    _write_verify_receipt(
        scoreable_target,
        "one-pass",
        binding=binding,
        commands=[_effectiveness_command()],
    )


def _write_promoted_status(target: Path, artifact_id: str, *, with_policy_marker: bool) -> None:
    (target / "memory" / "outcome").mkdir(parents=True, exist_ok=True)
    entry: dict = {"status": "promoted"}
    if with_policy_marker:
        entry["route_policy"] = {
            "policy_version": scorecard.SCORECARD_POLICY_VERSION,
            "route_authority": "full",
        }
    (target / "memory" / "outcome" / "status.json").write_text(json.dumps({"artifacts": {artifact_id: entry}}) + "\n")


def test_classify_band_states():
    assert scorecard.classify_band(None) == "unseen"
    assert scorecard.classify_band(_card_with_effectiveness(helped=0, trials=0)) == "unseen"
    assert scorecard.classify_band(_card_with_effectiveness(helped=1)) == "candidate"
    assert scorecard.classify_band(_card_with_effectiveness(helped=2)) == "provisional"
    assert (
        scorecard.classify_band(
            _card_with_effectiveness(helped=2),
            persisted_status="promoted",
            policy_marker=scorecard.SCORECARD_POLICY_VERSION,
        )
        == "promoted"
    )
    demoted_card = _card_with_effectiveness(helped=2, hurt=1)
    assert (
        scorecard.classify_band(
            demoted_card,
            persisted_status="promoted",
            policy_marker=scorecard.SCORECARD_POLICY_VERSION,
        )
        == "provisional"
    )
    assert scorecard.classify_band(None, persisted_status="promoted") == "unseen"
    assert (
        scorecard.classify_band(
            _card_with_effectiveness(helped=2),
            persisted_status="promoted",
        )
        == "provisional"
    )


def test_exploration_quota_formula():
    assert route_policy.exploration_quota(0) == 1
    assert route_policy.exploration_quota(1) == 1
    assert route_policy.exploration_quota(10) == 1
    assert route_policy.exploration_quota(20) == 2
    assert route_policy.exploration_quota(100) == 2


def test_unseen_cannot_be_sole_provider(tmp_path):
    card = _card_with_effectiveness(artifact_id="new-skill", helped=0, trials=1)
    manifest = verify_manifest.VerifyManifest(
        manifest_id="new-skill-manifest",
        binding_mode="fixture_eval",
        artifact_kind="skill",
        artifact_id="new-skill",
        verifier_id="brigade.verify.fixture",
        checks=(),
        route_paths=("code",),
        subject_path="skills/new-skill/SKILL.md",
        path=tmp_path / "verify/manifests/new-skill.json",
    )
    entry = route_policy.TrustedOptInEntry(
        manifest=manifest,
        manifest_path="verify/manifests/new-skill.json",
        card=card,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(route_policy, "discover_trusted_opt_in_manifests", lambda *args, **kwargs: ([entry], []))
        decision = route_policy.decide_route_skills(
            tmp_path,
            route_brief=_route_brief(),
            now=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
    assert decision.policy_applied
    assert decision.assignments == []
    assert any(
        entry["artifact_id"] == "new-skill" and entry["reason"] == "unseen_cannot_be_sole_provider"
        for entry in decision.accept_reject
    )


def test_promoted_skill_gets_priority_and_shadow_unseen(scoreable_target):
    _ensure_scorecard_manifest(scoreable_target, manifest_id="proven-manifest")
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="proven-manifest",
        artifact_id="proven-skill",
        route_paths=["code"],
    )
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="shadow-manifest",
        artifact_id="shadow-skill",
        route_paths=["code"],
        binding_mode="fixture_eval",
        fixture={
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
    )
    _write_promoted_status(scoreable_target, "proven-skill", with_policy_marker=True)
    _write_skill_for(scoreable_target, "proven-skill")
    _write_skill_for(scoreable_target, "shadow-skill")
    _write_ineligible_receipt(
        scoreable_target,
        "shadow-ineligible",
        artifact_id="shadow-skill",
        manifest_id="shadow-manifest",
    )
    proven_fp = _skill_fingerprint(scoreable_target, "proven-skill")
    _write_verify_receipt(
        scoreable_target,
        "proven-pass",
        binding=_fixture_binding(
            fingerprint=proven_fp,
            target=scoreable_target,
            manifest_id="proven-manifest",
            artifact_id="proven-skill",
        ),
        commands=[_effectiveness_command()],
    )
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert [item.artifact_id for item in decision.assignments] == ["proven-skill", "shadow-skill"]
    assert decision.assignments[0].route_authority == "full"
    shadow = decision.assignments[1]
    assert shadow.route_authority == "shadow"
    assert shadow.exploratory is True
    assert shadow.verify_manifest_id == "shadow-manifest"


def test_legacy_promoted_status_without_policy_marker_is_rejected(scoreable_target):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    _write_promoted_status(scoreable_target, "brigade-work", with_policy_marker=False)
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert not any(item.band == "promoted" for item in decision.assignments)


def test_candidate_requires_scope_globs_for_scoped_write(scoreable_target):
    _prepare_candidate_skill(scoreable_target, scope_globs=[])
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert not any(item.exploratory for item in decision.assignments)
    assert any(
        entry["artifact_id"] == "brigade-work" and entry["reason"] == "missing_scope_globs"
        for entry in decision.accept_reject
    )


def test_candidate_scoped_write_with_manifest_globs(scoreable_target):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**", "tests/**"])
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    exploratory = [item for item in decision.assignments if item.exploratory]
    assert len(exploratory) == 1
    assert exploratory[0].scope_globs == ("src/**", "tests/**")
    assert exploratory[0].manifest_path is not None


def test_at_most_one_exploratory_skill_includes_shadow(scoreable_target):
    _ensure_scorecard_manifest(scoreable_target, manifest_id="proven-manifest")
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="proven-manifest",
        artifact_id="proven-skill",
        route_paths=["code"],
    )
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="candidate-manifest",
        artifact_id="candidate-skill",
        route_paths=["code"],
        scope_globs=["src/**"],
    )
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="shadow-manifest",
        artifact_id="shadow-skill",
        route_paths=["code"],
        binding_mode="fixture_eval",
        fixture={
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
    )
    _write_promoted_status(scoreable_target, "proven-skill", with_policy_marker=True)
    for artifact_id, manifest_id in (("proven-skill", "proven-manifest"), ("candidate-skill", "candidate-manifest")):
        _write_skill_for(scoreable_target, artifact_id)
        fingerprint = _skill_fingerprint(scoreable_target, artifact_id)
        _write_verify_receipt(
            scoreable_target,
            f"{artifact_id}-pass",
            binding=_fixture_binding(
                fingerprint=fingerprint,
                target=scoreable_target,
                manifest_id=manifest_id,
                artifact_id=artifact_id,
            ),
            commands=[_effectiveness_command()],
        )
    _write_skill_for(scoreable_target, "shadow-skill")
    _write_ineligible_receipt(
        scoreable_target,
        "shadow-ineligible",
        artifact_id="shadow-skill",
        manifest_id="shadow-manifest",
    )
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    exploratory = [item for item in decision.assignments if item.exploratory]
    assert len(exploratory) == 1
    assert exploratory[0].artifact_id == "candidate-skill"
    assert not any(item.artifact_id == "shadow-skill" for item in decision.assignments)


def test_exploration_quota_blocks_after_cap(tmp_path):
    runs_dir = tmp_path / ".brigade" / "runs"
    route_class = outcome_cmd.route_fingerprint(route_policy.route_manifest_from_brief(_route_brief()))
    assert route_class is not None
    _write_prior_decision(
        runs_dir,
        "prior-1",
        route_class=route_class,
        assignments=[{"artifact_id": "skill-b", "exploratory": True}],
        started_at="2026-07-19T12:00:00+00:00",
    )
    card = _card_with_effectiveness(artifact_id="skill-b", helped=1)
    manifest = verify_manifest.VerifyManifest(
        manifest_id="skill-b-manifest",
        binding_mode="patch_backed",
        artifact_kind="skill",
        artifact_id="skill-b",
        verifier_id="brigade.verify.fixture",
        checks=(),
        route_paths=("code",),
        scope_globs=("src/**",),
        subject_path="skills/skill-b/SKILL.md",
        path=tmp_path / "verify/manifests/skill-b.json",
    )
    entry = route_policy.TrustedOptInEntry(
        manifest=manifest,
        manifest_path="verify/manifests/skill-b.json",
        card=card,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(route_policy, "discover_trusted_opt_in_manifests", lambda *args, **kwargs: ([entry], []))
        decision = route_policy.decide_route_skills(
            tmp_path,
            route_brief=_route_brief(),
            runs_dir=runs_dir,
            now=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
    assert decision.quota == 1
    assert decision.exploratory_assignment_count_7d == 1
    assert not any(item.exploratory for item in decision.assignments)
    assert any(entry["reason"] == "exploration_quota_exhausted" for entry in decision.accept_reject)


def test_hard_ceiling_blocks_skill_for_30_days(tmp_path):
    runs_dir = tmp_path / ".brigade" / "runs"
    route_class = outcome_cmd.route_fingerprint(route_policy.route_manifest_from_brief(_route_brief()))
    assert route_class is not None
    for index in range(route_policy.EXPLORATION_HARD_CEILING):
        _write_prior_decision(
            runs_dir,
            f"prior-{index}",
            route_class=route_class,
            assignments=[{"artifact_id": "skill-a", "exploratory": True}],
            started_at=f"2026-06-{20 + index:02d}T12:00:00+00:00",
        )
    card_a = _card_with_effectiveness(artifact_id="skill-a", helped=1, wilson=0.9)
    card_b = _card_with_effectiveness(artifact_id="skill-b", helped=1, wilson=0.5)
    entries = []
    for artifact_id, card in (("skill-a", card_a), ("skill-b", card_b)):
        manifest = verify_manifest.VerifyManifest(
            manifest_id=f"{artifact_id}-manifest",
            binding_mode="patch_backed",
            artifact_kind="skill",
            artifact_id=artifact_id,
            verifier_id="brigade.verify.fixture",
            checks=(),
            route_paths=("code",),
            scope_globs=("src/**",),
            subject_path=f"skills/{artifact_id}/SKILL.md",
            path=tmp_path / f"verify/manifests/{artifact_id}.json",
        )
        entries.append(
            route_policy.TrustedOptInEntry(
                manifest=manifest,
                manifest_path=f"verify/manifests/{artifact_id}.json",
                card=card,
            )
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(route_policy, "discover_trusted_opt_in_manifests", lambda *args, **kwargs: (entries, []))
        decision = route_policy.decide_route_skills(
            tmp_path,
            route_brief=_route_brief(),
            runs_dir=runs_dir,
            now=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
    exploratory = [item for item in decision.assignments if item.exploratory]
    assert len(exploratory) == 1
    assert exploratory[0].artifact_id == "skill-b"
    assert any(
        entry["artifact_id"] == "skill-a" and entry["reason"] == "exploration_hard_ceiling"
        for entry in decision.accept_reject
    )


def test_routing_budget_blocks_exploratory_only(tmp_path):
    card = _card_with_effectiveness(artifact_id="skill-a", helped=1)
    manifest = verify_manifest.VerifyManifest(
        manifest_id="skill-a-manifest",
        binding_mode="patch_backed",
        artifact_kind="skill",
        artifact_id="skill-a",
        verifier_id="brigade.verify.fixture",
        checks=(),
        route_paths=("code",),
        scope_globs=("src/**",),
        subject_path="skills/skill-a/SKILL.md",
        path=tmp_path / "verify/manifests/skill-a.json",
    )
    entry = route_policy.TrustedOptInEntry(
        manifest=manifest,
        manifest_path="verify/manifests/skill-a.json",
        card=card,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(route_policy, "discover_trusted_opt_in_manifests", lambda *args, **kwargs: ([entry], []))
        decision = route_policy.decide_route_skills(
            tmp_path,
            route_brief=_route_brief(),
            budget=route_policy.RouteBudget(token_budget=10, token_spent=10),
            now=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
    assert not any(item.exploratory for item in decision.assignments)
    assert any(entry["reason"] == "routing_budget_exhausted" for entry in decision.accept_reject)


def test_exploration_caps_do_not_mutate_scorecard_receipts(scoreable_target):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    before_cards = scorecard.build_scorecards(scoreable_target)
    receipt_paths = list(scorecard.discover_verify_receipt_paths(scoreable_target))
    before_receipts = {str(path): path.read_text() for path in receipt_paths}
    for _ in range(5):
        route_policy.decide_route_skills(
            scoreable_target,
            route_brief=_route_brief(),
            now=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
    after_cards = scorecard.build_scorecards(scoreable_target)
    after_receipts = {str(path): path.read_text() for path in receipt_paths}
    assert before_receipts == after_receipts
    assert before_cards[0].dimensions["effectiveness"] == after_cards[0].dimensions["effectiveness"]


def test_route_decision_payload_includes_policy_fields(scoreable_target, tmp_path):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    _write_promoted_status(scoreable_target, "brigade-work", with_policy_marker=True)
    runs_dir = tmp_path / "runs"
    output_dir = runs_dir / "current"
    output_dir.mkdir(parents=True)
    receipt = _run_receipt()
    (output_dir / "run.json").write_text(json.dumps(receipt) + "\n")
    from brigade import roster as roster_mod

    roster = roster_mod.Roster(
        orchestrator="chef",
        agents={"chef": roster_mod.Agent("chef", "codex", "plan"), "coder": roster_mod.Agent("coder", "codex", "code")},
        max_workers=1,
        allow_models=("codex",),
    )
    decision = route_policy.decide_route_skills(scoreable_target, route_brief=_route_brief())
    extensions = route_policy.route_policy_extensions_from_decision(decision)
    write_route_decision(output_dir, roster, policy_extensions=extensions)
    saved = json.loads((output_dir / "route-decision.json").read_text())
    assert saved["policy_version"] == route_policy.ROUTE_POLICY_VERSION
    assert "decided_at" in saved
    assert "exploration" in saved
    assert "quota" in saved["exploration"]
    assert "accept_reject" in saved["exploration"]


def test_route_decision_payload_omits_policy_without_opt_in(tmp_path):
    from brigade import roster as roster_mod

    roster = roster_mod.Roster(
        orchestrator="chef",
        agents={"chef": roster_mod.Agent("chef", "codex", "plan")},
        max_workers=1,
        allow_models=("codex",),
    )
    payload = route_decision_payload(_run_receipt(), roster, target=tmp_path)
    assert "policy_version" not in payload
    assert "score_inputs" not in payload


def test_pre_run_decision_is_preserved_by_write_route_decision(scoreable_target, tmp_path):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    runs_dir = tmp_path / "runs"
    output_dir = runs_dir / "current"
    output_dir.mkdir(parents=True)
    receipt = _run_receipt()
    (output_dir / "run.json").write_text(json.dumps(receipt) + "\n")
    from brigade import roster as roster_mod

    roster = roster_mod.Roster(
        orchestrator="chef",
        agents={"chef": roster_mod.Agent("chef", "codex", "plan")},
        max_workers=1,
        allow_models=("codex",),
    )
    decision = route_policy.decide_route_skills(scoreable_target, route_brief=_route_brief())
    extensions = route_policy.route_policy_extensions_from_decision(decision)
    write_route_decision(output_dir, roster, policy_extensions=extensions)
    before = json.loads((output_dir / "route-decision.json").read_text())
    write_route_decision(output_dir, roster, target=scoreable_target, runs_dir=runs_dir)
    after = json.loads((output_dir / "route-decision.json").read_text())
    assert after["decided_at"] == before["decided_at"]
    assert after["skill_assignments"] == before["skill_assignments"]
    assert after["exploration"] == before["exploration"]


def test_planner_prompt_receives_selected_policy(monkeypatch, scoreable_target):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    prompts: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):
        prompts.append(prompt)
        return aboyeur.agents.AgentResult(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "worker": "coder",
                            "task": "implement helper in src",
                            "covers": ["implement", "code"],
                            "selected_skill_ids": ["brigade-work"],
                        }
                    ]
                }
            ),
            ok=True,
        )

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    route = _route_brief()
    skill_policy = route_policy.decide_route_skills(scoreable_target, route_brief=route)
    aboyeur.plan(
        "build feature",
        _plan_mode_roster("codex"),
        route=route,
        skill_policy=skill_policy,
    )
    assert "Skill route policy" in prompts[0]
    assert "scoped write only within globs" in prompts[0]


def test_no_opt_in_keeps_plan_prompt_unchanged(monkeypatch, tmp_path):
    prompts: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):
        prompts.append(prompt)
        return aboyeur.agents.AgentResult(text=json.dumps({"assignments": []}), ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    route = _route_brief()
    skill_policy = route_policy.decide_route_skills(tmp_path, route_brief=route)
    assert skill_policy.policy_applied is False
    aboyeur.plan(
        "build feature",
        _plan_mode_roster("codex"),
        route=route,
        skill_policy=skill_policy,
    )
    assert "Skill route policy" not in prompts[0]


def test_shadow_verify_directive_in_planner_prompt(monkeypatch, scoreable_target):
    _ensure_scorecard_manifest(scoreable_target, manifest_id="proven-manifest")
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="proven-manifest",
        artifact_id="proven-skill",
        route_paths=["code"],
    )
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="shadow-manifest",
        artifact_id="shadow-skill",
        route_paths=["code"],
        binding_mode="fixture_eval",
        fixture={
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
    )
    _write_promoted_status(scoreable_target, "proven-skill", with_policy_marker=True)
    _write_skill_for(scoreable_target, "proven-skill")
    _write_skill_for(scoreable_target, "shadow-skill")
    _write_ineligible_receipt(
        scoreable_target,
        "shadow-ineligible",
        artifact_id="shadow-skill",
        manifest_id="shadow-manifest",
    )
    proven_fp = _skill_fingerprint(scoreable_target, "proven-skill")
    _write_verify_receipt(
        scoreable_target,
        "proven-pass",
        binding=_fixture_binding(
            fingerprint=proven_fp,
            target=scoreable_target,
            manifest_id="proven-manifest",
            artifact_id="proven-skill",
        ),
        commands=[_effectiveness_command()],
    )
    prompts: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):
        prompts.append(prompt)
        return aboyeur.agents.AgentResult(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "stage": 1,
                            "worker": "coder",
                            "task": "implement helper",
                            "covers": ["implement", "code"],
                        },
                        {
                            "stage": 2,
                            "worker": "coder",
                            "task": "brigade work verify run --target . --manifest shadow-manifest",
                            "covers": ["verify"],
                            "selected_skill_ids": ["shadow-skill"],
                        },
                    ]
                }
            ),
            ok=True,
        )

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    route = _route_brief()
    skill_policy = route_policy.decide_route_skills(scoreable_target, route_brief=route)
    aboyeur.plan("build feature", _plan_mode_roster("codex"), route=route, skill_policy=skill_policy)
    assert "verify-stage assignment" in prompts[0] or "verify-covering assignment" in prompts[0]
    assert "brigade work verify run --target . --manifest shadow-manifest" in prompts[0]


def test_scope_globs_reject_whole_repo_wildcards():
    assert route_policy.validate_scope_globs(("**",)) == "invalid_scope_glob"
    assert route_policy.validate_scope_globs(("**/*",)) == "invalid_scope_glob"
    assert route_policy.validate_scope_globs(("../secrets",)) == "invalid_scope_glob"
    assert route_policy.validate_scope_globs(("src/**",)) is None


def test_candidate_rejects_invalid_scope_globs(scoreable_target):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    path = scoreable_target / "verify" / "manifests" / f"{_SCORECARD_MANIFEST_ID}.json"
    payload = verify_manifest._load_manifest_file(path)
    assert payload is not None
    payload = copy.deepcopy(payload)
    payload["scope_globs"] = ["**"]
    path.write_text(json.dumps(payload, indent=2) + "\n")
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert not any(item.exploratory for item in decision.assignments)
    assert any(
        entry["artifact_id"] == "brigade-work" and entry["reason"] == "invalid_scope_glob"
        for entry in decision.accept_reject
    )


def test_shadow_exploration_quota_blocks_with_proven_route(scoreable_target):
    _ensure_scorecard_manifest(scoreable_target, manifest_id="proven-manifest")
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="proven-manifest",
        artifact_id="proven-skill",
        route_paths=["code"],
    )
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="shadow-manifest",
        artifact_id="shadow-skill",
        route_paths=["code"],
        binding_mode="fixture_eval",
        fixture={
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
    )
    _write_promoted_status(scoreable_target, "proven-skill", with_policy_marker=True)
    _write_skill_for(scoreable_target, "proven-skill")
    _write_skill_for(scoreable_target, "shadow-skill")
    proven_fp = _skill_fingerprint(scoreable_target, "proven-skill")
    _write_verify_receipt(
        scoreable_target,
        "proven-pass",
        binding=_fixture_binding(
            fingerprint=proven_fp,
            target=scoreable_target,
            manifest_id="proven-manifest",
            artifact_id="proven-skill",
        ),
        commands=[_effectiveness_command()],
    )
    runs_dir = scoreable_target / ".brigade" / "runs"
    route_class = outcome_cmd.route_fingerprint(route_policy.route_manifest_from_brief(_route_brief()))
    assert route_class is not None
    _write_prior_decision(
        runs_dir,
        "prior-shadow",
        route_class=route_class,
        assignments=[{"artifact_id": "other-shadow", "exploratory": True}],
        started_at="2026-07-19T12:00:00+00:00",
    )
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        runs_dir=runs_dir,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert [item.artifact_id for item in decision.assignments] == ["proven-skill"]
    assert not any(item.artifact_id == "shadow-skill" for item in decision.assignments)
    assert any(
        entry["artifact_id"] == "shadow-skill" and entry["reason"] == "exploration_quota_exhausted"
        for entry in decision.accept_reject
    )


def test_zero_receipt_cold_start_shadow(scoreable_target):
    _ensure_scorecard_manifest(scoreable_target, manifest_id="proven-manifest")
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="proven-manifest",
        artifact_id="proven-skill",
        route_paths=["code"],
    )
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="shadow-manifest",
        artifact_id="shadow-skill",
        route_paths=["code"],
        binding_mode="fixture_eval",
        fixture={
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
    )
    _write_promoted_status(scoreable_target, "proven-skill", with_policy_marker=True)
    _write_skill_for(scoreable_target, "proven-skill")
    _write_skill_for(scoreable_target, "shadow-skill")
    proven_fp = _skill_fingerprint(scoreable_target, "proven-skill")
    _write_verify_receipt(
        scoreable_target,
        "proven-pass",
        binding=_fixture_binding(
            fingerprint=proven_fp,
            target=scoreable_target,
            manifest_id="proven-manifest",
            artifact_id="proven-skill",
        ),
        commands=[_effectiveness_command()],
    )
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert [item.artifact_id for item in decision.assignments] == ["proven-skill", "shadow-skill"]
    shadow = decision.assignments[1]
    assert shadow.route_authority == "shadow"
    assert shadow.exploratory is True


def test_ambiguous_route_opt_in_manifests_fail_closed(scoreable_target):
    for manifest_id in ("proven-manifest", "proven-manifest-2"):
        payload = {
            "schema": "brigade.verify_manifest.v1",
            "schema_version": 1,
            "manifest_id": manifest_id,
            "binding_mode": "patch_backed",
            "verifier_id": "brigade.verify.test",
            "patch_source": "worktree",
            "route_paths": ["code"],
            "subject": {
                "artifact_kind": "skill",
                "artifact_id": "proven-skill",
                "subject_path": "skills/proven-skill/SKILL.md",
            },
            "checks": [
                {
                    "check_id": "verify.echo-ok",
                    "check_role": "effectiveness",
                    "command": f"{__import__('sys').executable} -c \"print('ok')\"",
                }
            ],
        }
        verify_manifest.write_workspace_manifest(scoreable_target, payload)
        _track_workspace_manifest(scoreable_target, manifest_id)
    _write_promoted_status(scoreable_target, "proven-skill", with_policy_marker=True)
    _write_skill_for(scoreable_target, "proven-skill")
    proven_fp = _skill_fingerprint(scoreable_target, "proven-skill")
    _write_verify_receipt(
        scoreable_target,
        "proven-pass",
        binding=_fixture_binding(
            fingerprint=proven_fp,
            target=scoreable_target,
            manifest_id="proven-manifest",
            artifact_id="proven-skill",
        ),
        commands=[_effectiveness_command()],
    )
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert not decision.assignments
    assert not any(item.band == "promoted" for item in decision.assignments)
    assert any(
        entry["artifact_id"] == "proven-skill" and entry["reason"] == "ambiguous_production_manifest"
        for entry in decision.accept_reject
    )


def test_promoted_with_production_and_fixture_yields_one_assignment(scoreable_target):
    path = scoreable_target / "verify" / "manifests" / "proven-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": "proven-manifest",
        "binding_mode": "patch_backed",
        "verifier_id": "brigade.verify.test",
        "patch_source": "worktree",
        "route_paths": ["code"],
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "proven-skill",
            "subject_path": "skills/proven-skill/SKILL.md",
        },
        "checks": [
            {
                "check_id": "verify.echo-ok",
                "check_role": "effectiveness",
                "command": f"{__import__('sys').executable} -c \"print('ok')\"",
            }
        ],
    }
    verify_manifest.write_workspace_manifest(scoreable_target, payload)
    _track_workspace_manifest(scoreable_target, "proven-manifest")
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="shadow-manifest",
        artifact_id="proven-skill",
        route_paths=["code"],
        binding_mode="fixture_eval",
        fixture={
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
    )
    _write_promoted_status(scoreable_target, "proven-skill", with_policy_marker=True)
    _write_skill_for(scoreable_target, "proven-skill")
    proven_fp = _skill_fingerprint(scoreable_target, "proven-skill")
    proven_manifest, _ = verify_manifest.resolve_manifest(scoreable_target, "proven-manifest")
    assert proven_manifest is not None
    _write_verify_receipt(
        scoreable_target,
        "proven-pass",
        binding={
            "binding_mode": "patch_backed",
            "artifact_kind": "skill",
            "artifact_id": "proven-skill",
            "content_fingerprint": proven_fp,
            "manifest_binding": verify_manifest.build_manifest_binding(scoreable_target, proven_manifest),
            "patch_binding": {
                "subject_path": "skills/proven-skill/SKILL.md",
                "patch_source": "worktree",
            },
            "verifier_identity": {"verifier_id": "brigade.verify.test", "session_id": "verifier-1"},
        },
        commands=[_effectiveness_command()],
    )
    decision = route_policy.decide_route_skills(
        scoreable_target,
        route_brief=_route_brief(),
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert [item.artifact_id for item in decision.assignments] == ["proven-skill"]
    assert decision.assignments[0].manifest_id == "proven-manifest"


def test_direct_worker_binds_scoped_candidate_skill(monkeypatch, scoreable_target, tmp_path):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    output_dir = tmp_path / "run-dir"
    output_dir.mkdir()

    def fake_dispatch(assignments, roster, **kwargs):
        build_prompt = kwargs["build_prompt"]
        prompt = build_prompt(roster.agents["coder"], assignments[0], direct=True)
        assert "Scoped-write constraint" in prompt
        return [aboyeur.WorkerResult(worker="coder", task="implement helper in src", text="done", ok=True)]

    monkeypatch.setattr(aboyeur, "dispatch", fake_dispatch)
    rc = run_aboyeur_guarded(
        "implement helper in src",
        _plan_mode_roster("codex"),
        cwd=scoreable_target,
        output_dir=output_dir,
        worker="coder",
        evidence_enabled=False,
        code_graph_enabled=False,
    )
    assert rc == 0
    plan_doc = json.loads((output_dir / "plan.json").read_text())
    assert plan_doc["assignments"][0]["selected_skill_ids"] == ["brigade-work"]
    route_decision = json.loads((output_dir / "route-decision.json").read_text())
    exploratory = [item for item in route_decision["skill_assignments"] if item.get("exploratory")]
    assert len(exploratory) == 1
    assert exploratory[0]["artifact_id"] == "brigade-work"


def test_direct_worker_rejects_shadow_without_quota_consumption(monkeypatch, scoreable_target, tmp_path):
    _ensure_scorecard_manifest(scoreable_target, manifest_id="proven-manifest")
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="proven-manifest",
        artifact_id="proven-skill",
        route_paths=["code"],
    )
    _write_route_opt_in_manifest(
        scoreable_target,
        manifest_id="shadow-manifest",
        artifact_id="shadow-skill",
        route_paths=["code"],
        binding_mode="fixture_eval",
        fixture={
            "manifest_id": "adversarial-failed-verification",
            "case_id": "report-failing-tests",
            "check_id": "fixture.echo-ok",
        },
    )
    _write_promoted_status(scoreable_target, "proven-skill", with_policy_marker=True)
    _write_skill_for(scoreable_target, "proven-skill")
    _write_skill_for(scoreable_target, "shadow-skill")
    proven_fp = _skill_fingerprint(scoreable_target, "proven-skill")
    _write_verify_receipt(
        scoreable_target,
        "proven-pass",
        binding=_fixture_binding(
            fingerprint=proven_fp,
            target=scoreable_target,
            manifest_id="proven-manifest",
            artifact_id="proven-skill",
        ),
        commands=[_effectiveness_command()],
    )
    output_dir = tmp_path / "run-dir"
    output_dir.mkdir()

    def fake_run_agent(cli_ref, prompt, **kwargs):
        return aboyeur.agents.AgentResult(text="done", ok=True)

    def fake_dispatch(*args, **kwargs):
        return [aboyeur.WorkerResult(worker="coder", task="implement helper", text="done", ok=True)]

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    monkeypatch.setattr(aboyeur, "dispatch", fake_dispatch)
    rc = run_aboyeur_guarded(
        "implement helper",
        _plan_mode_roster("codex"),
        cwd=scoreable_target,
        output_dir=output_dir,
        worker="coder",
        evidence_enabled=False,
        code_graph_enabled=False,
    )
    assert rc == 0
    route_decision = json.loads((output_dir / "route-decision.json").read_text())
    assert [item["artifact_id"] for item in route_decision["skill_assignments"]] == ["proven-skill"]
    assert not any(item.get("exploratory") for item in route_decision["skill_assignments"])
    assert any(
        entry["artifact_id"] == "shadow-skill" and entry["reason"] == route_policy.DIRECT_WORKER_REJECTS_SHADOW
        for entry in route_decision["exploration"]["accept_reject"]
    )
    plan_doc = json.loads((output_dir / "plan.json").read_text())
    assert "selected_skill_ids" not in plan_doc["assignments"][0]


def test_run_receipt_preserves_skill_route_policy_through_status_writes(monkeypatch, scoreable_target, tmp_path):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    output_dir = tmp_path / "run-dir"
    output_dir.mkdir()

    def fake_run_agent(cli_ref, prompt, **kwargs):
        return aboyeur.agents.AgentResult(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "worker": "coder",
                            "task": "implement helper in src",
                            "covers": ["implement", "code"],
                            "selected_skill_ids": ["brigade-work"],
                        }
                    ]
                }
            ),
            ok=True,
        )

    def fake_dispatch(*args, **kwargs):
        return [
            aboyeur.WorkerResult(
                worker="coder",
                task="implement helper in src",
                text="done",
                ok=True,
            )
        ]

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    monkeypatch.setattr(aboyeur, "dispatch", fake_dispatch)
    rc = run_aboyeur_guarded(
        "implement helper",
        _plan_mode_roster("codex"),
        cwd=scoreable_target,
        output_dir=output_dir,
        evidence_enabled=False,
        code_graph_enabled=False,
    )
    assert rc == 0
    run_receipt = json.loads((output_dir / "run.json").read_text())
    route_decision = json.loads((output_dir / "route-decision.json").read_text())
    assert "skill_route_policy" in run_receipt
    assert run_receipt["skill_route_policy"]["policy_version"] == route_policy.ROUTE_POLICY_VERSION
    assert run_receipt["skill_route_policy"]["exploration"] == route_decision["exploration"]
    assert run_receipt["skill_route_policy"]["skill_assignments"] == route_decision["skill_assignments"]
    plan_doc = json.loads((output_dir / "plan.json").read_text())
    assert plan_doc["assignments"][0]["selected_skill_ids"] == ["brigade-work"]


def test_worker_prompt_applies_scope_only_on_bound_assignment(monkeypatch, scoreable_target):
    _prepare_candidate_skill(scoreable_target, scope_globs=["src/**"])
    route = _route_brief()
    skill_policy = route_policy.decide_route_skills(scoreable_target, route_brief=route)
    bound = aboyeur.Assignment(
        worker="coder",
        task="edit src/foo.py",
        covers=("implement",),
        selected_skill_ids=("brigade-work",),
    )
    unbound = aboyeur.Assignment(worker="coder", task="run tests", covers=("verify",))
    assert "Scoped-write constraint" in aboyeur._worker_prompt(
        _plan_mode_roster("codex").agents["coder"],
        bound,
        skill_policy=skill_policy,
    )
    assert "Scoped-write constraint" not in aboyeur._worker_prompt(
        _plan_mode_roster("codex").agents["coder"],
        unbound,
        skill_policy=skill_policy,
    )
