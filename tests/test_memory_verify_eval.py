"""Offline model evaluation and unattended, digest-bound class admission."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

import pytest

from brigade import memory_proposal_io as io
from brigade import memory_verify_eval as evaluation
from brigade import memory_verify_gate as gate
from tests.test_memory_verify_contract import (
    BLOCK,
    CLAIM,
    FIXED,
    RESOLVER,
    FakeSeat,
    block_entry,
    make_card,
    standard,
)
from tests.test_memory_verify_run import verify_hub as verify_hub, resolver as resolver, seat

pytestmark = pytest.mark.skipif(not hasattr(__import__("os"), "O_NOFOLLOW"), reason="descriptor storage required")


def case(label="correct", **changes):
    card = make_card()
    items = []
    for item in standard().items:
        value = asdict(item)
        value.pop("compare")
        value["lineage"] = sorted(value["lineage"])
        value["values"] = list(value["values"])
        items.append(value)
    return {
        "id": "case-one",
        "card": {
            "scope": card.scope,
            "card_id": card.card_id,
            "revision": card.revision,
            "text": f"---\nevidence: []\n---\n{CLAIM} ^{BLOCK}\n",
            "updated_ms": card.updated_ms,
        },
        "evidence": items,
        "commits": RESOLVER,
        "expected": label,
        "expected_ops": [{"type": "replace_block", "block_id": BLOCK, "text": FIXED}] if label == "correct" else [],
        **changes,
    }


def corpus(target, cases=None):
    path = target / gate.CORPUS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(io.json_bytes({"schema": evaluation.CORPUS_SCHEMA, "cases": cases or [case()]}))
    policy = dict(gate.DEFAULT_POLICY, min_cases=1, min_feedback=0, repeats=2, consecutive_passes=1)
    (target / gate.POLICY).write_bytes(io.json_bytes(policy))
    return path


def run_eval(cases, **kwargs):
    return evaluation.evaluate(
        cases,
        propose=FakeSeat(block_entry()),
        confirm=FakeSeat(block_entry()),
        resolve_commit=RESOLVER.get,
        repeats=2,
        **kwargs,
    )


def test_scores_final_ops_and_counts_wrong_replacement_as_false_positive():
    report = run_eval([case(), case("no-change", id="healthy")])
    assert report["classes"]["correct"] == {"tp": 2, "fp": 2, "fn": 0, "support": 2, "precision": 0.5, "recall": 1.0}
    assert report["classes"]["no-change"]["recall"] == 0
    assert report["unsafe_proposals"] == 2
    wrong = case(expected_ops=[{"type": "replace_block", "block_id": BLOCK, "text": "Wrong replacement."}])
    report = run_eval([wrong])
    assert report["classes"]["correct"]["tp"] == 0
    assert report["classes"]["correct"]["fp"] == report["classes"]["correct"]["fn"] == 2


def test_deferred_supersede_is_a_missed_case():
    report = evaluation.evaluate(
        [case("supersede", expected_ops=[{"type": "supersede_card", "block_id": BLOCK, "other_card": "new-card"}])],
        propose=FakeSeat(block_entry(verdict="superseded")),
        confirm=FakeSeat(block_entry()),
        repeats=2,
    )
    assert report["classes"]["supersede"]["recall"] == 0
    assert report["classes"]["supersede"]["fn"] == 2


class AdmissionClient:
    def __init__(self, record, created_ms=1000):
        self.admission = {"record": record, "created_ms": created_ms, "evaluator": "admin"}

    def call(self, action, **fields):
        assert action == "status"
        return {"verify_evaluation": self.admission}


def test_go_no_go_and_policy_edit_invalidate_admission(tmp_path):
    corpus(tmp_path)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    report = run_eval([case()])
    record = gate.record(tmp_path, report, pair=pair, binding="a" * 64, now_ms=1000)
    assert record["classes"]["correct"]["decision"] == "go"
    assert record["classes"]["supersede"]["decision"] == "no-go"
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1001, client=AdmissionClient(record)) == {
        "correct"
    }
    policy = json.loads((tmp_path / gate.POLICY).read_bytes())
    policy["precision"] = 1.0
    (tmp_path / gate.POLICY).write_bytes(io.json_bytes(policy))
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1001, client=AdmissionClient(record)) == set()
    no_go = gate.record(tmp_path, run_eval([case("no-change")]), pair=pair, binding="a" * 64, now_ms=1002)
    assert no_go["classes"]["correct"]["decision"] == "no-go"
    assert "precision" in no_go["classes"]["correct"]["reasons"]


def test_no_record_never_admits_a_class(tmp_path):
    corpus(tmp_path)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1000) == set()


def test_upgrade_requires_explicit_enable_or_versioned_new_workspace_marker():
    from brigade.memory_verify_config import parse

    assert not parse({"verify": {}}).enabled
    assert not parse({}).enabled
    assert parse({"verify": {"new_workspace_version": 1}}).enabled
    assert parse({"verify": {"enabled": True}}).enabled
    assert not parse({"verify": {"enabled": False}}).enabled


@pytest.mark.parametrize("change", ["corpus", "seat", "hub", "expired"])
def test_authority_changes_invalidate_go(tmp_path, change):
    corpus(tmp_path)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    record = gate.record(tmp_path, run_eval([case()]), pair=pair, binding="a" * 64, now_ms=1000)
    binding, now = "a" * 64, 1001
    if change == "corpus":
        (tmp_path / gate.CORPUS).write_text("{}")
    elif change == "seat":
        pair = (seat("propose", "new-model"), pair[1])
    elif change == "hub":
        binding = "b" * 64
    elif change == "expired":
        now += 8 * gate.DAY_MS
    assert gate.allowed(tmp_path, pair=pair, binding=binding, now_ms=now, client=AdmissionClient(record)) == set()


def test_two_consecutive_passing_runs_and_failed_eval_revoke_go(tmp_path):
    corpus(tmp_path)
    policy_path = tmp_path / gate.POLICY
    policy = json.loads(policy_path.read_bytes())
    policy["consecutive_passes"] = 2
    policy_path.write_bytes(io.json_bytes(policy))
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    report = run_eval([case()])
    first = gate.record(tmp_path, report, pair=pair, binding="a" * 64, now_ms=1000)
    assert first["classes"]["correct"]["reasons"] == ["consecutive_passes"]
    second = gate.record(
        tmp_path, report, pair=pair, binding="a" * 64, now_ms=1001, previous=AdmissionClient(first).admission
    )
    assert second["classes"]["correct"]["decision"] == "go"
    gate.record(tmp_path, {"status": "failed"}, pair=pair, binding="a" * 64, now_ms=1002)
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1003) == set()


def evidence_files(target):
    from brigade import memory_verify_config as config
    from tests.test_memory_verify_contract import HANDOFF_PATH, HANDOFF_TEXT, RECEIPT_PATH, receipt_payload

    root = Path(config.load(target).workspaces[0].resolved)
    for name, text in [(RECEIPT_PATH, json.dumps(receipt_payload())), (HANDOFF_PATH, HANDOFF_TEXT)]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.mark.parametrize("admit", [True, False])
def test_mutation_unadmitted_class_cannot_apply(verify_hub, resolver, capsys, monkeypatch, admit):
    """Changing the admitted class to confirm must kill automatic replace_block."""
    from brigade import memory_verify_auto as automatic, memory_verify_state as state_mod
    from brigade import memory_verify_cmd as cmd
    from tests.test_memory_verify_run import verify

    target, hub, fake, _ = verify_hub
    evidence_files(target)
    assert verify(target, capsys)[0] == 0
    hub.bound = "latest"
    original_show = hub.do_proposal_show
    hub.do_proposal_show = lambda body: (200, {**original_show(body)[1], "policy_digest": None})

    applied = []
    hub.do_verify_auto_apply = lambda body: (
        applied.append(body) or (200, {"state": "applied", "decision_mode": "automatic"})
    )
    corpus(target)
    state = state_mod.load(target)
    pair = fake.ready(target, None, None)
    report = run_eval([case()])
    if not admit:
        report["classes"]["confirm"] = report["classes"].pop("correct")
        report["ops"]["append_evidence"] = report["ops"]["replace_block"]
    record = gate.record(target, report, pair=pair, binding=state["binding"], now_ms=1000)
    old_status = hub.do_status
    hub.do_status = lambda body: (200, {**old_status(body)[1], **AdmissionClient(record).call("status")})
    assert gate.allowed(target, pair=pair, binding=state["binding"], now_ms=1001, client=cmd._hub(target)[1]) == (
        {"correct"} if admit else {"confirm"}
    )
    result = automatic.apply_pending(target, state, cmd._hub(target)[1], pair=pair, now_ms=1001)
    assert bool(applied) == admit
    assert bool(result) == admit
    if admit:
        assert applied[0]["action"] == "verify_auto_apply"
        assert applied[0]["expected_op_types"] == ["replace_block", "append_evidence"]
        assert "binding_sha256" in applied[0]


def test_feedback_packet_becomes_a_private_labeled_case(verify_hub, resolver, capsys):
    from brigade import memory_verify_feedback as feedback
    from tests.test_memory_verify_run import verify

    target, hub, _, _ = verify_hub
    _, result = verify(target, capsys)
    ident = result["proposals"][0]["proposal_id"]
    hub.bound = "latest"
    shown = hub.do_proposal_show({"proposal_id": ident})[1]
    feedback.retain_case(target, ident, "rejected", shown)
    saved = json.loads((target / gate.FEEDBACK).read_bytes())
    assert saved["cases"][0]["expected"] == "no-change"
    assert saved["cases"][0]["expected_ops"] == []
    assert saved["counts"] == {"correct": {"accepted": 0, "rejected": 1}}
    assert (target / gate.FEEDBACK).stat().st_mode & 0o077 == 0
    feedback.retain_case(target, ident, "rejected", shown)
    assert len(json.loads((target / gate.FEEDBACK).read_bytes())["cases"]) == 1


def test_failed_run_never_attempts_automatic_application(verify_hub, capsys, monkeypatch):
    from brigade import memory_verify_auto as automatic
    from tests.test_memory_verify_run import verify

    target, _, fake, _ = verify_hub
    fake.status = "seat_timeout"

    def forbidden(*args, **kwargs):
        pytest.fail("failed run reached apply")

    monkeypatch.setattr(automatic, "apply_pending", forbidden)
    assert verify(target, capsys)[0] != 0


def test_seeded_corpus_scores_production_contract_without_a_hub():
    import re

    cases = evaluation.read_corpus(Path(__file__).parent / "fixtures/memory_verify_eval")

    def scripted(prompt):
        ident = re.search(r"card_id: (\S+)\n", prompt)[1]
        group = ident.split("-")[0]
        verdict = {"correct": "supported", "supersede": "superseded", "history": "history"}.get(group, "contradicted")
        if group == "adversarial":
            return FakeSeat(block_entry(verdict="insufficient", quotes=()), card_id=ident)(prompt)
        return FakeSeat(block_entry(verdict=verdict), card_id=ident)(prompt)

    report = evaluation.evaluate(cases, propose=scripted, confirm=scripted, repeats=2)
    assert report["cases"] == 70
    assert report["classes"]["correct"]["precision"] == 1
    assert report["classes"]["correct"]["recall"] == 1
    assert report["classes"]["confirm"]["recall"] == 1
    assert report["classes"]["supersede"]["recall"] == 0
    assert report["classes"]["history"]["recall"] == 0
    assert report["unsafe_proposals"] == 0


def test_forged_local_go_record_admits_nothing(tmp_path):
    corpus(tmp_path)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    record = gate.record(tmp_path, run_eval([case()]), pair=pair, binding="a" * 64, now_ms=1000)
    for row in record["classes"].values():
        row["decision"] = "go"
    record.pop("digest")
    record["digest"] = io.sha256(io.json_bytes(record))
    (tmp_path / gate.RECORD).write_bytes(io.json_bytes(record))
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1001) == set()
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1001, client=AdmissionClient({})) == set()


def test_scheduled_run_never_evaluates_or_uses_human_decide(verify_hub, resolver, capsys, monkeypatch):
    from tests.test_memory_verify_run import verify

    target, hub, _, _ = verify_hub
    evidence_files(target)
    corpus(target)

    def forbidden(*args, **kwargs):
        pytest.fail("scheduler reached evaluator or human decide")

    monkeypatch.setattr(gate, "run", forbidden)
    hub.do_decide = forbidden
    hub.do_status = (lambda original: lambda body: (200, {**original(body)[1], "verify_evaluation": None}))(
        hub.do_status
    )
    code, report = verify(target, capsys, "--scheduled")
    assert code == 0
    assert report["applications"] == []


def test_missing_corpus_writes_no_go_without_model_calls(verify_hub):
    target, _, fake, _ = verify_hub
    pair = fake.ready(target, None, None)
    record = gate.run(target, pair=pair, binding="a" * 64, now_ms=1000)
    assert all(row["decision"] == "no-go" for row in record["classes"].values())
    assert record["report"]["code"] == "verify_eval_unavailable"
    assert fake.calls == []


def test_eval_cli_is_offline_and_reports_operation_metrics(verify_hub, capsys):
    from brigade import cli

    target, hub, _, _ = verify_hub
    path = corpus(target)
    assert cli.main(["memory", "verify", "eval", "--corpus", str(path), "--target", str(target), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ops"]["replace_block"]["precision"] == 1.0
    assert report["ops"]["append_evidence"]["support"] == 0
    assert hub.calls == []


def test_gate_cli_writes_decision_against_current_hub_binding(verify_hub, capsys, monkeypatch):
    from brigade import cli

    target, hub, _, _ = verify_hub
    corpus(target)
    from brigade import memory_verify_cmd as cmd

    client = cmd._hub(target)[1]
    monkeypatch.setattr(gate, "evaluator", lambda *args: client)
    setattr(hub, "do_verify_eval.record", lambda body: (201, {"verify_evaluation": body["record"]}))
    assert cli.main(["memory", "verify", "gate", "--target", str(target), "--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["classes"]["correct"]["decision"] == "go"
    published = [call for call in hub.calls if call["action"] == "verify_eval.record"]
    assert published
    assert io.sha256(published[0]["policy"].encode()) == published[0]["record"]["policy_sha256"]
    assert (target / gate.RECORD).is_file()


def test_invalid_model_output_never_attempts_application(verify_hub, capsys, monkeypatch):
    from brigade import memory_verify_auto as automatic, memory_verify_run as runner, memory_verify_seat as seats
    from tests.test_memory_verify_run import verify

    target, _, _, _ = verify_hub
    monkeypatch.setattr(runner, "CALL", lambda *args, **kwargs: seats.Reply("ok", text="{}"))

    def forbidden(*args, **kwargs):
        pytest.fail("invalid model output reached apply")

    monkeypatch.setattr(automatic, "apply_pending", forbidden)
    _, report = verify(target, capsys)
    assert report["proposals"] == []


@pytest.mark.parametrize("value", [[], None, "invalid", {"digest": "bad"}])
def test_invalid_record_shape_is_no_admission(tmp_path, value):
    corpus(tmp_path)
    path = tmp_path / gate.RECORD
    path.write_bytes(io.json_bytes(value))
    path.chmod(0o600)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1000) == set()


def test_failed_auto_call_does_not_mark_automatic(verify_hub, resolver, capsys, monkeypatch):
    from brigade import memory_verify_auto as automatic, memory_verify_cmd as cmd, memory_verify_state as state_mod
    from tests.test_memory_verify_run import verify

    target, hub, fake, _ = verify_hub
    evidence_files(target)
    assert verify(target, capsys)[0] == 0
    hub.bound = "latest"
    original = hub.do_proposal_show
    hub.do_proposal_show = lambda body: (200, {**original(body)[1], "policy_digest": None})
    monkeypatch.setattr(gate, "allowed", lambda *args, **kwargs: {"correct"})
    hub.do_verify_auto_apply = lambda body: (409, {"error": "memory_verify_auto_held"})
    state = state_mod.load(target)
    result = automatic.apply_pending(
        target, state, cmd._hub(target)[1], pair=fake.ready(target, None, None), now_ms=1000
    )
    assert result[0]["state"] == "proposal-only"
    assert not (target / automatic.JOURNAL).exists()
    assert not automatic.automated({"state": "rejected"})
    assert automatic.automated({"state": "applied", "decision_mode": "automatic"})


def test_enable_preserves_existing_configuration(verify_hub, capsys):
    from brigade import cli, memory_verify_config as config

    target, _, _, _ = verify_hub
    path = target / config.CONFIG
    before = path.read_text().replace("enabled = true", "# enabled absent")
    path.write_text(before)
    assert not config.load(target).enabled
    assert cli.main(["memory", "verify", "enable", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    assert config.load(target).enabled
    assert config.parse(__import__("tomllib").loads(before)).workspaces == config.load(target).workspaces


def test_replay_preserves_comparison_bytes():
    import base64
    from brigade import memory_verify_evidence as ev

    value = case()
    shared = (
        b"copied receipt output repeated in both independent-looking sources with enough bytes to detect dependency" * 5
    )
    for evidence in value["evidence"]:
        evidence["compare_base64"] = base64.b64encode(shared).decode()
    _, assembly = evaluation.inputs(value)
    assert all(item.compare == shared for item in assembly.items)
    assert not ev.independent(*assembly.items[:2])


@pytest.mark.parametrize("automatic_decision", [True, False])
@pytest.mark.parametrize("journal", ["forged", "deleted", "corrupt"])
def test_feedback_uses_hub_mode_despite_local_journal(verify_hub, resolver, capsys, automatic_decision, journal):
    from brigade import memory_verify_auto as automatic, memory_verify_cmd as cmd, memory_verify_run as runner
    from brigade import memory_verify_state as state_mod
    from tests.test_memory_verify_run import verify

    target, hub, _, _ = verify_hub
    assert verify(target, capsys)[0] == 0
    state = state_mod.load(target)
    ident = next(
        entry["pending"]
        for table in state["scopes"].values()
        for entry in table["cards"].values()
        if entry.get("pending")
    )
    hub.bound = "latest"
    original = hub.do_proposal_show
    hub.do_proposal_show = lambda body: (
        200,
        {
            **original(body)[1],
            "state": "applied" if automatic_decision else "rejected",
            "decision_mode": "automatic" if automatic_decision else None,
        },
    )
    path = target / automatic.JOURNAL
    if journal != "deleted":
        path.write_bytes(io.json_bytes({ident: {}}) if journal == "forged" else b"corrupt")
        path.chmod(0o600)
    updates, failed = runner._refresh(target, cmd._hub(target)[1], state, frozenset(state["scopes"]))
    assert failed == 0 and updates
    if automatic_decision:
        assert not (target / gate.FEEDBACK).exists()
        assert all("review" not in entry for entry in updates.values())
    else:
        saved = json.loads((target / gate.FEEDBACK).read_bytes())
        assert saved["counts"] == {"correct": {"accepted": 0, "rejected": 1}}


def test_implementation_change_revokes_hub_admission(tmp_path, monkeypatch):
    corpus(tmp_path)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    record = gate.record(tmp_path, run_eval([case()]), pair=pair, binding="a" * 64, now_ms=1000)
    client = AdmissionClient(record)
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1001, client=client) == {"correct"}
    original = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: original(path) + (b"# changed" if path.name == "memory_verify_evidence.py" else b""),
    )
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1001, client=client) == set()


def test_new_memory_config_marker_is_not_added_on_force_upgrade(tmp_path, capsys):
    from brigade import memory_cmd, memory_verify_config as config

    assert memory_cmd.init(target=tmp_path, update_gitignore=False) == 0
    capsys.readouterr()
    assert config.load(tmp_path).enabled
    assert "new_workspace_version = 1" in (tmp_path / config.CONFIG).read_text()
    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False) == 0
    capsys.readouterr()
    assert not config.load(tmp_path).enabled


def test_forged_local_streak_does_not_advance_evaluator_admission(tmp_path):
    corpus(tmp_path)
    path = tmp_path / gate.POLICY
    policy = json.loads(path.read_bytes())
    policy["consecutive_passes"] = 2
    path.write_bytes(io.json_bytes(policy))
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    report = run_eval([case()])
    gate.record(tmp_path, report, pair=pair, binding="a" * 64, now_ms=1000)
    second = gate.record(tmp_path, report, pair=pair, binding="a" * 64, now_ms=1001)
    assert second["classes"]["correct"]["decision"] == "no-go"
    assert second["classes"]["correct"]["streak"] == 1


def test_feedback_is_bounded_by_serialized_size():
    from brigade import memory_verify_feedback as feedback

    def case(index, size):
        return {"id": f"p{index}", "source_class": "correct", "decision": "applied", "blob": "x" * size}

    cases = [case(i, 1000) for i in range(10)]
    payload = feedback.bounded_feedback(cases, 4500)
    assert payload is not None and len(payload) <= 4500
    kept = json.loads(payload)["cases"]
    assert kept and kept[-1]["id"] == "p9" and len(kept) < 10
    assert json.loads(payload)["counts"]["correct"]["accepted"] == len(kept)
    assert feedback.bounded_feedback([case(0, 5000)], 4500) is None
    assert feedback.MAX_FEEDBACK_BYTES <= feedback.io.MAX_INPUT_BYTES


def test_unreplayable_feedback_case_does_not_stop_runs(tmp_path, monkeypatch):
    from brigade import memory_verify_contract as contract
    from brigade import memory_verify_feedback as feedback
    from brigade import memory_verify_show as show

    def abstain(*_args, **_kwargs):
        raise contract.Abstain("verify_card_invalid")

    monkeypatch.setattr(show, "_local", abstain)
    feedback.retain_case(tmp_path, "proposal-1", "applied", {"verifier_manifests": []})
    assert not (tmp_path / gate.FEEDBACK).exists()


def test_unparseable_corpus_card_writes_no_go(verify_hub, monkeypatch):
    from brigade import memory_verify_contract as vc

    target, _, fake, _ = verify_hub
    pair = fake.ready(target, None, None)
    corpus(target)

    def unanchored(*_args, **_kwargs):
        raise vc.Abstain("verify_card_invalid")

    monkeypatch.setattr(gate.evaluation, "read_corpus", unanchored)
    record = gate.run(target, pair=pair, binding="a" * 64, now_ms=1000)
    assert all(row["decision"] == "no-go" for row in record["classes"].values())
    assert record["report"]["code"] == "verify_eval_unavailable"


def test_enable_recognizes_a_commented_verify_header(tmp_path):
    import tomllib

    from brigade import memory_verify_config as config

    path = tmp_path / config.CONFIG
    path.parent.mkdir(parents=True)
    path.write_text("[verify] # Verification settings\nenabled = false\n")
    config.enable(tmp_path)
    text = path.read_text()
    assert text.count("[verify]") == 1
    assert tomllib.loads(text)["verify"]["enabled"] is True


def test_binding_reads_corpus_with_the_corpus_reader_limit(tmp_path, monkeypatch):
    seen = {}
    real = gate.io.read_bytes

    def spy(path, *args, **kwargs):
        seen[path.name] = kwargs.get("limit", gate.io.MAX_INPUT_BYTES)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(gate.io, "read_bytes", spy)
    monkeypatch.setattr(gate, "policy", lambda _target: ({}, "d" * 64))
    try:
        gate.binding_digest(tmp_path, ("a", "b"), "c" * 64)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # Placeholder seats fail after both reads, which is all this test needs.
    assert seen.get(Path(gate.CORPUS).name) == gate.io.MAX_STATE_BYTES


def test_eval_cli_reports_a_stopped_seat(verify_hub, capsys, monkeypatch):
    from brigade import cli, memory_verify_run

    target, _, _, _ = verify_hub
    path = corpus(target)

    def stop(self, seat):
        raise memory_verify_run.SeatStop("seat_quota", fatal=False)

    monkeypatch.setattr(memory_verify_run.Calls, "seat", stop)
    code = cli.main(["memory", "verify", "eval", "--corpus", str(path), "--target", str(target), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code != 0
    assert report.get("code") == "seat_quota"
