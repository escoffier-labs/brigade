"""Supplied declarations never grant launch; immutable replay binds every input."""

from dataclasses import FrozenInstanceError, replace

import pytest

from brigade.native_t3_contract import (
    AuthorityReference,
    BindingEvidence,
    DiscoveryEvidence,
    ModelDeclaration,
    NativeIntent,
    NativeTarget,
    Qualification,
    WorkBinding,
    compare_intents,
    qualification_diagnostic,
    qualify_target,
)
from brigade.run_tasks import TaskHandle, TaskSpec


def target(**changes):
    return replace(
        NativeTarget(
            origin_environment="env-origin",
            origin_thread="thread-parent",
            target_environment="env-target",
            target_project="project-test",
            provider_instance="codex-test",
            driver="codex",
            launch_model="launch-model",
            options=(("reasoningEffort", "high"),),
            runtime_mode="app-server",
            interaction_mode="default",
            isolation="worktree",
            source_revision="a" * 40,
        ),
        **changes,
    )


def discovery(**changes):
    return replace(
        DiscoveryEvidence(
            environment="env-target",
            project="project-test",
            provider_instance="codex-test",
            driver="codex",
            observed_at=100.0,
            expires_at=125.0,
            child_enabled=True,
            runtime_modes=("app-server",),
            models=(ModelDeclaration("launch-model", (("reasoningEffort", ("high", "low")),)),),
        ),
        **changes,
    )


def binding(**changes):
    return replace(
        BindingEvidence(
            consumer="consumer-test",
            seat="worker-test",
            canonical_model="canonical-model",
            launch_model="launch-model",
            roster_digest="b" * 64,
            policy_digest="c" * 64,
            roster_revision=2,
            policy_version=3,
            observed_at=101.0,
            expires_at=124.0,
            refusal=None,
        ),
        **changes,
    )


def qualify(selected=None, **changes):
    supplied = dict(consumer="consumer-test", seat="worker-test", discovery=discovery(), binding=binding(), now=110.0)
    supplied.update(changes)
    return qualify_target(target() if selected is None else selected, **supplied)


def intent(**changes):
    return replace(
        NativeIntent(
            work=WorkBinding("authority-test", "work-test", "org-test/repo-test", "refs/heads/test", "node-test", 2, 3),
            handle=TaskHandle("run-test", "request-test", TaskSpec("task-one", "worker-test", "d" * 64), "e" * 64),
            dispatch_attempt=1,
            target=target(),
            payload_context_digest="f" * 64,
            batch_binding_ref="batch-test",
            native_client_request_key="request-native",
            authority_refs=(AuthorityReference("policy", "policy-test", 124.0),),
        ),
        **changes,
    )


def test_matched_declarations_preserve_source_times_and_never_claim_readiness():
    result = qualify()
    assert qualification_diagnostic(result) == {
        "status": "declared",
        "reasons": ["route-support-unknown", "isolation-unknown", "admission-unknown", "capacity-unknown"],
        "observed_at": 100.0,
        "expires_at": 124.0,
    }
    assert qualify(now=111.0) == result


@pytest.mark.parametrize("missing", ["discovery", "binding", "both"])
def test_missing_evidence_stays_unknown(missing):
    changes = {name: None for name in ("discovery", "binding") if missing in (name, "both")}
    result = qualify(**changes)
    assert result.status == "unknown"
    assert result.observed_at == ({"discovery": 101.0, "binding": 100.0, "both": None}[missing])


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"discovery": discovery(child_enabled=False), "binding": None}, "provider-disabled"),
        ({"binding": binding(refusal="policy-blocked"), "discovery": None}, "policy-blocked"),
        ({"binding": binding(refusal="retired-model"), "discovery": None}, "retired-model"),
    ],
)
def test_fresh_refusal_precedes_missing_declarations(changes, reason):
    result = qualify(**changes)
    assert result.status == "refused"
    assert reason in result.reasons


@pytest.mark.parametrize("source", ["discovery", "binding"])
@pytest.mark.parametrize(
    "times,now",
    [
        ({"expires_at": 109.0}, 110.0),
        ({"expires_at": 110.0}, 110.0),
        ({"observed_at": 111.0}, 110.0),
        ({"observed_at": 79.0, "expires_at": 140.0}, 110.0),
    ],
)
def test_expired_future_or_over_ttl_evidence_is_unknown(source, times, now):
    evidence = discovery(**times) if source == "discovery" else binding(**times)
    result = qualify(**{source: evidence}, now=now)
    assert result.status == "unknown"
    assert f"{source}-stale" in result.reasons


@pytest.mark.parametrize("source", ["discovery", "binding"])
def test_old_refusal_cannot_create_current_denial(source):
    evidence = (
        discovery(child_enabled=False, observed_at=70.0, expires_at=140.0)
        if source == "discovery"
        else binding(refusal="seat-disabled", observed_at=70.0, expires_at=140.0)
    )
    assert qualify(**{source: evidence}).status == "unknown"


def test_operation_ttl_boundary_uses_original_observation():
    old = discovery(observed_at=80.0, expires_at=140.0)
    assert qualify(discovery=old).status == "declared"
    assert qualify(discovery=old, now=110.001).status == "unknown"


@pytest.mark.parametrize("field", ["environment", "project", "provider_instance", "driver"])
def test_discovery_cannot_inherit_or_substitute_selected_context(field):
    result = qualify(discovery=discovery(**{field: "different"}))
    assert result.status == "refused"
    assert "discovery-context-mismatch" in result.reasons


@pytest.mark.parametrize("field", ["consumer", "seat", "launch_model"])
def test_binding_matches_explicit_consumer_seat_and_launch_model(field):
    result = qualify(binding=binding(**{field: "different"}))
    assert result.status == "refused"
    assert "binding-mismatch" in result.reasons


def test_canonical_model_match_does_not_replace_launch_model_binding():
    assert qualify(binding=binding(canonical_model="launch-model", launch_model="other-model")).status == "refused"


@pytest.mark.parametrize(
    "selected,evidence,reason",
    [
        (target(launch_model="other-model"), discovery(), "model-undeclared"),
        (target(options=(("unknownOption", "high"),)), discovery(), "option-undeclared"),
        (target(options=(("reasoningEffort", "medium"),)), discovery(), "option-value-undeclared"),
        (target(runtime_mode="other-runtime"), discovery(), "runtime-undeclared"),
        (target(), discovery(models=()), "model-undeclared"),
        (target(), discovery(runtime_modes=()), "runtime-undeclared"),
    ],
)
def test_only_exact_explicit_model_options_and_runtime_are_declared(selected, evidence, reason):
    result = qualify(selected, discovery=evidence, binding=None)
    assert result.status == "refused"
    assert reason in result.reasons


def test_boolean_and_string_option_values_remain_distinct():
    declared = discovery(models=(ModelDeclaration("launch-model", (("flag", (True,)),)),))
    assert qualify(target(options=(("flag", True),)), discovery=declared).status == "declared"
    assert qualify(target(options=(("flag", "true"),)), discovery=declared).status == "refused"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, "110"])
def test_invalid_now_never_enters_diagnostics(bad):
    with pytest.raises(ValueError, match="^invalid native T3 contract$"):
        qualify(now=bad)


@pytest.mark.parametrize("factory", [discovery, binding])
@pytest.mark.parametrize("field", ["observed_at", "expires_at"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_evidence_requires_finite_supplied_times(factory, field, bad):
    with pytest.raises(ValueError, match="^invalid native T3 contract$"):
        factory(**{field: bad})


@pytest.mark.parametrize("factory", [discovery, binding])
def test_inverted_evidence_interval_is_rejected(factory):
    with pytest.raises(ValueError):
        factory(observed_at=130.0, expires_at=120.0)


def test_replay_is_identical_after_canonicalizing_caller_containers():
    selected = target(options=[("zFlag", True), ["reasoningEffort", "high"]])
    replay = target(options=(("reasoningEffort", "high"), ("zFlag", True)))
    assert compare_intents(intent(target=selected), intent(target=replay)) == "identical"
    assert compare_intents(intent(), intent()) == "identical"


@pytest.mark.parametrize(
    "field,value",
    [
        ("authority_ref", "authority-other"),
        ("work_id", "work-other"),
        ("canonical_repository", "org-test/repo-other"),
        ("source_ref", "refs/heads/other"),
        ("owner_node", "node-other"),
        ("ownership_revision", 3),
        ("generation", 4),
    ],
)
def test_changed_work_and_holder_identity_conflict(field, value):
    original = intent()
    assert compare_intents(original, intent(work=replace(original.work, **{field: value}))) == "conflict"


@pytest.mark.parametrize(
    "field,value", [("run_id", "run-other"), ("request_id", "request-other"), ("attempt_id", "a" * 64)]
)
def test_changed_task_handle_identity_conflicts(field, value):
    original = intent()
    assert compare_intents(original, intent(handle=replace(original.handle, **{field: value}))) == "conflict"


@pytest.mark.parametrize(
    "field,value", [("task_id", "task-two"), ("seat", "worker-other"), ("payload_digest", "b" * 64)]
)
def test_changed_task_spec_and_repeated_seat_assignment_are_distinct(field, value):
    original = intent()
    handle = replace(original.handle, spec=replace(original.handle.spec, **{field: value}))
    assert compare_intents(original, intent(handle=handle)) == "conflict"


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin_environment", "env-other"),
        ("origin_thread", "thread-other"),
        ("target_environment", "env-other"),
        ("target_project", "project-other"),
        ("provider_instance", "provider-other"),
        ("driver", "driver-other"),
        ("launch_model", "model-other"),
        ("options", (("reasoningEffort", "low"),)),
        ("runtime_mode", "runtime-other"),
        ("interaction_mode", "plan"),
        ("isolation", "shared"),
        ("source_revision", "b" * 40),
    ],
)
def test_every_selected_target_field_is_immutable_intent(field, value):
    original = intent()
    assert compare_intents(original, intent(target=target(**{field: value}))) == "conflict"


@pytest.mark.parametrize(
    "changes",
    [
        {"dispatch_attempt": 2},
        {"payload_context_digest": "a" * 64},
        {"batch_binding_ref": "batch-other"},
        {"native_client_request_key": "request-other"},
        {"authority_refs": ()},
        {"authority_refs": (AuthorityReference("decision", "policy-test", 124.0),)},
        {"authority_refs": (AuthorityReference("policy", "policy-other", 124.0),)},
        {"authority_refs": (AuthorityReference("policy", "policy-test", 125.0),)},
        {"authority_refs": (AuthorityReference("policy", "policy-test", None),)},
    ],
)
def test_dispatch_payload_batch_client_key_and_all_authority_fields_conflict(changes):
    assert compare_intents(intent(), intent(**changes)) == "conflict"


def test_mutating_supplied_containers_cannot_change_frozen_values():
    options = [["reasoningEffort", "high"]]
    values = ["high"]
    declarations = [["reasoningEffort", values]]
    models = [ModelDeclaration("launch-model", declarations)]
    modes = ["app-server"]
    refs = [AuthorityReference("policy", "policy-test", None)]
    selected = target(options=options)
    evidence = discovery(models=models, runtime_modes=modes)
    original = intent(target=selected, authority_refs=refs)
    options[0][1] = "low"
    values.append("low")
    declarations.clear()
    models.clear()
    modes.clear()
    refs.clear()
    assert selected.options == (("reasoningEffort", "high"),)
    assert evidence.models[0].options == (("reasoningEffort", ("high",)),)
    assert evidence.runtime_modes == ("app-server",)
    assert original.authority_refs == (AuthorityReference("policy", "policy-test", None),)
    with pytest.raises(FrozenInstanceError):
        original.dispatch_attempt = 2


@pytest.mark.parametrize(
    "factory,changes",
    [
        (target, {"options": (("flag", True), ("flag", False))}),
        (target, {"source_revision": "A" * 40}),
        (target, {"source_revision": "a" * 39}),
        (target, {"isolation": "unknown"}),
        (target, {"interaction_mode": "auto"}),
        (target, {"target_environment": ""}),
        (discovery, {"project": ""}),
        (discovery, {"child_enabled": 1}),
        (discovery, {"models": ["raw-error"]}),
        (binding, {"roster_revision": True}),
        (binding, {"policy_version": True}),
        (binding, {"policy_version": -1}),
        (binding, {"refusal": "raw private provider error"}),
        (intent, {"dispatch_attempt": True}),
        (intent, {"dispatch_attempt": 0}),
        (intent, {"payload_context_digest": "not-a-digest"}),
        (intent, {"handle": TaskHandle("run-test", "request-test", TaskSpec("task-one", "worker-test", "a" * 64))}),
        (intent, {"handle": TaskHandle("", "request-test", TaskSpec("task-one", "worker-test", "a" * 64), "e" * 64)}),
        (
            intent,
            {"authority_refs": [AuthorityReference("policy", "one", None), AuthorityReference("policy", "two", None)]},
        ),
    ],
)
def test_malformed_identity_and_boolean_ordinals_are_bounded_refusals(factory, changes):
    with pytest.raises(ValueError, match="^invalid native T3 contract$"):
        factory(**changes)


@pytest.mark.parametrize(
    "field,bad", [("ownership_revision", True), ("ownership_revision", -1), ("generation", True), ("generation", 0)]
)
def test_work_revisions_are_exact_integers(field, bad):
    with pytest.raises(ValueError):
        replace(intent().work, **{field: bad})


@pytest.mark.parametrize(
    "kind", ["policy", "decision", "delegation", "admission", "reservation", "launch-proof", "budget-dispatch"]
)
def test_recognized_authority_references_are_optional_correlation_only(kind):
    assert intent(authority_refs=[AuthorityReference(kind, "reference-test", None)]).authority_refs[0].kind == kind


@pytest.mark.parametrize("kind", ["unknown", {"raw": "private-error"}])
def test_unknown_authority_kind_is_rejected_without_echo(kind):
    with pytest.raises(ValueError, match="^invalid native T3 contract$"):
        AuthorityReference(kind, "reference-test", None)


@pytest.mark.parametrize("options", [(("flag", (True,)), ("flag", (False,))), (("flag", (1,)),), (("flag", ()),)])
def test_model_option_declarations_reject_ambiguity_or_non_exact_values(options):
    with pytest.raises(ValueError):
        ModelDeclaration("launch-model", options)


@pytest.mark.parametrize(
    "bad", ["secret-sentinel\nraw-error", "x" * 513, {"api_key": "secret-sentinel", "error": "raw-error"}]
)
def test_untrusted_material_never_enters_exceptions_or_diagnostics(bad):
    for construct in (
        lambda: target(provider_instance=bad),
        lambda: target(options=(("reasoningEffort", bad),)),
        lambda: binding(refusal=bad),
        lambda: intent(batch_binding_ref=bad),
        lambda: AuthorityReference("policy", bad, None),
    ):
        with pytest.raises(ValueError) as caught:
            construct()
        assert str(caught.value) == "invalid native T3 contract"
    diagnostic = qualification_diagnostic(qualify(discovery=None, binding=None))
    assert "secret-sentinel" not in str(diagnostic)
    assert "raw-error" not in str(diagnostic)


def test_accepted_reference_bound_is_not_truncated():
    reference = "r" * 512
    assert intent(batch_binding_ref=reference).batch_binding_ref == reference
    assert AuthorityReference("policy", reference, None).reference == reference


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "ready"},
        {"reasons": ("raw-error-secret-sentinel",)},
        {"observed_at": float("nan")},
        {"expires_at": float("inf")},
        {"observed_at": 2.0, "expires_at": 1.0},
    ],
)
def test_diagnostics_accept_only_validated_fixed_tokens_and_times(changes):
    with pytest.raises(ValueError, match="^invalid native T3 contract$"):
        replace(Qualification("unknown", ("discovery-missing",), None, None), **changes)
