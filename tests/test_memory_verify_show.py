"""Reviewer hash chain and text-free decision feedback for verify slice 10."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli
from brigade import memory_proposal_io as io
from brigade import memory_verify_packets as pk
from brigade import memory_verify_run as run_mod
from brigade import memory_verify_state as vstate
from tests.test_memory_verify_run import (
    CARD_ID,
    SCOPE,
    entry_for,
    state_of,
    verify,
    writes,
)
from tests.test_memory_verify_run import pytestmark as pytestmark
from tests.test_memory_verify_run import resolver as resolver
from tests.test_memory_verify_run import verify_hub as verify_hub


def proposal(verify_hub, capsys):
    target, hub, _, _ = verify_hub
    code, report = verify(target, capsys)
    assert code == 0
    hub.bound = "latest"
    return report["proposals"][0]["proposal_id"]


def show(target, ident, capsys, *, json_output=True):
    flags = ["--json"] if json_output else []
    code = cli.main(["memory", "verify", "show", ident, "--target", str(target), *flags])
    output = capsys.readouterr()
    return code, output


@pytest.mark.parametrize("tamper", ["manifest", "packet", "packet_and_local_hash"])
def test_tampering_prints_no_evidence(verify_hub, capsys, resolver, tamper):
    target, _, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    manifest_path = target / pk.PACKETS / f"{ident}.manifest.json"
    packet_path = target / pk.PACKETS / f"{ident}.json"
    if tamper == "manifest":
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    else:
        packet_path.write_bytes(packet_path.read_bytes() + b" ")
        if tamper == "packet_and_local_hash":
            manifest = json.loads(manifest_path.read_bytes())
            manifest["packet_sha256"] = io.sha256(packet_path.read_bytes())
            manifest_path.write_bytes(io.json_bytes(manifest))
    code, output = show(target, ident, capsys)
    assert code == 1
    report = json.loads(output.out)
    assert report["code"] == "verify_review_mismatch"
    assert set(report) == {"status", "code", "reason"}
    assert "Zanzibar" not in output.out + output.err


def test_without_review_the_command_refuses_before_reading_local_evidence(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    hub.bound = None
    code, output = show(target, ident, capsys)
    assert code == 1 and json.loads(output.out)["code"] == "verify_review_forbidden"
    assert "Zanzibar" not in output.out + output.err


@pytest.mark.parametrize("manifest_state", ["pending_review", "active", "rejected", "expired", None])
def test_show_requires_an_applicable_manifest_state(verify_hub, capsys, resolver, monkeypatch, manifest_state):
    target, hub, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    original = hub.do_proposal_show

    def shown(body):
        status, reply = original(body)
        reply["verifier_manifests"][0]["state"] = manifest_state
        return status, reply

    monkeypatch.setattr(hub, "do_proposal_show", shown)
    code, output = show(target, ident, capsys)
    if manifest_state in ("pending_review", "active"):
        assert code == 0 and json.loads(output.out)["evidence"]
    else:
        assert code == 1
        report = json.loads(output.out)
        assert report["code"] == "verify_review_mismatch"
        assert set(report) == {"status", "code", "reason"}
        assert "Zanzibar" not in output.out + output.err


def test_headers_precede_quotes_and_json_has_the_same_review_fields(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    before = (target / vstate.STATE).read_bytes()
    calls, sent = len(fake.calls), len(writes(hub))
    code, output = show(target, ident, capsys)
    assert code == 0
    report = json.loads(output.out)
    assert report["proposal_id"] == ident
    assert report["evidence"][0]["authenticated"] is True
    assert report["evidence"][0]["counted"] is True
    assert report["evidence"][0]["findings"] == []
    assert any(item["quotes"] for item in report["evidence"])
    code, terminal = show(target, ident, capsys, json_output=False)
    assert code == 0
    assert terminal.out.index("rank=") < terminal.out.index('"commit":')
    assert terminal.out.index("authenticated=") < terminal.out.index('"commit":')
    assert terminal.out.index("findings=") < terminal.out.index('"commit":')
    assert "Zanzibar" not in output.err + terminal.err
    assert before == (target / vstate.STATE).read_bytes()
    assert len(fake.calls) == calls and len(writes(hub)) == sent


@pytest.mark.parametrize("decision", ["applied", "rejected"])
def test_decisions_count_once_per_verdict_without_text(verify_hub, capsys, resolver, decision):
    target, hub, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    hub.shown[ident] = decision
    assert verify(target, capsys)[0] == 0
    label = "accepted" if decision == "applied" else "rejected"
    expected = {"contradicted": {"accepted": int(label == "accepted"), "rejected": int(label == "rejected")}}
    assert state_of(target)["decisions"] == expected
    assert verify(target, capsys)[0] == 0
    assert state_of(target)["decisions"] == expected
    raw = (target / vstate.STATE).read_text()
    assert "Zanzibar" not in raw and CARD_ID not in raw
    assert "pending" not in entry_for(target, CARD_ID)
    if decision == "rejected":
        assert entry_for(target, CARD_ID)["verdict"] == "rejected_contradicted"


def test_rejected_revision_and_evidence_are_not_proposed_again(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    hub.shown[ident] = "rejected"
    assert verify(target, capsys)[0] == 0
    state = state_of(target)
    from brigade import memory_fairness as fairness

    entry = state["scopes"][SCOPE]["cards"][fairness.key_digest(SCOPE, [CARD_ID])]
    entry.pop("verified", None)
    # Exercise the feedback guard independently of the submission ledger's deduplication.
    state["submissions"] = {}
    vstate.save(target, state)
    hub.calls.clear()
    fake.calls.clear()
    code, report = verify(target, capsys)
    assert code == 0 and report["skipped"]["verify_rejected_unchanged"] == 1
    assert writes(hub) == [] and fake.calls == []


def test_unchanged_rejection_does_not_starve_another_stale_card(verify_hub, capsys, resolver):
    from brigade import memory_fairness as fairness

    target, hub, fake, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    hub.shown[ident] = "rejected"
    assert verify(target, capsys)[0] == 0
    today = entry_for(target, CARD_ID)["verified"]
    state = state_of(target)
    cards = state["scopes"][SCOPE]["cards"]
    rejected = cards[fairness.key_digest(SCOPE, [CARD_ID])]
    rejected["verified"] = today - 60
    cards[fairness.key_digest(SCOPE, ["a"])]["verified"] = today - 30
    vstate.save(target, state)
    config_path = target / ".brigade/memory-care.toml"
    config_path.write_text(config_path.read_text().replace("[verify]\n", "[verify]\ncards_per_run = 1\n"))
    # The other stale card has checkable evidence and requires the model seats.
    hub._commit("card.put", SCOPE, "a", hub.cards[(SCOPE, CARD_ID)]["text"], "live", run_mod.hubc.event_id())
    hub.calls.clear()
    fake.calls.clear()

    code, first = verify(target, capsys)
    assert code == 0 and first["skipped"]["verify_rejected_unchanged"] == 1
    assert first["cards"] == [] and first["proposals"] == []
    assert writes(hub) == [] and fake.calls == []
    code, second = verify(target, capsys)
    assert code == 0
    assert [card["card_id"] for card in second["cards"]] == ["a"]
    assert second["calls"]["made"] > 0
    assert entry_for(target, "a")["verified"] == today
    saved = entry_for(target, CARD_ID)
    assert saved["verified"] == today
    assert saved["rejected"] == rejected["rejected"]
    assert saved["verdict"] == "rejected_contradicted"


@pytest.mark.parametrize("change", ["none", "changed", "appended", "missing", "symlink"])
def test_cited_files_are_resolved_under_the_bound_root(verify_hub, capsys, resolver, change):
    from brigade import memory_verify_config as config
    from tests.test_memory_verify_contract import HANDOFF_PATH, HANDOFF_TEXT, RECEIPT_PATH, receipt_payload

    target, _, _, _ = verify_hub
    root = Path(config.load(target).workspaces[0].resolved)
    receipt = root / RECEIPT_PATH
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps(receipt_payload()))
    handoff = root / HANDOFF_PATH
    handoff.parent.mkdir(parents=True)
    handoff.write_text(HANDOFF_TEXT)
    ident = proposal(verify_hub, capsys)
    if change == "changed":
        handoff.write_text("Changed evidence.\n")
    elif change == "appended":
        handoff.write_text(HANDOFF_TEXT + "New evidence after the original excerpt.\n")
    elif change == "missing":
        handoff.unlink()
    elif change == "symlink":
        elsewhere = target / "unbound.txt"
        elsewhere.write_text(HANDOFF_TEXT)
        handoff.unlink()
        handoff.symlink_to(elsewhere)
    code, output = show(target, ident, capsys)
    assert code == 0
    evidence = json.loads(output.out)["evidence"]
    assert evidence[0]["file_status"] == "current"
    expected = {
        "none": "current",
        "changed": "changed",
        "appended": "changed",
        "missing": "missing",
        "symlink": "missing",
    }
    assert next(item for item in evidence if item["ref"] == HANDOFF_PATH)["file_status"] == expected[change]


@pytest.mark.parametrize("change", ["revision", "evidence"])
def test_changed_inputs_allow_a_new_reviewed_proposal(verify_hub, capsys, resolver, monkeypatch, change):
    from brigade import memory_fairness as fairness
    from tests.test_memory_verify_contract import HANDOFF_TEXT, handoff_item
    from tests.test_memory_verify_run import assemble

    target, hub, fake, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    hub.shown[ident] = "rejected"
    assert verify(target, capsys)[0] == 0
    if change == "revision":
        head = hub.cards[(SCOPE, CARD_ID)]
        hub._commit("card.put", SCOPE, CARD_ID, head["text"], "live", run_mod.hubc.event_id())
    else:

        def changed(heads, settings, archive, **kwargs):
            for assembly in assemble(heads, settings, archive, **kwargs):
                if assembly.items:
                    assembly.items[-1] = handoff_item(HANDOFF_TEXT + "A later check agreed.\n")
                yield assembly

        monkeypatch.setattr(run_mod, "ASSEMBLE", changed)
    state = state_of(target)
    state["scopes"][SCOPE]["cards"][fairness.key_digest(SCOPE, [CARD_ID])].pop("verified", None)
    vstate.save(target, state)
    hub.calls.clear()
    fake.calls.clear()
    code, report = verify(target, capsys)
    assert code == 0 and len(report["proposals"]) == 1
    assert report["proposals"][0]["proposal_id"] != ident
    assert [call["action"] for call in writes(hub)] == ["evidence.attach", "propose"]


def test_settled_manifest_and_packet_expire_together(verify_hub, capsys, resolver):
    import stat
    import time

    target, hub, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    manifest = target / pk.PACKETS / f"{ident}.manifest.json"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    hub.shown[ident] = "applied"
    assert verify(target, capsys)[0] == 0
    code, output = show(target, ident, capsys)
    assert code == 0 and json.loads(output.out)["proposal_id"] == ident
    assert len(list((target / pk.PACKETS).glob(f"{ident}.t*.json"))) == 2
    pk.maintain(target, set(), int(time.time() * 1000) + 15 * pk.DAY_MS)
    assert not list((target / pk.PACKETS).glob(f"{ident}*.json"))


def test_dry_run_never_persists_decisions(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    before = (target / vstate.STATE).read_bytes()
    hub.shown[ident] = "rejected"
    assert verify(target, capsys, "--dry-run")[0] == 0
    assert (target / vstate.STATE).read_bytes() == before


def test_dry_run_skips_a_new_rejection_like_a_real_run(verify_hub, capsys, resolver):
    from brigade import memory_fairness as fairness

    target, hub, fake, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    hub.shown[ident] = "rejected"
    state = state_of(target)
    state["scopes"][SCOPE]["cards"][fairness.key_digest(SCOPE, [CARD_ID])].pop("verified")
    vstate.save(target, state)
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    hub.calls.clear()
    fake.calls.clear()

    code, dry = verify(target, capsys, "--dry-run")
    assert code == 0
    after = {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    assert before == after
    assert writes(hub) == []
    assert fake.calls == []
    code, real = verify(target, capsys)
    assert code == 0
    assert dry["skipped"]["verify_rejected_unchanged"] == real["skipped"]["verify_rejected_unchanged"] == 1
    assert dry["calls"] == real["calls"] == {"made": 0, "ok": 0}
    assert dry["cards"] == real["cards"] == []
    assert dry["proposals"] == real["proposals"] == []
    assert writes(hub) == [] and fake.calls == []
    assert state_of(target)["decisions"]["contradicted"]["rejected"] == 1


@pytest.mark.parametrize("field", ["review", "rejected", "decisions"])
@pytest.mark.parametrize(
    "value", [None, "PrivateWords", {"text": "PrivateWords"}, {"contradicted": {"accepted": True, "rejected": 0}}]
)
def test_feedback_state_rejects_free_text_and_malformed_counters(tmp_path, field, value):
    from brigade import memory_fairness as fairness

    state = vstate.fresh()
    if field == "decisions":
        state[field] = value
    else:
        state["scopes"] = {
            SCOPE: {"cards": {fairness.key_digest(SCOPE, [CARD_ID]): {"seen": 1, "proposed": 0, field: value}}}
        }
    with pytest.raises(vstate.VerifyStateError) as error:
        vstate.save(tmp_path, state)
    assert "PrivateWords" not in str(error.value)


def test_a_legacy_pending_packet_still_records_its_rejection(verify_hub, capsys, resolver):
    from brigade import memory_fairness as fairness

    target, hub, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    state = state_of(target)
    entry = state["scopes"][SCOPE]["cards"][fairness.key_digest(SCOPE, [CARD_ID])]
    entry.pop("review")
    vstate.save(target, state)
    hub.shown[ident] = "rejected"
    assert verify(target, capsys)[0] == 0
    assert state_of(target)["decisions"]["contradicted"]["rejected"] == 1
    assert entry_for(target, CARD_ID)["rejected"]["evidence"] == entry["evidence"]


def test_manifest_finalization_recovers_after_an_interrupted_write(verify_hub, capsys, resolver, monkeypatch):
    target, hub, _, _ = verify_hub
    original = pk.atomic_write
    failed = False

    def interrupt(fd, name, data):
        nonlocal failed
        if name.endswith(".manifest.json") and not failed:
            failed = True
            raise OSError("private detail")
        original(fd, name, data)

    monkeypatch.setattr(pk, "atomic_write", interrupt)
    code, report = verify(target, capsys)
    assert code == 1 and "private detail" not in json.dumps(report)
    assert list((target / pk.PACKETS).glob("staged-*.json"))
    assert verify(target, capsys)[0] == 0
    hub.bound = "latest"
    ident = entry_for(target, CARD_ID)["pending"]
    code, output = show(target, ident, capsys)
    assert code == 0 and json.loads(output.out)["evidence"]
    assert not list((target / pk.PACKETS).glob("staged-*.json"))


def test_manifest_mismatch_discards_the_stage_without_overwriting_artifacts(verify_hub, capsys, resolver, monkeypatch):
    target, hub, _, _ = verify_hub
    conflicting = b'{"other": "manifest"}\n'

    def conflict(target, staged, proposal_id):
        with pk.directory(target) as fd:
            pk.atomic_write(fd, f"{proposal_id}.manifest.json", conflicting)
        return pk.finalize(target, staged, proposal_id)

    monkeypatch.setattr(run_mod, "FINALIZE", conflict)
    code, report = verify(target, capsys)
    assert code == 0 and report["skipped"][pk.MISMATCH] == 1
    ident = entry_for(target, CARD_ID)["pending"]
    root = target / pk.PACKETS
    assert (root / f"{ident}.manifest.json").read_bytes() == conflicting
    assert not (root / f"{ident}.json").exists()
    assert not list(root.glob("staged-*.json"))
    sent = len(writes(hub))
    code, again = verify(target, capsys)
    assert code == 0 and pk.MISMATCH not in again["skipped"]
    assert len(writes(hub)) == sent


def test_show_writes_no_text_to_any_local_file(verify_hub, capsys, resolver):
    target, _, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    assert show(target, ident, capsys)[0] == 0
    after = {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    assert before == after


def test_dropped_findings_are_shown_before_quotes(verify_hub, capsys, resolver, monkeypatch):
    from brigade import memory_verify_evidence as ev
    from tests.test_memory_verify_run import assemble

    target, _, _, _ = verify_hub

    def flagged(heads, settings, archive, **kwargs):
        for assembly in assemble(heads, settings, archive, **kwargs):
            assembly.flagged.append(ev.Flag("cited", 3, ev.HANDOFF, ("injection_test",)))
            yield assembly

    monkeypatch.setattr(run_mod, "ASSEMBLE", flagged)
    ident = proposal(verify_hub, capsys)
    code, output = show(target, ident, capsys, json_output=False)
    assert code == 0
    assert output.out.index("injection_test") < output.out.index("quote:")


def test_packet_cap_evicts_a_terminal_manifest_with_its_packet(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    ident = proposal(verify_hub, capsys)
    hub.shown[ident] = "applied"
    assert verify(target, capsys)[0] == 0
    root = target / pk.PACKETS
    used = sum(path.stat().st_size for path in root.iterdir())
    assert pk.room(target, pk.PACKET_CAP - used + 1)
    assert not list(root.glob(f"{ident}*.json"))
