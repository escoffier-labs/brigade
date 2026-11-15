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


def test_go_no_go_and_policy_edit_invalidate_admission(tmp_path):
    corpus(tmp_path)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    report = run_eval([case()])
    record = gate.record(tmp_path, report, pair=pair, binding="a" * 64, now_ms=1000)
    assert record["classes"]["correct"]["decision"] == "go"
    assert record["classes"]["supersede"]["decision"] == "no-go"
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1001) == {"correct"}
    policy = json.loads((tmp_path / gate.POLICY).read_bytes())
    policy["precision"] = 1.0
    (tmp_path / gate.POLICY).write_bytes(io.json_bytes(policy))
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1001) == set()
    no_go = gate.record(tmp_path, run_eval([case("no-change")]), pair=pair, binding="a" * 64, now_ms=1002)
    assert no_go["classes"]["correct"]["decision"] == "no-go"
    assert "precision" in no_go["classes"]["correct"]["reasons"]


def test_no_record_never_admits_a_class(tmp_path):
    corpus(tmp_path)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    assert gate.allowed(tmp_path, pair=pair, binding="a" * 64, now_ms=1000) == set()


def test_default_is_enabled_with_explicit_off_switch():
    from brigade.memory_verify_config import parse

    assert parse({"verify": {}}).enabled
    assert not parse({"verify": {"enabled": False}}).enabled


@pytest.mark.parametrize("change", ["corpus", "seat", "hub", "expired", "tampered"])
def test_authority_changes_invalidate_go(tmp_path, change):
    corpus(tmp_path)
    pair = (seat("propose", "test-small"), seat("confirm", "test-large"))
    gate.record(tmp_path, run_eval([case()]), pair=pair, binding="a" * 64, now_ms=1000)
    binding, now = "a" * 64, 1001
    if change == "corpus":
        (tmp_path / gate.CORPUS).write_text("{}")
    elif change == "seat":
        pair = (seat("propose", "new-model"), pair[1])
    elif change == "hub":
        binding = "b" * 64
    elif change == "expired":
        now += 8 * gate.DAY_MS
    else:
        path = tmp_path / gate.RECORD
        value = json.loads(path.read_bytes())
        value["classes"]["supersede"]["decision"] = "go"
        path.write_bytes(io.json_bytes(value))
    assert gate.allowed(tmp_path, pair=pair, binding=binding, now_ms=now) == set()


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
    second = gate.record(tmp_path, report, pair=pair, binding="a" * 64, now_ms=1001)
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

    class Reviewer:
        identity = cmd._hub(target)[1].identity

        def call(self, action, **fields):
            if action == "status":
                return {**hub.do_status({})[1], "principal": "agent:eval-reviewer"}
            return cmd._hub(target)[1].call(action, **fields)

    monkeypatch.setattr(automatic, "_reviewer", lambda target, client: Reviewer())
    applied = []
    hub.do_decide = lambda body: applied.append(body) or (200, {"state": "applied"})
    corpus(target)
    state = state_mod.load(target)
    pair = fake.ready(target, None, None)
    report = run_eval([case()])
    if not admit:
        report["classes"]["confirm"] = report["classes"].pop("correct")
        report["ops"]["append_evidence"] = report["ops"]["replace_block"]
    gate.record(target, report, pair=pair, binding=state["binding"], now_ms=1000)
    assert gate.allowed(target, pair=pair, binding=state["binding"], now_ms=1001) == (
        {"correct"} if admit else {"confirm"}
    )
    result = automatic.apply_pending(target, state, cmd._hub(target)[1], pair=pair, now_ms=1001)
    assert bool(applied) == admit
    assert bool(result) == admit
    if admit:
        assert applied[0]["decision"] == "accept"
        assert "expected_policy_digest" in applied[0]


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


def test_reviewer_credential_comes_from_user_config_not_workspace(verify_hub, monkeypatch):
    from brigade import memory_verify_auto as automatic, memory_verify_cmd as cmd

    target, _, _, home = verify_hub
    path = home / ".brigade/fleet.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    token = home / "reviewer-token"
    path.write_text(f'[memory_verify]\nreview_token_file = "{token}"\n')
    seen = []
    settings = cmd._hub(target)[0]
    monkeypatch.setattr(
        automatic.hubc, "load_settings", lambda target, credential_pair: seen.append(credential_pair) or settings
    )
    verifier = cmd.hubc.client_for(settings)
    assert automatic._reviewer(target, verifier) is not None
    assert seen == [(verifier.hub_url, token)]


def test_scheduled_run_evaluates_and_applies_without_a_person(verify_hub, resolver, capsys, monkeypatch):
    from brigade import memory_verify_auto as automatic, memory_verify_cmd as cmd
    from tests.test_memory_verify_run import verify

    target, hub, _, _ = verify_hub
    evidence_files(target)
    corpus(target)
    hub.bound = "latest"
    original = hub.do_proposal_show
    hub.do_proposal_show = lambda body: (200, {**original(body)[1], "policy_digest": None})

    class Reviewer:
        identity = cmd._hub(target)[1].identity

        def call(self, action, **fields):
            if action == "status":
                return {**hub.do_status({})[1], "principal": "agent:eval-reviewer"}
            return cmd._hub(target)[1].call(action, **fields)

    monkeypatch.setattr(automatic, "_reviewer", lambda *args: Reviewer())
    applied = []
    hub.do_decide = lambda body: applied.append(body) or (200, {"state": "applied"})
    code, report = verify(target, capsys, "--scheduled")
    assert code == 0
    assert report["applications"][0]["state"] == "applied"
    assert applied and automatic.automated(target, applied[0]["proposal_id"])


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


def test_gate_cli_writes_decision_against_current_hub_binding(verify_hub, capsys):
    from brigade import cli

    target, hub, _, _ = verify_hub
    corpus(target)
    assert cli.main(["memory", "verify", "gate", "--target", str(target), "--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["classes"]["correct"]["decision"] == "go"
    assert not [call for call in hub.calls if call["action"] != "status"]
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


def test_unavailable_reviewer_keeps_scheduled_corrections_as_proposals(verify_hub, resolver, capsys, monkeypatch):
    from brigade import memory_verify_auto as automatic, memory_hub_client as hubc
    from tests.test_memory_verify_run import verify

    target, _, _, _ = verify_hub
    evidence_files(target)
    corpus(target)

    def unavailable(*args):
        raise hubc.HubUnavailable("unavailable")

    monkeypatch.setattr(automatic, "_reviewer", unavailable)
    code, report = verify(target, capsys, "--scheduled")
    assert code == 0 and report["proposals"]
    assert report["auto_apply"] == "proposal-only"
