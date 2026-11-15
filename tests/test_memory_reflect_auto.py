"""Workspace opt-in and candidate-boundary automatic reflection."""

import pytest

from brigade import cli, memory_reflect_policy as policy
from tests.test_memory_reflect_hub import run, setup as reflect_setup, writes

setup = pytest.fixture(reflect_setup.__wrapped__)


def test_new_install_defaults_and_reinstall_preserves_off(tmp_path, capsys):
    args = ["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "none"]
    assert cli.main(args) == 0
    assert policy.load(tmp_path)["auto_apply"] is True
    assert cli.main(["memory", "reflect", "auto-apply", "off", "--target", str(tmp_path)]) == 0
    assert cli.main(args) == 0
    assert policy.load(tmp_path)["auto_apply"] is False


def test_existing_install_untouched(tmp_path, capsys):
    args = ["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "none"]
    assert cli.main(args) == 0
    (tmp_path / policy.PATH).unlink()
    assert cli.main(args) == 0
    assert not (tmp_path / policy.PATH).exists()


def test_missing_policy_is_off(tmp_path):
    assert policy.allowed(tmp_path, []) == "reflect_policy_unavailable"


def test_gate_hold_is_completed_proposal(setup, capsys):
    target, hub, _ = setup
    policy.set_enabled(target, True)
    hub.do_auto_apply = lambda body: (
        200,
        {
            "state": "pending",
            "auto_apply_gates": [{"gate": "retrieval_eval", "state": "held", "code": "retrieval_eval_unbound"}],
        },
    )
    before = dict(hub.cards)
    code, report = run(target, capsys, "run", "--scheduled")
    assert code == 0
    assert report["proposals"][0]["automatic"]["state"] == "pending"
    assert report["proposals"][0]["automatic"]["checks"][0]["code"] == "retrieval_eval_unbound"
    assert hub.cards == before
    assert [c["action"] for c in writes(hub)] == ["propose", "auto_apply"]


def test_off_switch_read_at_each_candidate(tmp_path):
    policy.set_enabled(tmp_path, True)
    assert policy.allowed(tmp_path, [{"type": "append_evidence"}]) is None
    policy.set_enabled(tmp_path, False)
    assert policy.allowed(tmp_path, [{"type": "append_evidence"}]) == "auto_apply_disabled"


def test_hub_rollback_still_uses_compensating_proposal(setup, capsys):
    target, hub, _ = setup
    code, report = run(target, capsys, "rollback", "00000000000000000000000000000000")
    assert code == 0
    assert report["status"] == "proposed"
    assert writes(hub)[-1]["action"] == "rollback"


def test_successful_auto_apply_uses_hub_authority(setup, capsys):
    target, hub, _ = setup
    policy.set_enabled(target, True)
    hub.do_auto_apply = lambda body: (200, {"state": "applied", "auto_apply_gates": []})
    code, report = run(target, capsys, "run", "--apply")
    assert code == 0
    assert report["proposals"][0]["automatic"]["state"] == "applied"
    assert [c["action"] for c in writes(hub)] == ["propose", "auto_apply"]


def test_dry_run_never_attempts_application(setup, capsys):
    target, hub, _ = setup
    policy.set_enabled(target, True)
    assert run(target, capsys, "run", "--apply", "--dry-run")[0] == 0
    assert writes(hub) == []


def test_old_workspace_scheduled_run_never_applies(setup, capsys):
    target, hub, _ = setup
    assert run(target, capsys, "run", "--scheduled", "--apply")[0] == 0
    assert [c["action"] for c in writes(hub)] == ["propose"]


def test_external_owner_gate_refusal_falls_back(setup, capsys):
    target, hub, _ = setup
    policy.set_enabled(target, True)
    hub.do_auto_apply = lambda body: (422, {"code": "memory_owner_authority_unbound", "error": "held"})
    before = dict(hub.cards)
    code, report = run(target, capsys, "run", "--scheduled", "--apply")
    assert code == 0
    assert report["proposals"][0]["automatic"]["reason"] == "memory_owner_authority_unbound"
    assert hub.cards == before


def test_opt_in_revisits_pending_proposal_without_resubmitting(setup, capsys):
    target, hub, _ = setup
    assert run(target, capsys, "run")[0] == 0
    policy.set_enabled(target, True)
    hub.do_auto_apply = lambda body: (200, {"state": "pending", "auto_apply_gates": []})
    assert run(target, capsys, "run", "--apply")[0] == 0
    assert [c["action"] for c in writes(hub)] == ["propose", "auto_apply"]


def test_switch_stops_inflight_before_second_candidate(tmp_path):
    policy.set_enabled(tmp_path, True)

    class Client:
        calls = 0

        def call(self, action, **fields):
            self.calls += 1
            policy.set_enabled(tmp_path, False)
            return {"state": "applied"}

    client = Client()
    ops = [{"type": "append_evidence"}]
    assert policy.attempt(tmp_path, client, "first", ops, "epoch")["state"] == "applied"
    assert policy.attempt(tmp_path, client, "second", ops, "epoch")["reason"] == "auto_apply_disabled"
    assert client.calls == 1


def test_local_gate_failure_records_hold_without_changing_cards(tmp_path, capsys):
    import json

    from tests.test_memory_reflect import card, command, evidence, log, snapshot

    path = card(tmp_path, "cache")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/support.json"), "outcome", "2026-10-20T12:00:00Z"))
    policy.set_enabled(tmp_path, True)
    before = snapshot(tmp_path)
    code, report = command(tmp_path, capsys, "run", "--scheduled", "--apply")
    assert code == 0
    assert report["automatic"]
    assert all(item["reason"] == "local_authority_unbound" for item in report["automatic"])
    assert snapshot(tmp_path) == before
    saved = tmp_path / f".brigade/memory/reflect/runs/{report['run_id']}/automatic.json"
    assert json.loads(saved.read_text()) == report["automatic"]


@pytest.mark.parametrize("reason", ["owner_not_hub", "auto_apply_killed", "retrieval_eval_unbound"])
def test_authority_hold_never_changes_cards(setup, capsys, reason):
    target, hub, _ = setup
    policy.set_enabled(target, True)
    hub.do_auto_apply = lambda body: (
        200,
        {"state": "pending", "auto_apply_gates": [{"gate": "authority", "state": "held", "code": reason}]},
    )
    before = dict(hub.cards)
    code, report = run(target, capsys, "run", "--scheduled", "--apply")
    assert code == 0
    assert report["proposals"][0]["automatic"]["checks"][0]["code"] == reason
    assert hub.cards == before


def test_policy_seed_preserves_portable_init(tmp_path, monkeypatch):
    import json
    import os

    monkeypatch.setattr(os, "supports_dir_fd", set())
    policy.ensure(tmp_path)
    assert json.loads((tmp_path / policy.PATH).read_text())["auto_apply"] is True
    # Reflection itself still fails closed without safe descriptor support.
    assert policy.allowed(tmp_path, []) == "reflect_policy_unavailable"


def test_care_reinstall_preserves_missing_policy(setup, capsys):
    from brigade import care_cmd

    target, _, home = setup
    # Simulate an older initialized workspace upgrading before its care reinstall.
    (target / ".brigade/config.json").write_text('{"depth":"repo","harnesses":[],"owner":"none","includes":[]}')
    assert cli.main(["init", "--target", str(target), "--depth", "repo", "--harnesses", "none"]) == 0
    assert not (target / policy.NEW_WORKSPACE_PATH).exists()
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["memory-reflect"]) == 0
    assert not (target / policy.PATH).exists()
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["memory-reflect"]) == 0
    capsys.readouterr()
    assert run(target, capsys, "run", "--scheduled")[0] == 0
    assert [c["action"] for c in writes(setup[1])] == ["propose"]
    policy.set_enabled(target, False)
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["memory-reflect"]) == 0
    assert policy.load(target)["auto_apply"] is False


def test_existing_state_without_config_is_not_new(tmp_path):
    (tmp_path / ".brigade/memory").mkdir(parents=True)
    assert cli.main(["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "none"]) == 0
    assert not (tmp_path / policy.PATH).exists()


def test_init_writes_versioned_marker(tmp_path):
    import json

    assert cli.main(["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "none"]) == 0
    assert json.loads((tmp_path / policy.NEW_WORKSPACE_PATH).read_text())["schema"] == "brigade.new-workspace.v1"


def test_explicit_care_flag_and_command_enable(setup, capsys):
    target, _, home = setup
    assert (
        cli.main(
            [
                "care",
                "install",
                "--target",
                str(target),
                "--entry",
                "memory-reflect",
                "--reflect-auto-apply",
                "--dry-run",
            ]
        )
        == 0
    )
    assert not (target / policy.PATH).exists()
    from brigade import care_cmd

    assert (
        care_cmd.install(
            target=target, backend="systemd", home=home, entry_ids=["memory-reflect"], reflect_auto_apply=True
        )
        == 0
    )
    assert policy.load(target)["auto_apply"] is True
    capsys.readouterr()
    assert run(target, capsys, "auto-apply", "off")[0] == 0
    assert run(target, capsys, "auto-apply", "on")[0] == 0
    assert policy.load(target)["auto_apply"] is True


@pytest.mark.parametrize("enabled", [False, True])
def test_cached_proposals_do_not_starve_new_work(setup, capsys, enabled):
    target, hub, _ = setup
    if enabled:
        policy.set_enabled(target, True)
        hub.do_auto_apply = lambda body: (200, {"state": "pending"})
    for ident in ("c", "d"):
        hub._commit(
            "card.put", "fleet", ident, "---\nevidence: []\n---\nSame claim. ^c-abcdef\n", "live", "event-" + ident
        )
    for _ in range(3):
        assert run(target, capsys, "run", "--scheduled", "--limit", "1")[0] == 0
    assert len([c for c in writes(hub) if c["action"] == "propose"]) == 3


def test_off_repairs_invalid_policy(tmp_path):
    policy.ensure(tmp_path)
    (tmp_path / policy.PATH).write_text("invalid")
    assert policy.set_enabled(tmp_path, False)["auto_apply"] is False


def test_hub_diagnostics_are_bounded(tmp_path):
    policy.set_enabled(tmp_path, True)

    class Client:
        def call(self, *args, **kwargs):
            return {
                "state": "untrusted text",
                "reason": "untrusted text",
                "auto_apply_gates": [
                    {"gate": "retrieval", "state": "held", "code": "safe_code", "text": "untrusted text"}
                ]
                * 100,
            }

    result = policy.attempt(tmp_path, Client(), "proposal", [], "epoch")
    assert result["state"] == "pending"
    assert result["reason"] is None
    assert len(result["checks"]) <= 32
    assert "untrusted text" not in str(result)


def test_lost_hub_reply_is_unknown(tmp_path):
    from brigade.memory_hub_client import HubUnavailable

    policy.set_enabled(tmp_path, True)

    class Client:
        def call(self, *args, **kwargs):
            raise HubUnavailable("response lost")

    assert policy.attempt(tmp_path, Client(), "proposal", [], "epoch") == {
        "state": "unknown",
        "reason": "auto_apply_unavailable",
    }
