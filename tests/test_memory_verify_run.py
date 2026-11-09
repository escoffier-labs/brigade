"""`memory verify run`: the pre-run checks, submission, manifests, packets, failure, snapshots and the care entry.

Seats are scripted fakes (the contract tests' `FakeSeat`), evidence assembly
yields the contract tests' receipt and handoff items, and the hub is a
`FakeHub` subclass that adds the verifier status fields, `evidence.attach`,
`propose` and `proposal_show`. The git commit resolver is slice 9a's work:
tests that need a correction patch the single seam, `commit_resolver`, and one
test leaves it alone to show no `replace_block` is ever submitted without it.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shlex
import stat
import time

import pytest

from brigade import cli
from brigade import memory_fairness as fairness
from brigade import memory_hub_client as hubc
from brigade import memory_proposal_io as io
from brigade import memory_verify_cmd as verify_cmd
from brigade import memory_verify_contract as vc
from brigade import memory_verify_evidence as ev
from brigade import memory_verify_packets as pk
from brigade import memory_verify_run as run_mod
from brigade import memory_verify_seat as seats
from brigade import memory_verify_select as pick
from brigade import memory_verify_state as vstate
from tests.test_memory_reflect_hub import ReflectHub
from tests.test_memory_reflect_hub import pytestmark as pytestmark
from tests.test_memory_verify_contract import (
    ABBREV,
    BLOCK,
    CARD_ID,
    CLAIM,
    FIXED,
    RESOLVER,
    FakeSeat,
    block_entry,
    card_text,
    handoff_item,
    receipt_items,
)
from tests.test_memory_verify_select import Clock, configure

SCOPE = "fleet"
SECOND_ID = "card-two"
DAY = pick.DAY_MS
WRITES = {"evidence.attach", "propose"}
READS = {"status", "log", "card", "read_stats", "proposal_show"}
VERIFIER = {"capabilities": ["verifier_floor", "verifier_manifests"], "verifier": True, "reflect_principal": False}


class VerifyHub(ReflectHub):
    """Adds the slice 3 and 4 hub surface the verify run reads and writes."""

    def __init__(self):
        super().__init__()
        self.verify_status = dict(VERIFIER)
        self.attachments: list[dict] = []
        self.proposals: dict[str, dict] = {}
        self.propose_reply: tuple[int, dict] | None = None
        self.attach_reply: tuple[int, dict] | None = None
        self.shown: dict[str, str] = {}
        # What proposal_show lists as bound verifier manifests: None omits the key, as for a caller
        # without review. "latest" binds the last attached manifest, "other" a different one, "fail" errors.
        self.bound: str | None = None
        setattr(self, "do_evidence.attach", self._attach)

    def do_status(self, body):
        status, reply = super().do_status(body)
        if status == 200:
            reply = {**reply, **self.verify_status}
            for name, value in list(reply.items()):
                if value is None:
                    del reply[name]
        return status, reply

    def _attach(self, body):
        if self.attach_reply is not None:
            return self.attach_reply
        data = base64.b64decode(body["artifact_base64"])
        assert io.sha256(data) == body["artifact_sha256"]
        assert body["evidence_id"].split("/") == [self.who, run_mod.WORKSPACE_ID, body["artifact_sha256"]]
        manifest = json.loads(data)
        assert (manifest["card"]["scope"], manifest["card"]["card_id"]) == (body["scope"], body["card_id"])
        self.attachments.append({**body, "manifest": manifest})
        return 201, {"evidence_id": body["evidence_id"], "state": "pending_review"}

    def do_propose(self, body):
        if self.propose_reply is not None:
            return self.propose_reply
        status, reply = super().do_propose(body)
        self.proposals[reply["proposal_id"]] = body
        return status, reply

    def do_proposal_show(self, body):
        state = self.shown.get(body["proposal_id"], "pending")
        if state == "missing":
            return 404, {"code": "memory_proposal_not_found", "error": "proposal not found"}
        reply = {"proposal_id": body["proposal_id"], "state": state}
        if self.bound == "fail":
            return 503, {"code": "memory_action_unavailable", "error": "busy"}
        if self.bound is not None:
            digest = self.attachments[-1]["artifact_sha256"] if self.bound == "latest" else "0" * 64
            reply["verifier_manifests"] = [
                {
                    "evidence_id": "x",
                    "artifact_kind": run_mod.MANIFEST_KIND,
                    "artifact_sha256": digest,
                    "stance": "contrary",
                    "state": "pending_review",
                }
            ]
        return 200, reply


def writes(hub):
    return [call for call in hub.calls if call["action"] not in READS]


def seat(role, model):
    return seats.Seat(
        role=role,
        name=f"{role}-seat",
        cli="claude",
        adapter="claude",
        model=model,
        model_key=("anthropic", model),
        argv=("claude",),
        executable="/usr/bin/true",
        identity=(1, 2),
        fingerprint="0" * 64,
    )


class Seats:
    """The scripted seat layer: every call answers as the contract fixtures' correcting seat."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.status = "ok"
        self.ready_calls = 0

    def ready(self, target, propose, confirm):
        self.ready_calls += 1
        return seat("propose", "claude-test-small"), seat("confirm", "claude-test-large")

    def call(self, area, which, prompt, *, timeout):
        self.calls.append((which.role, prompt))
        if self.status != "ok":
            return seats.Reply(self.status, fatal=self.status == "seat_auth")
        card_id = re.search(r"card_id: (\S+)\n", prompt)[1]
        return seats.Reply("ok", text=FakeSeat(block_entry(), card_id=card_id)(prompt))


def assemble(heads, settings, archive, *, now_ms=None, **_):
    """The contract fixtures' receipt and handoff for a commit card. Every other card has no evidence."""
    for head in heads:
        assembly = ev.Assembly(card_id=head["card_id"], scope=head["scope"], now_ms=now_ms or 0)
        if ABBREV in head["text"]:
            fields, output = receipt_items()
            assembly.items = [fields, output, handoff_item()]
        else:
            assembly.abstain = ev.NO_EVIDENCE
        yield assembly


@pytest.fixture
def verify_hub(tmp_path, monkeypatch):
    from tests._home import set_home

    home = set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    for name in ("BRIGADE_MEMORY_HUB_URL", "BRIGADE_MEMORY_HUB_TOKEN_FILE", "BRIGADE_FLEET_NODE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "verify"
    (target / ".brigade").mkdir(parents=True)
    hub = VerifyHub()
    for ident, anchor in (("a", "c-abcdef"), ("b", "c-fedcba")):
        hub._commit(
            "card.put", SCOPE, ident, f"---\nevidence: []\n---\nSame claim. ^{anchor}\n", "live", hubc.event_id()
        )
    hub._commit("card.put", SCOPE, CARD_ID, card_text((BLOCK, CLAIM)), "live", hubc.event_id())
    from tests.test_memory_hub_client import HUB_URL, NODE_CREDENTIAL

    monkeypatch.setattr(hubc, "_memory_hub_override", lambda fleet: (HUB_URL, NODE_CREDENTIAL, "env"))
    monkeypatch.setattr(hubc, "urllib_transport", hub)
    clock = Clock()
    monkeypatch.setattr(verify_cmd, "MONOTONIC", clock.monotonic)
    monkeypatch.setattr(verify_cmd, "SLEEP", clock.sleep)
    configure(
        target,
        tmp_path / "bound",
        extra='enabled = true\npropose_seat = "propose-seat"\nconfirm_seat = "confirm-seat"\n',
    )
    fake = Seats()
    monkeypatch.setattr(run_mod, "READY", fake.ready)
    monkeypatch.setattr(run_mod, "CALL", fake.call)
    monkeypatch.setattr(run_mod, "WORK_AREA", lambda target: contextlib.nullcontext(object()))
    monkeypatch.setattr(run_mod, "ASSEMBLE", assemble)
    monkeypatch.setattr(run_mod, "ARCHIVE", lambda: None)
    return target, hub, fake, home


@pytest.fixture
def resolver(monkeypatch):
    """Stands in for slice 9a's resolver at the one seam: the fixture abbreviation names the fixture commit."""
    monkeypatch.setattr(run_mod, "commit_resolver", lambda workspace: RESOLVER.get)


def verify(target, capsys, *args):
    code = cli.main(["memory", "verify", "run", *args, "--target", str(target), "--json"])
    out = capsys.readouterr().out
    assert "Zanzibar" not in out and "Same claim" not in out
    return code, json.loads(out)


def state_of(target):
    return vstate.load(target)


def packets(target):
    root = target / pk.PACKETS
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


def entry_for(target, card_id, scope=SCOPE):
    return state_of(target)["scopes"][scope]["cards"][fairness.key_digest(scope, [card_id])]


# --- Pre-run checks ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"capabilities": []}, "verify_hub_floor_missing"),
        ({"capabilities": ["verifier_floor"]}, "verify_hub_floor_missing"),
        ({"capabilities": ["verifier_manifests"]}, "verify_hub_floor_missing"),
        ({"capabilities": None}, "verify_hub_floor_missing"),
        ({"verifier": False}, "verify_not_verifier"),
        ({"verifier": None}, "verify_not_verifier"),
        ({"reflect_principal": True}, "verify_reflect_principal"),
        ({"reflect_principal": None}, "verify_reflect_principal"),
        ({"credential": "admin"}, "verify_admin_credential"),
    ],
)
def test_a_hub_without_the_floor_or_a_wrong_principal_fails_before_any_model_call_or_write(
    verify_hub, capsys, resolver, change, code
):
    target, hub, fake, _ = verify_hub
    hub.verify_status.update(change)
    status, report = verify(target, capsys)
    assert status == 1 and report["status"] == "failed" and report["code"] == code
    assert fake.calls == [] and fake.ready_calls == 0
    assert writes(hub) == []
    # Only the status read happened: no pull, no read_stats, no proposal reads.
    assert {call["action"] for call in hub.calls} == {"status"}
    assert report["schedule"] == {"consecutive_failures": 1, "paused": False}
    assert packets(target) == []


@pytest.mark.parametrize("flags", [["--scheduled"], [], ["--dry-run"]])
def test_a_disabled_pass_makes_no_model_call_no_hub_request_and_no_state_write(verify_hub, capsys, tmp_path, flags):
    target, hub, fake, _ = verify_hub
    configure(target, tmp_path / "bound", extra='propose_seat = "propose-seat"\nconfirm_seat = "confirm-seat"\n')
    status, report = verify(target, capsys, *flags)
    assert status == 1 and report["code"] == run_mod.DISABLED and report["reason"] == run_mod.DISABLED_REASON
    assert fake.calls == [] and fake.ready_calls == 0 and hub.calls == []
    assert not (target / vstate.STATE).exists()
    # The read-only preview keeps working, as it did before the pass could run.
    assert verify(target, capsys, "--dry-run", "--no-model")[0] == 0
    assert fake.calls == []


def test_a_refused_seat_fails_the_run_before_any_hub_write(verify_hub, capsys, monkeypatch):
    target, hub, fake, _ = verify_hub

    def refuse(*args):
        raise seats.SeatRefused("verify_seat_not_isolated", "no passing probe")

    monkeypatch.setattr(run_mod, "READY", refuse)
    status, report = verify(target, capsys)
    assert status == 1 and report["code"] == "verify_seat_not_isolated"
    assert fake.calls == [] and writes(hub) == []


# --- Dry run -------------------------------------------------------------------------------


def test_a_dry_run_calls_seats_but_makes_no_hub_write_no_packet_and_leaves_the_pause(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    state = vstate.fresh()
    state.update(consecutive_failures=2)
    vstate.save(target, state)
    before = (target / vstate.STATE).read_bytes()
    status, report = verify(target, capsys, "--dry-run")
    assert status == 0 and report["status"] == "completed" and report["dry_run"] is True
    assert report["skipped"]["dry_run_proposal"] == 1
    assert [role for role, _ in fake.calls] == ["propose", "confirm"]
    assert writes(hub) == [] and report["proposals"] == []
    assert not (target / pk.PACKETS).exists()
    assert (target / vstate.STATE).read_bytes() == before
    assert report["schedule"] == {"consecutive_failures": 2, "paused": False}


# --- Submission ----------------------------------------------------------------------------


def test_a_real_run_submits_a_pinned_one_card_proposal_with_a_manifest_append_evidence_op(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    status, report = verify(target, capsys)
    assert status == 0 and report["status"] == "completed"
    assert [call["action"] for call in writes(hub)] == ["evidence.attach", "propose"]
    attach, propose = writes(hub)
    (proposal,) = report["proposals"]
    assert proposal["card_id"] == CARD_ID and proposal["scope"] == SCOPE
    replace, append = propose["ops"]
    head = hub.cards[(SCOPE, CARD_ID)]
    for op in (replace, append):
        assert (op["scope"], op["card_id"], op["base_revision"]) == (SCOPE, CARD_ID, head["revision"])
        assert op["block_id"] == BLOCK and re.fullmatch(r"[0-9a-f]{64}", op["expected_block_hash"])
    assert replace["type"] == "replace_block" and replace["text"] == FIXED
    assert append["type"] == "append_evidence" and append["evidence_ids"] == [attach["evidence_id"]]
    assert set(append) == {
        "type",
        "scope",
        "card_id",
        "base_revision",
        "block_id",
        "expected_block_hash",
        "evidence_ids",
    }
    assert attach["artifact_kind"] == run_mod.MANIFEST_KIND and attach["stance"] == "contrary"
    assert attach["workspace_id"] == run_mod.WORKSPACE_ID
    # The manifest carries codes, refs, digests and ranges. No card, evidence or model text.
    manifest_bytes = base64.b64decode(attach["artifact_base64"])
    manifest = json.loads(manifest_bytes)
    assert manifest["schema"] == run_mod.MANIFEST_SCHEMA
    assert manifest["card"] == {"scope": SCOPE, "card_id": CARD_ID, "base_revision": head["revision"]}
    assert manifest["blocks"] == [{"block_id": BLOCK, "verdict": "contradicted", "confidence": "high"}]
    assert {item["kind"] for item in manifest["evidence"]} == {ev.RECEIPT_FIELDS, ev.HANDOFF}
    assert all(item["ranges"] for item in manifest["evidence"])
    assert manifest["seats"][0]["model"] == "claude-test-small"
    for text in ("Zanzibar", FIXED, "fix verified"):
        assert text not in manifest_bytes.decode()
    # The packet is the one the manifest binds, owner-only, and named by its proposal id.
    packet_path = target / pk.PACKETS / f"{proposal['proposal_id']}.json"
    assert packets(target) == [packet_path.name]  # The stage is gone once the packet is published.
    assert io.sha256(packet_path.read_bytes()) == manifest["packet_sha256"]
    assert stat.S_IMODE(os.stat(packet_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(packet_path.parent).st_mode) == 0o700
    packet = json.loads(packet_path.read_bytes())
    assert packet["ops"][0]["text"] == FIXED and [call["role"] for call in packet["calls"]] == ["propose", "confirm"]
    assert {item["rank"] for item in packet["evidence"]} == {ev.AUTHENTICATED, ev.MEDIUM}
    assert manifest["input_sha256"] == io.sha256(packet["calls"][0]["prompt"].encode())
    # The card is remembered as pending, and the ledger holds the proposal id.
    assert entry_for(target, CARD_ID)["pending"] == proposal["proposal_id"]
    ledger = state_of(target)["submissions"]
    assert [pending["proposal_id"] for pending in ledger.values()] == [proposal["proposal_id"]]
    assert report["schedule"] == {"consecutive_failures": 0, "paused": False}
    # The next run reads the proposal back, sees it still open and proposes nothing new.
    hub.calls.clear()
    status, report = verify(target, capsys)
    assert status == 0 and writes(hub) == []
    assert [call["proposal_id"] for call in hub.calls if call["action"] == "proposal_show"] == [proposal["proposal_id"]]
    assert report["skipped"]["pending_proposal"] == 1


def test_without_the_slice_9a_resolver_no_replace_block_is_ever_submitted(verify_hub, capsys):
    target, hub, fake, _ = verify_hub
    assert all(run_mod.commit_resolver(workspace) is None for workspace in run_mod.config.load(target).workspaces)
    status, report = verify(target, capsys)
    assert status == 0 and report["status"] == "completed"
    card = next(card for card in report["cards"] if card["card_id"] == CARD_ID)
    # The proposing seat asked for the correction. The expansion abstained, so nothing was confirmed or opened.
    assert card["ops"] == [] and [block["reason"] for block in card["blocks"]] == [vc.COMMIT_UNRESOLVED]
    assert [role for role, _ in fake.calls] == ["propose"]
    assert entry_for(target, CARD_ID)["verdict"] == vc.COMMIT_UNRESOLVED
    # Eligible again after the interval, it still opens nothing.
    later = int(time.time() * 1000) + 8 * DAY
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=later)
    assert any(card["card_id"] == CARD_ID for card in report["cards"])
    assert writes(hub) == [] and report["proposals"] == [] and packets(target) == []


def test_a_duplicate_pending_reply_records_the_existing_id(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    existing = hubc.event_id()
    hub.propose_reply = (409, {"code": "memory_proposal_duplicate_pending", "proposal_id": existing})
    status, report = verify(target, capsys)
    assert status == 0 and report["status"] == "completed"
    assert report["skipped"]["duplicate_pending"] == 1 and report["proposals"] == []
    assert entry_for(target, CARD_ID)["pending"] == existing
    assert [pending["proposal_id"] for pending in state_of(target)["submissions"].values()] == [existing]
    # A verifier without review sees no bound manifests, so no packet is published under that id.
    assert packets(target) == []
    assert entry_for(target, CARD_ID)["verdict"] == run_mod.PACKET_UNBOUND
    assert report["skipped"][run_mod.PACKET_UNBOUND] == 1


@pytest.mark.parametrize("code", sorted(hubc.QUOTA_REFUSALS))
def test_principal_and_scope_quota_replies_stop_proposing_without_a_failure(verify_hub, capsys, resolver, code):
    target, hub, fake, _ = verify_hub
    hub._commit("card.put", SCOPE, SECOND_ID, card_text((BLOCK, CLAIM)), "live", hubc.event_id())
    hub.propose_reply = (429, {"code": code, "error": "quota"})
    status, report = verify(target, capsys)
    assert status == 0 and report["status"] == "completed"
    # The first card met the quota. The second is still checked, then deferred without a cooldown.
    assert report["skipped"]["proposal_quota"] == 1 and report["skipped"][run_mod.DEFERRED_QUOTA] == 1
    assert [call["action"] for call in writes(hub)] == ["evidence.attach", "propose"]
    assert {card["card_id"] for card in report["cards"]} >= {CARD_ID, SECOND_ID}
    assert "verified" not in entry_for(target, SECOND_ID)
    assert report["schedule"] == {"consecutive_failures": 0, "paused": False}
    assert "pending" not in entry_for(target, CARD_ID)
    # The request is in the ledger without a result, so its stage stays for the next run to replay.
    (staged,) = packets(target)
    assert pk.STAGED.fullmatch(staged)
    assert "verified" not in entry_for(target, CARD_ID)


@pytest.mark.parametrize("code", sorted(hubc.EVIDENCE_QUOTA_REFUSALS))
def test_a_retained_evidence_quota_stops_before_proposing_without_a_failure(verify_hub, capsys, resolver, code):
    target, hub, _, _ = verify_hub
    hub.attach_reply = (429, {"code": code, "error": "quota"})
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"]["evidence_quota"] == 1
    assert [call["action"] for call in writes(hub)] == ["evidence.attach"]
    assert report["schedule"]["consecutive_failures"] == 0


def test_an_unclean_manifest_is_never_attached(verify_hub, capsys, resolver, monkeypatch):
    target, hub, _, _ = verify_hub
    original = vc.scan_problem
    # Flag only the manifest. The replacement still passes the contract's own check 7.
    monkeypatch.setattr(vc, "scan_problem", lambda text: run_mod.MANIFEST_SCHEMA in text or original(text))
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"]["verify_manifest_unclean"] == 1
    assert writes(hub) == []


# --- The pending ceiling -------------------------------------------------------------------


def seed_pending(target, count):
    """Remember `count` pending proposals for cards that are not live, under the run's own binding."""
    state = state_of(target)
    cards = state["scopes"][SCOPE]["cards"]
    ids = []
    for index in range(count):
        proposal = hubc.event_id()
        cards[f"{index:032x}"] = {"seen": 1, "proposed": 1, "pending": proposal}
        ids.append(proposal)
    for card in cards.values():
        card.pop("verified", None)
    vstate.save(target, state)
    return ids


def test_the_pending_ceiling_stops_new_proposals(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    hub.propose_reply = (429, {"code": "memory_proposal_scope_quota", "error": "quota"})
    assert verify(target, capsys)[0] == 0  # Binds the state to this hub without opening anything.
    hub.propose_reply = None
    ids = seed_pending(target, run_mod.PENDING_CEILING)
    hub.calls.clear()
    fake.calls.clear()
    status, report = verify(target, capsys)
    # Selection stops at the ceiling, and so does the replay of the earlier quota-stopped request.
    assert status == 0 and report["skipped"]["pending_ceiling"] == 2 and report["selected"] == []
    assert fake.calls == [] and writes(hub) == []
    assert sorted(call["proposal_id"] for call in hub.calls if call["action"] == "proposal_show") == sorted(ids)
    # Once the hub says they are settled, the ceiling frees and the card is proposed.
    for proposal in ids:
        hub.shown[proposal] = "rejected"
    status, report = verify(target, capsys)
    assert status == 0 and len(report["proposals"]) == 1


def test_the_ceiling_and_the_per_run_cap_also_bind_within_a_run(verify_hub, capsys, resolver, monkeypatch):
    target, hub, _, _ = verify_hub
    hub._commit("card.put", SCOPE, SECOND_ID, card_text((BLOCK, CLAIM)), "live", hubc.event_id())
    hub.propose_reply = (429, {"code": "memory_proposal_scope_quota", "error": "quota"})
    assert verify(target, capsys)[0] == 0
    hub.propose_reply = None
    seed_pending(target, run_mod.PENDING_CEILING - 1)
    status, report = verify(target, capsys)
    assert len(report["proposals"]) == 1 and report["skipped"][run_mod.DEFERRED_CAP] == 1
    monkeypatch.setattr(run_mod, "MAX_NEW_PROPOSALS", 1)
    state = state_of(target)
    state["scopes"][SCOPE]["cards"] = {
        key: {name: value for name, value in entry.items() if name not in ("pending", "verified")}
        for key, entry in state["scopes"][SCOPE]["cards"].items()
        if "reads" in entry
    }
    state["submissions"] = {}
    vstate.save(target, state)
    status, report = verify(target, capsys)
    assert len(report["proposals"]) == 1 and report["skipped"][run_mod.DEFERRED_CAP] == 1


def test_a_correction_over_the_per_run_cap_is_checked_again_next_run(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    extra = [f"card-{index}" for index in range(2, 6)]
    for card_id in extra:
        hub._commit("card.put", SCOPE, card_id, card_text((BLOCK, CLAIM)), "live", hubc.event_id())
    status, report = verify(target, capsys)
    assert status == 0 and len(report["proposals"]) == run_mod.MAX_NEW_PROPOSALS
    assert report["skipped"][run_mod.DEFERRED_CAP] == 1
    opened = {item["card_id"] for item in report["proposals"]}
    (fifth,) = {CARD_ID, *extra} - opened
    assert "verified" not in entry_for(target, fifth)
    status, report = verify(target, capsys)
    assert fifth in {card["card_id"] for card in report["cards"]}
    assert [item["card_id"] for item in report["proposals"]] == [fifth]


def test_a_correction_after_a_quota_stop_is_checked_again_next_run(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    hub._commit("card.put", SCOPE, SECOND_ID, card_text((BLOCK, CLAIM)), "live", hubc.event_id())
    hub.propose_reply = (429, {"code": "memory_proposal_principal_quota", "error": "quota"})
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"][run_mod.DEFERRED_QUOTA] == 1
    (stopped,) = [call["ops"][0]["card_id"] for call in writes(hub) if call["action"] == "propose"]
    (deferred,) = {CARD_ID, SECOND_ID} - {stopped}
    hub.propose_reply = None
    status, report = verify(target, capsys)
    # The quota-stopped request is replayed, and the deferred card is checked again and proposed.
    assert deferred in {card["card_id"] for card in report["cards"]}
    assert sorted(item["card_id"] for item in report["proposals"]) == sorted([CARD_ID, SECOND_ID])


def test_a_disabled_doctor_probes_no_seat(verify_hub, capsys, tmp_path, monkeypatch):
    target, hub, fake, _ = verify_hub
    configure(target, tmp_path / "bound", extra='propose_seat = "propose-seat"\nconfirm_seat = "confirm-seat"\n')
    probed = []
    monkeypatch.setattr(seats, "doctor_payload", lambda *args, **kwargs: probed.append(args) or {"status": "ok"})
    monkeypatch.setattr(seats, "probe", lambda *args, **kwargs: probed.append(args))
    code = cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and report["code"] == run_mod.DISABLED
    assert probed == [] and fake.calls == [] and hub.calls == []


# --- Failure and pause ---------------------------------------------------------------------


def test_three_failures_pause_scheduled_calls_and_a_manual_success_clears_them(verify_hub, capsys):
    target, hub, fake, _ = verify_hub
    hub.mode = "down"
    for count in (1, 2, 3):
        status, report = verify(target, capsys, "--scheduled")
        assert status == 1 and report["code"] == "hub_unavailable"
        assert report["schedule"] == {"consecutive_failures": count, "paused": count == 3}
    hub.mode = "up"
    calls = len(hub.calls)
    status, report = verify(target, capsys, "--scheduled")
    assert status == 0 and report["status"] == "paused" and len(hub.calls) == calls
    # A dry run leaves the pause alone.
    assert verify(target, capsys, "--dry-run")[1]["schedule"]["consecutive_failures"] == 3
    assert state_of(target)["paused"] is True
    status, report = verify(target, capsys)
    assert status == 0 and report["schedule"] == {"consecutive_failures": 0, "paused": False}
    assert verify(target, capsys, "--scheduled")[1]["status"] == "completed"


def test_a_seat_quota_stops_the_run_without_a_failure_and_an_auth_failure_fails_it(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    fake.status = "seat_quota"
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"]["seat_quota"] == 1 and report["schedule"]["consecutive_failures"] == 0
    fake.status = "seat_auth"
    status, report = verify(target, capsys)
    assert status == 1 and report["code"] == "seat_auth" and report["schedule"]["consecutive_failures"] == 1
    assert writes(hub) == []


def test_every_model_call_failing_fails_the_run(verify_hub, capsys):
    target, hub, fake, _ = verify_hub
    fake.status = "seat_timeout"
    status, report = verify(target, capsys)
    assert status == 1 and report["code"] == "verify_seats_failed"
    # A timed-out card is not marked verified, so it is tried again.
    assert "verified" not in entry_for(target, CARD_ID)


# --- Packets -------------------------------------------------------------------------------


def write_packet(target, name, data=b"{}\n"):
    root = target / pk.PACKETS
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / name).write_bytes(data)
    os.chmod(root / name, 0o600)


def test_packets_of_settled_proposals_expire_14_days_after_settling(tmp_path):
    target = tmp_path
    now = 30 * DAY
    still_open, settled = hubc.event_id(), hubc.event_id()
    old, young = hubc.event_id(), hubc.event_id()
    write_packet(target, f"{still_open}.json")
    write_packet(target, f"{settled}.json")
    write_packet(target, f"{old}.t{now - 14 * DAY}.json")
    write_packet(target, f"{young}.t{now - 14 * DAY + 1}.json")
    counts = pk.maintain(target, {still_open}, now)
    assert counts == {"packets_temporary_removed": 0, "packets_settled": 1, "packets_expired": 1}
    assert packets(target) == sorted(
        [f"{still_open}.json", f"{settled}.t{now}.json", f"{young}.t{now - 14 * DAY + 1}.json"]
    )
    assert stat.S_IMODE(os.stat(target / pk.PACKETS).st_mode) == 0o700


def test_the_packet_cap_drops_the_oldest_settled_packets_first_and_never_open_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(pk, "PACKET_CAP", 100)
    first, second, live = hubc.event_id(), hubc.event_id(), hubc.event_id()
    write_packet(tmp_path, f"{first}.t1.json", b"x" * 30)
    write_packet(tmp_path, f"{second}.t2.json", b"x" * 30)
    write_packet(tmp_path, f"{live}.json", b"x" * 30)
    assert pk.room(tmp_path, 30)
    assert packets(tmp_path) == sorted([f"{second}.t2.json", f"{live}.json"])
    assert not pk.room(tmp_path, 80)
    assert packets(tmp_path) == [f"{live}.json"]


def test_a_run_settles_and_later_expires_the_packet_of_an_applied_proposal(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    now = int(time.time() * 1000)
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    (proposal,) = report["proposals"]
    hub.shown[proposal["proposal_id"]] = "applied"
    later = now + DAY
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=later)
    assert report["skipped"]["packets_settled"] == 1 and "pending" not in entry_for(target, CARD_ID)
    assert packets(target) == [f"{proposal['proposal_id']}.t{later}.json"]
    report = run_mod.run_payload(
        target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=later + pk.PACKET_DAYS * DAY
    )
    assert report["skipped"]["packets_expired"] == 1
    assert f"{proposal['proposal_id']}.t{later}.json" not in packets(target)


def test_a_full_packet_directory_defers_the_card_before_any_hub_write(verify_hub, capsys, resolver, monkeypatch):
    target, hub, _, _ = verify_hub
    monkeypatch.setattr(pk, "PACKET_CAP", 64)
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"]["verify_packet_cap"] == 1 and writes(hub) == []


# --- Atomic publication, staging and replay -------------------------------------------------


class FixedSecrets:
    """Deterministic nonces and evidence order for the contract module, so a rerun builds identical packets.

    Each check draws two nonces, propose then confirm, so the two alternate.
    """

    drawn = 0

    @classmethod
    def token_hex(cls, count):
        cls.drawn += 1
        return ("ab" if cls.drawn % 2 else "cd") * count

    @staticmethod
    def SystemRandom():  # noqa: N802 - mirrors the secrets API
        import random

        return random.Random(7)


def manifest_of(hub):
    attach = [call for call in writes(hub) if call["action"] == "evidence.attach"][-1]
    return json.loads(base64.b64decode(attach["artifact_base64"]))


def park(target, proposal_id):
    """Make the card checkable again while `proposal_id` stays open under another remembered card."""
    state = state_of(target)
    cards = state["scopes"][SCOPE]["cards"]
    for entry in cards.values():
        entry.pop("verified", None)
        entry.pop("pending", None)
    if proposal_id is not None:
        cards["f" * 32] = {"seen": 1, "proposed": 1, "pending": proposal_id}
    state["submissions"] = {}
    vstate.save(target, state)


def test_a_duplicate_pending_packet_is_written_once_kept_when_matching_and_never_overwritten(
    verify_hub, capsys, resolver, monkeypatch
):
    target, hub, _, _ = verify_hub
    monkeypatch.setattr(vc, "secrets", FixedSecrets)
    hub.bound = "latest"
    existing = hubc.event_id()
    hub.propose_reply = (409, {"code": "memory_proposal_duplicate_pending", "proposal_id": existing})
    assert verify(target, capsys)[0] == 0
    path = target / pk.PACKETS / f"{existing}.json"
    first = os.stat(path)
    assert io.sha256(path.read_bytes()) == manifest_of(hub)["packet_sha256"]
    # The same check again: the matching packet stays exactly as it is.
    park(target, existing)
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"]["duplicate_pending"] == 1
    assert pk.MISMATCH not in report["skipped"]
    again = os.stat(path)
    assert (again.st_ino, again.st_mtime_ns) == (first.st_ino, first.st_mtime_ns)
    assert packets(target) == [path.name]


def test_a_mismatched_packet_for_a_duplicate_is_never_overwritten(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    hub.propose_reply = (429, {"code": "memory_proposal_scope_quota", "error": "quota"})
    assert verify(target, capsys)[0] == 0  # Binds the state to this hub without opening anything.
    existing = hubc.event_id()
    park(target, existing)
    for name in packets(target):
        (target / pk.PACKETS / name).unlink()
    write_packet(target, f"{existing}.json", b'{"other": "packet"}\n')
    hub.bound = "latest"
    hub.propose_reply = (409, {"code": "memory_proposal_duplicate_pending", "proposal_id": existing})
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"][pk.MISMATCH] == 1
    assert (target / pk.PACKETS / f"{existing}.json").read_bytes() == b'{"other": "packet"}\n'
    assert packets(target) == [f"{existing}.json"]  # The stage is gone, the proposal is left for review.
    assert entry_for(target, CARD_ID)["pending"] == existing


def test_a_crash_between_create_and_write_leaves_no_final_name_and_the_next_run_recovers(
    verify_hub, capsys, resolver, monkeypatch
):
    target, hub, _, _ = verify_hub
    original = pk.write_all

    in_flight = []

    def crash(handle, data):
        # What the directory holds while the bytes are being written: only the temporary file.
        in_flight.append(sorted(path.name for path in (target / pk.PACKETS).iterdir()))
        raise OSError("disk gone")

    monkeypatch.setattr(pk, "write_all", crash)
    status, report = verify(target, capsys)
    assert status == 1 and report["code"] == "verify_io"
    (names,) = in_flight
    assert len(names) == 1 and pk.TEMPORARY.fullmatch(names[0])
    assert packets(target) == [] and writes(hub) == []
    monkeypatch.setattr(pk, "write_all", original)
    status, report = verify(target, capsys)
    assert status == 0
    (proposal,) = report["proposals"]
    path = target / pk.PACKETS / f"{proposal['proposal_id']}.json"
    assert io.sha256(path.read_bytes()) == manifest_of(hub)["packet_sha256"]


def test_a_leftover_temporary_file_is_removed_by_the_next_run(verify_hub, capsys):
    target, _, _, _ = verify_hub
    leftover = f".packet-{'0' * 32}.tmp"
    write_packet(target, leftover, b"partial")
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"]["packets_temporary_removed"] == 1
    assert leftover not in packets(target)


def test_the_temporary_file_is_owner_only_inside_the_packet_directory(verify_hub, capsys, resolver, monkeypatch):
    target, _, _, _ = verify_hub
    seen = []
    original = pk.write_all

    def observe(handle, data):
        for path in target.rglob(".packet-*.tmp"):
            seen.append((path.parent, stat.S_IMODE(os.stat(path).st_mode)))
        original(handle, data)

    monkeypatch.setattr(pk, "write_all", observe)
    assert verify(target, capsys)[0] == 0
    # The stage, then the packet: each through its own temporary file in the packet directory.
    assert len(seen) == 2 and set(seen) == {(target / pk.PACKETS, 0o600)}
    assert not list(target.rglob(".packet-*.tmp"))


def test_a_failure_after_submit_before_finalize_is_recovered_from_the_stage(verify_hub, capsys, resolver, monkeypatch):
    target, hub, _, _ = verify_hub

    def lost(*args):
        raise OSError("lost before finalize")

    monkeypatch.setattr(run_mod, "FINALIZE", lost)
    status, report = verify(target, capsys)
    assert status == 1 and report["code"] == "verify_io"
    (proposal_id,) = [pending["proposal_id"] for pending in state_of(target)["submissions"].values()]
    assert [name for name in packets(target) if pk.STAGED.fullmatch(name)]
    assert f"{proposal_id}.json" not in packets(target)
    monkeypatch.setattr(run_mod, "FINALIZE", pk.finalize)
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"]["packets_recovered"] == 1
    assert packets(target) == [f"{proposal_id}.json"]
    path = target / pk.PACKETS / f"{proposal_id}.json"
    assert io.sha256(path.read_bytes()) == manifest_of(hub)["packet_sha256"]
    assert sum(1 for call in hub.calls if call["action"] == "propose") == 1


def test_a_failed_submit_is_replayed_by_the_next_healthy_run_with_the_same_request(verify_hub, capsys, resolver):
    target, hub, fake, _ = verify_hub
    hub.propose_reply = (503, {"code": "memory_writer_busy", "error": "busy", "retryable": True})
    status, report = verify(target, capsys)
    assert status == 1 and report["schedule"]["consecutive_failures"] == 1
    assert "verified" not in entry_for(target, CARD_ID)
    hub.propose_reply = None
    calls = len(fake.calls)
    status, report = verify(target, capsys)
    assert status == 0 and len(report["proposals"]) == 1
    first, second = [call for call in hub.calls if call["action"] == "propose"]
    assert (first["client_event_id"], first["ops"]) == (second["client_event_id"], second["ops"])
    # The replayed card is not checked again, and the real success clears the counter.
    assert len(fake.calls) == calls
    assert report["schedule"] == {"consecutive_failures": 0, "paused": False}


def test_a_tampered_stage_is_removed_and_never_sent(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    hub.propose_reply = (429, {"code": "memory_proposal_scope_quota", "error": "quota"})
    assert verify(target, capsys)[0] == 0
    (name,) = packets(target)
    path = target / pk.PACKETS / name
    record = json.loads(path.read_bytes())
    record["packet"] = base64.b64encode(b'{"changed": true}\n').decode()
    path.write_bytes(json.dumps(record).encode())
    tampered = record["evidence_id"]
    hub.propose_reply = None
    hub.calls.clear()
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"]["packets_stage_invalid"] == 1
    assert all(call["evidence_id"] != tampered for call in writes(hub) if call["action"] == "evidence.attach")
    for call in writes(hub):
        if call["action"] == "propose":
            assert call["ops"][-1]["evidence_ids"] != [tampered]


def test_a_duplicate_bound_to_a_different_manifest_publishes_nothing_and_records_why(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    hub.bound = "other"
    existing = hubc.event_id()
    hub.propose_reply = (409, {"code": "memory_proposal_duplicate_pending", "proposal_id": existing})
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"][run_mod.PACKET_UNBOUND] == 1
    assert packets(target) == []  # Neither a packet under that id nor the stage remains.
    entry = entry_for(target, CARD_ID)
    assert entry["verdict"] == run_mod.PACKET_UNBOUND and entry["pending"] == existing


def test_a_failed_binding_read_keeps_the_stage_until_it_expires(verify_hub, resolver):
    target, hub, fake, _ = verify_hub
    hub.bound = "fail"
    existing = hubc.event_id()
    hub.propose_reply = (409, {"code": "memory_proposal_duplicate_pending", "proposal_id": existing})
    now = int(time.time() * 1000)
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    assert report["skipped"][run_mod.BINDING_UNCHECKED] == 1
    (staged,) = packets(target)
    assert pk.STAGED.fullmatch(staged)
    # The next run still cannot read the binding, so the stage stays, and nothing is published.
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now + DAY)
    assert packets(target) == [staged] and report["skipped"][run_mod.BINDING_UNCHECKED] == 1
    later = now + pk.STAGE_DAYS * DAY
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=later)
    assert report["skipped"][run_mod.STAGE_EXPIRED] == 1 and packets(target) == []
    assert entry_for(target, CARD_ID)["verdict"] == run_mod.STAGE_EXPIRED


def test_a_lost_reply_then_expiry_holds_the_card_until_its_tombstone_ages_out(verify_hub, resolver):
    target, hub, fake, _ = verify_hub
    # The reply to this propose is lost: the hub may hold a proposal whose id this job never learns.
    hub.propose_reply = (503, {})
    now = int(time.time() * 1000)
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    assert report["code"] == "hub_unavailable"
    (old_request,) = [entry["client_event_id"] for entry in state_of(target)["submissions"].values()]
    proposes = sum(1 for call in hub.calls if call["action"] == "propose")
    expiry = now + pk.STAGE_DAYS * DAY
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=expiry)
    assert report["skipped"][run_mod.STAGE_EXPIRED] == 1 and packets(target) == []
    assert entry_for(target, CARD_ID)["verdict"] == run_mod.STAGE_EXPIRED
    # The request id stays behind as a tombstone that names the card only by its key digest.
    ((key, tombstone),) = state_of(target)["submissions"].items()
    card_key = fairness.key_digest(SCOPE, [CARD_ID])
    assert tombstone == {"client_event_id": old_request, "ops_sha256": key, "expired": True, "card": card_key}
    # The card was checked again in that run, but its new correction was not proposed.
    assert report["skipped"][run_mod.UNRESOLVED_REQUEST] == 1
    assert sum(1 for call in hub.calls if call["action"] == "propose") == proposes
    # While the tombstone lives, later runs do not select the card at all.
    hub.propose_reply = None
    calls = len(fake.calls)
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=expiry + DAY)
    assert report["skipped"][run_mod.UNRESOLVED_REQUEST] == 1 and len(fake.calls) == calls
    assert CARD_ID not in {item["card_id"] for item in report["selected"]}
    assert sum(1 for call in hub.calls if call["action"] == "propose") == proposes
    # Once the tombstone ages out, the card is proposed again under a new request.
    aged = hubc.event_id_ms(old_request) + run_mod.TOMBSTONE_DAYS * DAY
    run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=aged)
    assert state_of(target)["submissions"] == {}
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=aged + DAY)
    (opened,) = report["proposals"]
    assert opened["card_id"] == CARD_ID
    last = [call for call in hub.calls if call["action"] == "propose"][-1]
    assert last["client_event_id"] != old_request


def test_identical_ops_after_an_expired_stage_reuse_the_same_request_id(verify_hub, resolver, monkeypatch):
    target, hub, _, _ = verify_hub
    monkeypatch.setattr(vc, "secrets", FixedSecrets)
    # The hub may have stored this request although no reply arrived. Here it answers retryable.
    hub.propose_reply = (503, {"code": "memory_writer_busy", "error": "busy", "retryable": True})
    now = int(time.time() * 1000)
    run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    (first,) = [call for call in hub.calls if call["action"] == "propose"]
    hub.propose_reply = (429, {"code": "memory_proposal_scope_quota", "error": "quota"})
    # The stage expires. The same run checks the card again, rebuilds identical ops and resends them.
    report = run_mod.run_payload(
        target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now + pk.STAGE_DAYS * DAY
    )
    assert report["skipped"][run_mod.STAGE_EXPIRED] == 1 and report["skipped"]["request_id_reused"] == 1
    second = [call for call in hub.calls if call["action"] == "propose"][-1]
    assert second["ops"] == first["ops"] and second["client_event_id"] == first["client_event_id"]
    # The tombstone is consumed: the live entry holds the reused id.
    ((pending),) = state_of(target)["submissions"].values()
    assert pending["client_event_id"] == first["client_event_id"] and "expired" not in pending


def test_tombstones_are_bounded_by_age_and_count(verify_hub, monkeypatch):
    target, hub, _, _ = verify_hub
    now = int(time.time() * 1000)
    run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    ages = [run_mod.TOMBSTONE_DAYS * DAY, run_mod.TOMBSTONE_DAYS * DAY - DAY, 3 * DAY, 2 * DAY, DAY]
    state = state_of(target)
    for index, age in enumerate(ages):
        key = f"{index:064x}"
        state["submissions"][key] = {
            "client_event_id": hubc.event_id(now - age),
            "ops_sha256": key,
            "expired": True,
        }
    vstate.save(target, state)
    run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    # Under the cap, only the request 90 days old ages out.
    assert sorted(state_of(target)["submissions"]) == [f"{index:064x}" for index in range(1, 5)]
    # Over the cap, the oldest go first and the youngest stay.
    monkeypatch.setattr(run_mod, "MAX_TOMBSTONES", 2)
    run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    assert sorted(state_of(target)["submissions"]) == [f"{3:064x}", f"{4:064x}"]


def test_a_stage_with_an_altered_append_evidence_op_is_refused(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    hub.propose_reply = (429, {"code": "memory_proposal_scope_quota", "error": "quota"})
    assert verify(target, capsys)[0] == 0
    (name,) = packets(target)
    path = target / pk.PACKETS / name
    record = json.loads(path.read_bytes())
    record["ops"][-1]["base_revision"] = record["ops"][-1]["base_revision"] + 1
    path.write_bytes(json.dumps(record).encode())
    (request,) = [entry["client_event_id"] for entry in state_of(target)["submissions"].values()]
    hub.propose_reply = None
    hub.calls.clear()
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"][run_mod.OPS_MISMATCH] == 1
    assert writes(hub) == [] and packets(target) == []
    # No new request id was minted from the altered stage.
    assert [entry["client_event_id"] for entry in state_of(target)["submissions"].values()] == [request]


def test_a_transient_attach_failure_is_replayed_by_the_next_healthy_run(verify_hub, capsys, resolver):
    target, hub, _, _ = verify_hub
    hub.attach_reply = (503, {"code": "memory_writer_busy", "error": "busy", "retryable": True})
    status, report = verify(target, capsys)
    assert report["skipped"][run_mod.MANIFEST_TRANSIENT] == 1
    assert "verified" not in entry_for(target, CARD_ID)
    (staged,) = packets(target)
    assert pk.STAGED.fullmatch(staged)
    hub.attach_reply = None
    status, report = verify(target, capsys)
    assert status == 0 and len(report["proposals"]) == 1
    attaches = [call for call in hub.calls if call["action"] == "evidence.attach"]
    assert len(attaches) == 2 and attaches[0]["evidence_id"] == attaches[1]["evidence_id"]
    assert packets(target) == [f"{report['proposals'][0]['proposal_id']}.json"]


def test_a_staged_submission_for_a_scope_no_longer_bound_is_never_sent(verify_hub, capsys, resolver, tmp_path):
    target, hub, _, _ = verify_hub
    hub.propose_reply = (503, {"code": "memory_writer_busy", "error": "busy", "retryable": True})
    verify(target, capsys)
    (staged,) = packets(target)
    other = tmp_path / "other-root"
    other.mkdir()
    (target / ".brigade/memory-care.toml").write_text(
        '[hub]\nmode="read"\nread_scopes=["fleet", "repo:second"]\n'
        '[verify]\nenabled = true\npropose_seat = "propose-seat"\nconfirm_seat = "confirm-seat"\n'
        f'[[verify.workspace]]\nscope="repo:second"\npath="{other}"\narchive_projects=[]\n'
    )
    hub.propose_reply = None
    hub.calls.clear()
    status, report = verify(target, capsys)
    assert status == 0 and report["skipped"][run_mod.SCOPE_UNBOUND] == 1
    assert writes(hub) == [] and packets(target) == []
    (pending,) = state_of(target)["submissions"].values()
    assert pending["refused"] == run_mod.SCOPE_UNBOUND
    assert state_of(target)["scopes"][SCOPE]["cards"][fairness.key_digest(SCOPE, [CARD_ID])]["verdict"] == (
        run_mod.SCOPE_UNBOUND
    )


def test_an_idle_run_leaves_the_pause_counter_and_a_run_with_work_resets_it(verify_hub, capsys):
    target, hub, fake, _ = verify_hub
    assert verify(target, capsys)[0] == 0  # Every card is now inside its interval.
    state = state_of(target)
    state.update(consecutive_failures=2)
    vstate.save(target, state)
    calls = len(fake.calls)
    status, report = verify(target, capsys)
    assert status == 0 and report["idle"] is True and len(fake.calls) == calls
    assert report["schedule"] == {"consecutive_failures": 2, "paused": False}
    park(target, None)
    status, report = verify(target, capsys)
    assert status == 0 and report["idle"] is False
    assert report["schedule"] == {"consecutive_failures": 0, "paused": False}


# --- Read count snapshots ------------------------------------------------------------------


def counted(hub, reads):
    """Read stats with the given (reads_7d, reads_30d, total) per card id, one row per page."""

    def reply(body):
        rows = [
            {"card_id": card_id, "reads_7d": r[0], "reads_30d": r[1], "last_read_day": 20400, "total_reads": r[2]}
            for card_id, r in sorted(reads.items())
        ]
        start = int(body.get("after") or 0)
        following = str(start + 1) if start + 1 < len(rows) else None
        return 200, {
            "scope": body["scope"],
            "rows": rows[start : start + 1],
            "next_after": following,
            "read_counts_since_day": 1,
        }

    hub.do_read_stats = reply


def snapshot(target):
    table = state_of(target)["scopes"][SCOPE]
    reads = {key: entry.get("reads") for key, entry in table["cards"].items()}
    return table.get("counts_ms"), reads


def test_a_snapshot_is_stored_whole_in_one_write_after_complete_pagination(verify_hub, monkeypatch):
    target, hub, _, _ = verify_hub
    counted(hub, {"a": (5, 10, 20), "b": (1, 2, 3)})
    saved = []
    original = vstate.save

    def recording(target, state):
        saved.append(json.loads(json.dumps(state)))
        original(target, state)

    monkeypatch.setattr(vstate, "save", recording)
    now = int(time.time() * 1000)
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    assert report["status"] == "completed" and report["read_stats"] == {SCOPE: "fresh"}
    with_counts = [state for state in saved if "counts_ms" in state["scopes"].get(SCOPE, {})]
    first = with_counts[0]["scopes"][SCOPE]
    # The first write that carries the snapshot carries all of it: every live card's reads and counts_ms.
    assert first["counts_ms"] == now
    live = {fairness.key_digest(SCOPE, [card_id]) for card_id in ("a", "b", CARD_ID)}
    assert {key for key, entry in first["cards"].items() if "reads" in entry} == live
    assert first["cards"][fairness.key_digest(SCOPE, ["a"])]["reads"] == [5, 10, 20]
    assert first["cards"][fairness.key_digest(SCOPE, [CARD_ID])]["reads"] == [0, 0, 0]
    assert not any("counts_ms" in state["scopes"].get(SCOPE, {}) for state in saved[: saved.index(with_counts[0])])


@pytest.mark.parametrize("failure", ["rate_limited", "refused"])
def test_an_incomplete_pagination_stores_no_snapshot(verify_hub, failure):
    target, hub, _, _ = verify_hub
    counted(hub, {"a": (5, 10, 20), "b": (1, 2, 3)})
    paged = hub.do_read_stats

    def second_page_fails(body):
        if body.get("after"):
            if failure == "rate_limited":
                return 429, {"code": hubc.READ_STATS_RATE_LIMITED, "error": "slow"}
            return 403, {"code": "memory_grant_missing", "error": "no"}
        return paged(body)

    hub.do_read_stats = second_page_fails
    now = int(time.time() * 1000)
    report = run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    if failure == "rate_limited":
        assert report["status"] == "completed" and report["read_stats"] == {SCOPE: "unavailable"}
        counts_ms, reads = snapshot(target)
        assert counts_ms is None and not any(reads.values())
    else:
        assert report["status"] == "failed"
        assert not state_of(target)["scopes"]


def test_a_snapshot_scores_a_rate_limited_scope_only_within_its_bounded_age_and_never_for_never_read(verify_hub):
    target, hub, _, _ = verify_hub
    counted(hub, {"a": (50, 100, 400)})
    first = int(time.time() * 1000) - 120 * DAY  # Old enough that never-read could be reported.
    run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=first)
    assert snapshot(target)[0] == first

    def forget_verification():
        state = state_of(target)
        for entry in state["scopes"][SCOPE]["cards"].values():
            entry.pop("verified", None)
        vstate.save(target, state)

    hub.do_read_stats = lambda body: (429, {"code": hubc.READ_STATS_RATE_LIMITED, "error": "slow"})
    forget_verification()
    within = first + verify_cmd.SNAPSHOT_MAX_AGE_MS
    report = run_mod.run_payload(target=target, dry_run=True, list_ids=True, scheduled=False, now_ms=within)
    assert report["read_stats"] == {SCOPE: "cached"}
    scores = {item["card_id"]: item["score"] for item in report["selected"]}
    expected = pick.access(pick.Reads(50, 100, 400)) - pick.access(pick.Reads())
    assert scores["a"] - scores["b"] == pytest.approx(expected, abs=1e-3)
    # The snapshot shows reads for a and none for b, yet the never-read report stays unknown.
    assert report["never_read"] == {"count": None, "by_scope": {SCOPE: None}, "card_ids": []}
    beyond = within + 1
    report = run_mod.run_payload(target=target, dry_run=True, list_ids=True, scheduled=False, now_ms=beyond)
    assert report["read_stats"] == {SCOPE: "unavailable"} and report["selected"] == []
    # The dry runs wrote nothing, so the snapshot is unchanged.
    assert snapshot(target)[0] == first


def test_a_snapshot_from_another_binding_is_never_used(verify_hub):
    target, hub, _, _ = verify_hub
    counted(hub, {"a": (50, 100, 400)})
    now = int(time.time() * 1000)
    run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=now)
    state = state_of(target)
    state["binding"] = "f" * 64
    vstate.save(target, state)
    hub.do_read_stats = lambda body: (429, {"code": hubc.READ_STATS_RATE_LIMITED, "error": "slow"})
    report = run_mod.run_payload(target=target, dry_run=True, list_ids=False, scheduled=False, now_ms=now + 1)
    assert report["read_stats"] == {SCOPE: "unavailable"}


# --- The care entry ------------------------------------------------------------------------


def care_install(target, capsys, *entries):
    args = ["memory", "care", "install", "--target", str(target), "--json"]
    for entry in entries:
        args += ["--entry", entry]
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.err


def test_the_care_entry_runs_scheduled_verification_at_11(verify_hub, capsys):
    target, _, _, home = verify_hub
    code, err = care_install(target, capsys, "memory-verify")
    assert code == 0, err
    (unit,) = (home / ".config/systemd/user").glob("*memory-verify.service")
    argv = shlex.split(next(line for line in unit.read_text().splitlines() if line.startswith("ExecStart="))[10:])
    assert argv[-6:] == ["memory", "verify", "run", "--scheduled", "--target", str(target.resolve())]
    (timer,) = (home / ".config/systemd/user").glob("*memory-verify.timer")
    assert "OnCalendar=*-*-* 11:00:00" in timer.read_text()


def test_installing_into_the_reflect_jobs_folder_is_refused(verify_hub, capsys):
    target, hub, fake, home = verify_hub
    (target / run_mod.REFLECT_FOLDER).mkdir(parents=True)
    code, err = care_install(target, capsys, "memory-verify")
    assert code == 2 and run_mod.REFLECT_REFUSAL in err
    assert not list(home.glob("**/*memory-verify*"))
    # A run there is refused too, before any hub call, and it counts toward the pause.
    status, report = verify(target, capsys)
    assert status == 1 and report["code"] == "verify_reflect_folder" and hub.calls == []
    assert report["schedule"]["consecutive_failures"] == 1


def test_installing_beside_memory_reflect_is_refused(verify_hub, capsys):
    target, _, _, _ = verify_hub
    code, err = care_install(target, capsys, "memory-reflect", "memory-verify")
    assert code == 2 and "separate folders" in err


def test_installing_without_an_explicit_verifier_credential_is_refused(verify_hub, capsys, monkeypatch):
    target, _, _, _ = verify_hub
    # No memory hub override: the folder would fall back to the fleet node token.
    monkeypatch.setattr(hubc, "_memory_hub_override", lambda fleet: None)
    code, err = care_install(target, capsys, "memory-verify")
    assert code == 2 and "never the fleet node or admin token" in err

    def admin_override(fleet):
        raise hubc.HubConfigError("the memory hub override must not use a fleet credential")

    monkeypatch.setattr(hubc, "_memory_hub_override", admin_override)
    code, err = care_install(target, capsys, "memory-verify")
    assert code == 2 and "not a fleet credential" in err


def test_care_status_reads_the_verify_pause_state(verify_hub, capsys):
    target, hub, _, _ = verify_hub
    hub.mode = "down"
    for _ in range(3):
        verify(target, capsys, "--scheduled")
    from brigade import care_cmd

    status = care_cmd._entry_status(target, care_cmd._catalog_by_id()["memory-verify"])
    assert status["paused"] is True and status["consecutive_failures"] == 3 and status["kind"] == "verify"
