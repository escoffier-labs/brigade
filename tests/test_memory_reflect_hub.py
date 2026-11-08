"""Scheduled hub reflection only opens reviewed proposals."""

from __future__ import annotations

import json
import os
import shlex

import pytest

from brigade import care_cmd, cli, memory_hub_client as hubc
from tests._home import set_home
from tests.test_memory_hub_client import FakeHub, HUB_URL, NODE_CREDENTIAL

ORIGINAL_OVERRIDE = hubc._memory_hub_override

pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)


class ReflectHub(FakeHub):
    def __init__(self):
        super().__init__()
        self.reject = False

    def do_card(self, body):
        status, payload = super().do_card(body)
        if status == 200:
            payload["card"]["scan_state"] = getattr(self, "scan_states", {}).get(
                (body["scope"], body["card_id"]), "clean"
            )
            payload["card"]["trust"] = getattr(self, "trust_classes", {}).get(
                (body["scope"], body["card_id"]), payload["card"]["trust"]
            )
        return status, payload

    def do_propose(self, body):
        assert body["base_epoch"] == self.epoch
        if self.reject:
            return 422, {"code": "memory_proposal_invalid", "error": "invalid ops"}
        return 201, {"proposal_id": hubc.event_id(), "state": "pending", "diff": "review required"}

    def do_rollback(self, body):
        assert body["base_epoch"] == self.epoch
        return 201, {"proposal_id": hubc.event_id(), "state": "pending", "diff": "compensation"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    for name in ("BRIGADE_MEMORY_HUB_URL", "BRIGADE_MEMORY_HUB_TOKEN_FILE", "BRIGADE_FLEET_NODE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "workspace"
    (target / ".brigade").mkdir(parents=True)
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["fleet"]\n')
    hub = ReflectHub()
    for ident, anchor in (("a", "c-abcdef"), ("b", "c-fedcba")):
        hub._commit(
            "card.put", "fleet", ident, f"---\nevidence: []\n---\nSame claim. ^{anchor}\n", "live", hubc.event_id()
        )
    monkeypatch.setattr(hubc, "_memory_hub_override", lambda fleet: (HUB_URL, NODE_CREDENTIAL, "env"))
    monkeypatch.setattr(hubc, "urllib_transport", hub)
    return target, hub, home


def run(target, capsys, *args):
    code = cli.main(["memory", "reflect", *args, "--target", str(target), "--json"])
    return code, json.loads(capsys.readouterr().out)


def writes(hub):
    return [c for c in hub.calls if c["action"] not in {"status", "log", "card"}]


def test_dry_run_has_zero_hub_writes(setup, capsys):
    target, hub, _ = setup
    before = dict(hub.cards)
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0
    assert report["candidate_count"] == 1
    assert writes(hub) == []
    assert hub.cards == before


def test_run_submits_pinned_proposals_only(setup, capsys):
    target, hub, _ = setup
    code, report = run(target, capsys, "run")
    assert code == 0
    assert len(report["proposals"]) == 1
    assert [c["action"] for c in writes(hub)] == ["propose"]
    op = writes(hub)[0]["ops"][0]
    assert op["base_revision"] == 1
    assert op["other_card"] == {"scope": "fleet", "card_id": "b", "base_revision": 2}
    assert op["block_id"] == "c-abcdef"
    assert hub.cards[("fleet", "b")]["state"] == "live"


def test_three_failures_pause_scheduled_calls_until_success(setup, capsys):
    target, hub, _ = setup
    hub.mode = "down"
    for count in (1, 2, 3):
        assert run(target, capsys, "run", "--hub", "--scheduled")[0] != 0
        schedule = run(target, capsys, "status")[1]["schedule"]
        assert schedule["consecutive_failures"] == count
        assert schedule["paused"] == (count == 3)
    calls = len(hub.calls)
    assert run(target, capsys, "run", "--hub", "--scheduled")[1]["status"] == "paused"
    assert len(hub.calls) == calls
    entry = care_cmd._catalog_by_id()["memory-reflect"]
    assert care_cmd._entry_status(target, entry)["paused"]
    assert cli.main(["memory", "care", "status", "--target", str(target), "--json"]) in (0, 1)
    assert json.loads(capsys.readouterr().out)["reflect"]["paused"]
    hub.mode = "up"
    assert run(target, capsys, "run", "--dry-run")[0] == 0
    assert run(target, capsys, "status")[1]["schedule"]["consecutive_failures"] == 3
    assert run(target, capsys, "run")[0] == 0
    assert run(target, capsys, "status")[1]["schedule"]["consecutive_failures"] == 0
    assert run(target, capsys, "run", "--hub", "--scheduled")[0] == 0


def test_rollback_requests_compensation_only(setup, capsys):
    target, hub, _ = setup
    assert run(target, capsys, "rollback", hubc.event_id())[0] == 0
    assert [c["action"] for c in writes(hub)] == ["rollback"]
    assert all(c["state"] == "live" for c in hub.cards.values())


def test_care_install_memory_reflect(setup, capsys):
    target, _, home = setup
    assert cli.main(["memory", "care", "install", "--entry", "memory-reflect", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    units = list((home / ".config/systemd/user").glob("*memory-reflect.service"))
    assert len(units) == 1
    body = units[0].read_text()
    argv = shlex.split(
        next(line.removeprefix("ExecStart=") for line in body.splitlines() if line.startswith("ExecStart="))
    )
    assert argv[-7:] == ["memory", "reflect", "run", "--hub", "--scheduled", "--target", str(target)]
    assert "decide" not in body and "apply" not in body
    environment = [line for line in body.splitlines() if line.startswith("Environment=")]
    assert len(environment) == 1 and "PATH=" in environment[0]
    assert "BRIGADE_MEMORY_HUB" not in body and "BRIGADE_HOME" not in body


def test_repeat_does_not_open_duplicate_proposals(setup, capsys):
    target, hub, _ = setup
    assert run(target, capsys, "run")[0] == 0
    assert run(target, capsys, "run")[0] == 0
    assert len(writes(hub)) == 1


def test_lost_response_replays_the_same_event(setup, capsys, monkeypatch):
    target, hub, _ = setup
    original = hub.do_propose
    requests = []

    def lose_response(body):
        requests.append(dict(body))
        if len(requests) == 1:
            raise ConnectionResetError("lost response")
        return original(body)

    monkeypatch.setattr(hub, "do_propose", lose_response)
    assert run(target, capsys, "run")[0] == 1
    assert run(target, capsys, "run")[0] == 0
    assert requests[0] == requests[1]


def test_disabled_hub_never_falls_back_in_scheduled_job(setup, capsys):
    target, hub, _ = setup
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="off"\n')
    assert run(target, capsys, "run", "--hub", "--scheduled")[0] == 1
    assert hub.calls == []
    assert run(target, capsys, "status")[1]["schedule"]["consecutive_failures"] == 1


def test_workspace_cannot_redirect_credentials(setup, capsys):
    target, hub, _ = setup
    with (target / ".brigade/memory-care.toml").open("a") as handle:
        handle.write('hub_url="https://example.com"\nnode_token_file="/tmp/example-token"\n')
    assert run(target, capsys, "run", "--hub")[0] == 1
    assert hub.calls == []


def test_no_valid_pins_is_successful_skip(setup, capsys):
    target, hub, _ = setup
    for ident in ("a", "b"):
        hub._commit("card.put", "fleet", ident, "Same claim.\n", "live", hubc.event_id())
    for _ in range(4):
        code, report = run(target, capsys, "run")
        assert code == 0
        assert report["skipped_reasons"] == {"missing_pin": 1}
        assert not report["schedule"]["paused"]
    assert writes(hub) == []


def test_duplicate_comparison_preserves_opaque_whitespace(setup, capsys):
    target, hub, _ = setup
    for ident, indent in (("a", "    "), ("b", "")):
        hub._commit(
            "card.put",
            "fleet",
            ident,
            f"Claim. ^c-abcdef\n\n```python\nif ready:\n{indent}finish()\n```\n",
            "live",
            hubc.event_id(),
        )
    assert run(target, capsys, "run")[0] == 0
    assert writes(hub) == []


def test_revoked_read_does_not_fall_back_to_cached_cards(setup, capsys):
    target, hub, _ = setup
    assert run(target, capsys, "run", "--dry-run")[0] == 0
    hub.grants["node-1"] = set()
    assert run(target, capsys, "run")[0] == 1
    assert writes(hub) == []


def test_other_scheduler_backends_pin_the_reflect_command(setup):
    import base64
    import plistlib

    target, _, home = setup
    entry = care_cmd._catalog_by_id()["memory-reflect"]
    plists = care_cmd._launchd_plists(workspace=target, home=home, entries=(entry,))
    argv = plistlib.loads(next(iter(plists.values())))["ProgramArguments"]
    assert argv[-7:] == ["memory", "reflect", "run", "--hub", "--scheduled", "--target", str(target)]
    assert set(plistlib.loads(next(iter(plists.values())))["EnvironmentVariables"]) == {"PATH"}
    cron = care_cmd._cron_command(entry, workspace=target)
    assert '"reflect" "run" "--hub" "--scheduled"' in cron
    windows = care_cmd._schtasks_create_command(entry, workspace=target)
    decoded = base64.b64decode(windows.split("-EncodedCommand ")[1]).decode("utf-16le")
    assert "reflect run --hub --scheduled" in decoded


@pytest.mark.parametrize("source", ["env", "table"])
def test_reflect_uses_user_hub_settings(setup, capsys, monkeypatch, source):
    from brigade import fleet_client

    target, hub, home = setup
    token = home / "memory-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(NODE_CREDENTIAL)
    token.chmod(0o600)
    monkeypatch.setattr(hubc, "_memory_hub_override", ORIGINAL_OVERRIDE)
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {})
    if source == "env":
        monkeypatch.setenv("BRIGADE_MEMORY_HUB_URL", HUB_URL)
        monkeypatch.setenv("BRIGADE_MEMORY_HUB_TOKEN_FILE", str(token))
    else:
        config = home / ".brigade/fleet.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f"[memory_hub]\nhub_url={json.dumps(HUB_URL)}\nnode_token_file={json.dumps(str(token))}\n")
    assert run(target, capsys, "run")[0] == 0
    assert hub.tokens == {NODE_CREDENTIAL}
    assert [c["action"] for c in writes(hub)] == ["propose"]


def test_dry_runs_leave_pause_and_counter_unchanged(setup, capsys):
    target, hub, _ = setup
    hub.mode = "down"
    for count in (1, 2, 3):
        assert run(target, capsys, "run")[0] == 1
        assert run(target, capsys, "status")[1]["schedule"]["consecutive_failures"] == count
    state = (target / ".brigade/memory/reflect/schedule.json").read_bytes()
    assert run(target, capsys, "run", "--dry-run")[0] == 1
    assert (target / ".brigade/memory/reflect/schedule.json").read_bytes() == state
    hub.mode = "up"
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0 and report["candidate_count"] == 1
    assert report["schedule"]["paused"]
    assert (target / ".brigade/memory/reflect/schedule.json").read_bytes() == state
    assert writes(hub) == []


def test_incremental_log_keeps_sequence_watermark(setup, capsys):
    target, hub, _ = setup
    assert run(target, capsys, "run", "--dry-run")[0] == 0
    before = len(hub.calls)
    assert run(target, capsys, "run", "--dry-run")[0] == 0
    logs = [c for c in hub.calls[before:] if c["action"] == "log"]
    assert logs and logs[0]["after_seq"] == 2


def test_unsafe_state_is_refused_without_hub_calls(setup, capsys, tmp_path):
    target, hub, _ = setup
    external = tmp_path / "external"
    external.mkdir()
    state_dir = target / ".brigade/memory"
    state_dir.mkdir()
    (state_dir / "reflect").symlink_to(external, target_is_directory=True)
    assert run(target, capsys, "run")[0] == 2
    assert hub.calls == []
    assert list(external.iterdir()) == []


def add_group(hub, scope="fleet", prefix="x", body="Another claim."):
    for suffix, anchor in (("a", "c-abcdef"), ("b", "c-fedcba")):
        hub._commit(
            "card.put", scope, prefix + suffix, f"---\nevidence: []\n---\n{body} ^{anchor}\n", "live", hubc.event_id()
        )


@pytest.mark.parametrize("narrow", [("--scope", "fleet"), ("--limit", "1")])
def test_narrow_run_preserves_unprocessed_submissions(setup, capsys, narrow):
    target, hub, _ = setup
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["fleet","repo:example"]\n')
    add_group(hub, scope="repo:example")
    assert run(target, capsys, "run")[0] == 0
    assert len(writes(hub)) == 2
    code, report = run(target, capsys, "run", *narrow)
    if narrow[0] == "--scope":
        assert code == 1 and report["error"] == "hub_reflect_dedicated_replica_required"
    else:
        assert code == 0
    assert run(target, capsys, "run")[0] == 0
    assert len(writes(hub)) == 2


def test_recorded_proposals_do_not_consume_limit(setup, capsys):
    target, hub, _ = setup
    add_group(hub)
    assert run(target, capsys, "run", "--limit", "1")[0] == 0
    assert run(target, capsys, "run", "--limit", "1")[0] == 0
    assert [c["ops"][0]["card_id"] for c in writes(hub)] == ["a", "xa"]


@pytest.mark.parametrize("code", ["memory_client_event_expired", "memory_client_event_conflict"])
def test_bad_event_id_is_replaced_in_same_run(setup, capsys, monkeypatch, code):
    target, hub, _ = setup
    original = hub.do_propose
    attempts = []

    def refuse_once(body):
        attempts.append(body["client_event_id"])
        if len(attempts) == 1:
            return 409, {"code": code, "error": "must not retain this text"}
        return original(body)

    monkeypatch.setattr(hub, "do_propose", refuse_once)
    result, report = run(target, capsys, "run")
    assert result == 0
    assert report["refused_ops"][0]["code"] == code
    state = json.loads((target / ".brigade/memory/reflect/schedule.json").read_text())
    [pending] = state["submissions"].values()
    assert pending["client_event_id"] == attempts[-1]
    assert "proposal_id" in pending
    assert len(report["proposals"]) == 1
    assert run(target, capsys, "run")[0] == 0
    assert len(attempts) == 2 and attempts[0] != attempts[1]


@pytest.mark.parametrize("code", ["memory_proposal_invalid", "memory_content_blocked"])
def test_op_refusal_does_not_block_other_ops(setup, capsys, monkeypatch, code):
    target, hub, _ = setup
    add_group(hub)
    original = hub.do_propose

    def refuse_one(body):
        if body["ops"][0]["card_id"] == "a":
            return 422, {"code": code, "error": "must not retain this text"}
        return original(body)

    monkeypatch.setattr(hub, "do_propose", refuse_one)
    result, report = run(target, capsys, "run")
    assert result == 0
    assert len(report["proposals"]) == 1 and len(report["refused_ops"]) == 1
    assert report["refused_ops"][0]["code"] == code
    assert set(report["refused_ops"][0]) == {"op_digest", "code"}
    assert "must not retain" not in json.dumps(report)
    assert "must not retain" not in (target / ".brigade/memory/reflect/schedule.json").read_text()
    assert [c["ops"][0]["card_id"] for c in writes(hub)] == ["a", "xa"]


def test_authorization_refusal_stops_and_counts_failure(setup, capsys, monkeypatch):
    target, hub, _ = setup
    add_group(hub)
    monkeypatch.setattr(hub, "do_propose", lambda body: (403, {"code": "memory_grant_missing", "error": "private"}))
    result, report = run(target, capsys, "run")
    assert result == 1 and report["schedule"]["consecutive_failures"] == 1
    assert len(writes(hub)) == 1
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "bad",
    [
        {"binding": []},
        {"binding": "invalid"},
        {"submissions": []},
        {"binding": "a" * 64, "submissions": {"b" * 64: []}},
        {"binding": "a" * 64, "submissions": {"b" * 64: {}}},
        {"binding": "a" * 64, "submissions": {"b" * 64: {"client_event_id": 7}}},
        {"binding": "a" * 64, "submissions": {"b" * 64: {"client_event_id": "invalid"}}},
        {"binding": "a" * 64, "submissions": {"b" * 64: {"client_event_id": hubc.event_id(), "proposal_id": []}}},
        {"paused": 0},
    ],
)
def test_invalid_state_is_reported_without_traceback(setup, capsys, bad):
    from brigade import memory_reflect_hub as reflect

    target, hub, _ = setup
    reflect._save(target, {"consecutive_failures": 0, "paused": False, **bad})
    code, report = run(target, capsys, "status")
    assert code == 0
    assert report["schedule"]["paused"] and "state_error" in report["schedule"]
    assert run(target, capsys, "run")[0] == 2
    assert hub.calls == []


def test_corrupt_json_state_reports_recovery_without_traceback(setup, capsys):
    from brigade import memory_reflect_hub as reflect

    target, hub, _ = setup
    reflect._save(target, {"consecutive_failures": 0, "paused": False})
    path = target / reflect.STATE
    path.write_text("{")
    assert "state_error" in run(target, capsys, "status")[1]["schedule"]
    code, report = run(target, capsys, "run")
    assert code == 2 and "schedule.json" in report["error"]
    assert hub.calls == []
    path.unlink()
    assert run(target, capsys, "run")[0] == 0


@pytest.mark.parametrize("scan", ["flagged", "private", "blocked", "failed", None])
def test_non_clean_groups_are_skipped(setup, capsys, scan):
    target, hub, _ = setup
    hub.scan_states = {("fleet", "b"): scan}
    code, report = run(target, capsys, "run")
    assert code == 0 and writes(hub) == []
    assert report["skipped_reasons"] == {"non_clean": 1}


def test_mixed_trust_group_is_skipped(setup, capsys):
    target, hub, _ = setup
    hub.trust_classes = {("fleet", "b"): "reviewed"}
    code, report = run(target, capsys, "run")
    assert code == 0 and writes(hub) == []
    assert report["skipped_reasons"] == {"mixed_trust": 1}


def test_partial_group_validation_drops_all_group_ops(setup, capsys):
    target, hub, _ = setup
    hub._commit(
        "card.put", "fleet", "c", "---\nevidence: invalid\n---\nSame claim. ^c-cccccc\n", "live", hubc.event_id()
    )
    code, report = run(target, capsys, "run")
    assert code == 0
    assert writes(hub) == []
    assert report["skipped_reasons"] == {"invalid_metadata": 2}


def test_fleet_only_credentials_are_refused_before_hub_call(setup, capsys, monkeypatch):
    from brigade import fleet_client

    target, hub, _ = setup
    monkeypatch.setattr(hubc, "_memory_hub_override", ORIGINAL_OVERRIDE)
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": HUB_URL, "node_token": NODE_CREDENTIAL}
    )
    code, report = run(target, capsys, "run")
    assert code == 1
    assert "explicit" in report["error"] and "memory_hub" in report["error"]
    assert hub.calls == []


def test_dry_run_text_reports_paused_state(setup, capsys):
    target, hub, _ = setup
    hub.mode = "down"
    for _ in range(3):
        assert run(target, capsys, "run")[0] == 1
    hub.mode = "up"
    code = cli.main(["memory", "reflect", "run", "--dry-run", "--target", str(target)])
    assert code == 0
    assert "paused=true" in capsys.readouterr().out


def test_text_output_records_refusal_codes_only(setup, capsys, monkeypatch):
    target, hub, _ = setup
    monkeypatch.setattr(
        hub, "do_propose", lambda body: (422, {"code": "memory_proposal_invalid", "error": "private card text"})
    )
    assert cli.main(["memory", "reflect", "run", "--target", str(target)]) == 1
    output = capsys.readouterr().out
    assert "memory_proposal_invalid" in output
    assert "private card text" not in output


def test_narrow_run_preserves_unprocessed_pending_id(setup, capsys, monkeypatch):
    target, hub, _ = setup
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["fleet","repo:example"]\n')
    add_group(hub, scope="repo:example")
    original = hub.do_propose
    attempts = []

    def lose_response_once(body):
        if body["ops"][0]["scope"] == "repo:example":
            attempts.append(dict(body))
            if len(attempts) == 1:
                raise ConnectionResetError("lost response")
        return original(body)

    monkeypatch.setattr(hub, "do_propose", lose_response_once)
    assert run(target, capsys, "run")[0] == 1
    code, report = run(target, capsys, "run", "--scope", "fleet")
    assert code == 1 and report["error"] == "hub_reflect_dedicated_replica_required"
    assert run(target, capsys, "run")[0] == 0
    assert len(attempts) == 2 and attempts[0] == attempts[1]


def test_epoch_change_does_not_reuse_recorded_proposals(setup, capsys):
    target, hub, _ = setup
    assert run(target, capsys, "run")[0] == 0
    hub.epoch = hubc.event_id()
    # Complete the restore rebuild first. Reflect refuses the pull's warning,
    # so this test isolates proposal binding from the existing pull contract.
    from brigade import memory_hub_sync as sync

    settings = hubc.load_settings(target)
    sync.pull(target, hubc.client_for(settings), settings)
    assert run(target, capsys, "run")[0] == 0
    assert len(writes(hub)) == 2
    assert writes(hub)[0]["base_epoch"] != writes(hub)[1]["base_epoch"]


def test_clean_group_keeps_lowest_id_with_matching_trust(setup, capsys):
    target, hub, _ = setup
    hub._commit("card.put", "fleet", "c", "---\nevidence: []\n---\nSame claim. ^c-cccccc\n", "live", hubc.event_id())
    hub.trust_classes = {("fleet", ident): "reviewed" for ident in ("a", "b", "c")}
    assert run(target, capsys, "run")[0] == 0
    assert [(c["ops"][0]["card_id"], c["ops"][0]["other_card"]["card_id"]) for c in writes(hub)] == [
        ("a", "b"),
        ("a", "c"),
    ]


@pytest.mark.parametrize("code", ["memory_content_blocked", "memory_proposal_invalid", "memory_base_mismatch"])
def test_permanent_refusal_is_skipped_on_later_runs(setup, capsys, monkeypatch, code):
    target, hub, _ = setup
    add_group(hub)
    original = hub.do_propose

    def refuse_one(body):
        if body["ops"][0]["card_id"] == "a":
            return 422, {"code": code, "error": "do not retain card text"}
        return original(body)

    monkeypatch.setattr(hub, "do_propose", refuse_one)
    result, report = run(target, capsys, "run")
    assert result == 0 and len(report["proposals"]) == 1
    state = json.loads((target / ".brigade/memory/reflect/schedule.json").read_text())
    saved = [entry for entry in state["submissions"].values() if "refused" in entry]
    assert len(saved) == 1 and saved[0]["refused"] == code
    assert set(saved[0]) == {"client_event_id", "refused"}
    assert "do not retain" not in json.dumps(state)
    for _ in range(3):
        result, report = run(target, capsys, "run")
        assert result == 0 and report["candidate_count"] == 0
        assert report["refused_ops"] == []
        assert report["skipped_reasons"] == {"already_proposed": 1, "refused": 1}
        assert not report["schedule"]["paused"]
    assert len(writes(hub)) == 2


def test_remembered_refusal_does_not_consume_limit(setup, capsys, monkeypatch):
    target, hub, _ = setup
    add_group(hub)
    original = hub.do_propose

    def refuse_one(body):
        if body["ops"][0]["card_id"] == "a":
            return 422, {"code": "memory_proposal_invalid"}
        return original(body)

    monkeypatch.setattr(hub, "do_propose", refuse_one)
    result, report = run(target, capsys, "run", "--limit", "1")
    assert result == 1 and report["error"] == "all_ops_refused"
    result, report = run(target, capsys, "run", "--limit", "1")
    assert result == 0 and len(report["proposals"]) == 1
    assert report["skipped_reasons"] == {"refused": 1}
    assert [c["ops"][0]["card_id"] for c in writes(hub)] == ["a", "xa"]
    assert not report["schedule"]["paused"]


def test_card_revision_change_reenables_refused_op(setup, capsys, monkeypatch):
    target, hub, _ = setup
    original = hub.do_propose
    attempts = []

    def refuse_once(body):
        attempts.append(body)
        if len(attempts) == 1:
            return 422, {"code": "memory_content_blocked"}
        return original(body)

    monkeypatch.setattr(hub, "do_propose", refuse_once)
    assert run(target, capsys, "run")[0] == 1
    text = hub.cards[("fleet", "a")]["text"]
    hub._commit("card.put", "fleet", "a", text, "live", hubc.event_id())
    assert run(target, capsys, "run")[0] == 0
    assert len(attempts) == 2
    assert attempts[0]["ops"][0]["base_revision"] != attempts[1]["ops"][0]["base_revision"]


@pytest.mark.parametrize("status, retryable", [(500, False), (503, False), (422, True)])
def test_retryable_refusals_are_not_persisted(setup, capsys, monkeypatch, status, retryable):
    target, hub, _ = setup
    monkeypatch.setattr(
        hub,
        "do_propose",
        lambda body: (status, {"code": "memory_scan_unavailable", "error": "private", "retryable": retryable}),
    )
    for count in (1, 2, 3):
        result, report = run(target, capsys, "run")
        assert result == 1 and report["error"] == "all_ops_refused"
        assert report["schedule"]["consecutive_failures"] == count
        state = json.loads((target / ".brigade/memory/reflect/schedule.json").read_text())
        assert all("refused" not in entry for entry in state["submissions"].values())
    assert report["schedule"]["paused"]
    assert len(writes(hub)) == 3
    assert len({c["client_event_id"] for c in writes(hub)}) == 1


@pytest.mark.parametrize("code", ["memory_client_event_expired", "memory_client_event_conflict"])
def test_event_id_recovery_is_bounded_to_one_retry(setup, capsys, monkeypatch, code):
    target, hub, _ = setup
    monkeypatch.setattr(hub, "do_propose", lambda body: (409, {"code": code}))
    result, report = run(target, capsys, "run")
    assert result == 1 and report["error"] == "all_ops_refused"
    assert len(writes(hub)) == 2
    assert writes(hub)[0]["client_event_id"] != writes(hub)[1]["client_event_id"]
    assert report["schedule"]["consecutive_failures"] == 1
    state = json.loads((target / ".brigade/memory/reflect/schedule.json").read_text())
    assert state["submissions"] == {}


@pytest.mark.parametrize("refused", [[], None, 42, "not a code"])
def test_invalid_persisted_refusal_is_reported(setup, capsys, refused):
    from brigade import memory_reflect_hub as reflect

    target, hub, _ = setup
    reflect._save(
        target,
        {
            "consecutive_failures": 0,
            "paused": False,
            "binding": "a" * 64,
            "submissions": {"b" * 64: {"client_event_id": hubc.event_id(), "refused": refused}},
        },
    )
    assert "state_error" in run(target, capsys, "status")[1]["schedule"]
    assert run(target, capsys, "run")[0] == 2
    assert hub.calls == []


def legacy_replica(target):
    from brigade import memory_hub_store as store, memory_hub_sync as sync

    settings = hubc.load_settings(target)
    sync.pull(target, hubc.client_for(settings), settings)
    with store.locked(target):
        replica = store.replica(target)
        for head in replica.heads.values():
            head.pop("scan_state", None)
        replica.save()


def test_legacy_replica_refreshes_clean_metadata_before_proposing(setup, capsys):
    target, hub, _ = setup
    legacy_replica(target)
    before = len(hub.calls)
    code, report = run(target, capsys, "run")
    assert code == 0 and len(report["proposals"]) == 1
    reads = [c for c in hub.calls[before:] if c["action"] == "card"]
    assert {c["card_id"] for c in reads} == {"a", "b"}
    assert all(c["offset"] == 0 for c in reads)
    assert [c["action"] for c in writes(hub)] == ["propose"]


def test_legacy_refresh_still_skips_non_clean_group(setup, capsys):
    target, hub, _ = setup
    legacy_replica(target)
    hub.scan_states = {("fleet", "b"): "flagged"}
    before = len(hub.calls)
    code, report = run(target, capsys, "run")
    assert code == 0 and report["skipped_reasons"] == {"non_clean": 1}
    assert any(c["action"] == "card" for c in hub.calls[before:])
    assert writes(hub) == []


def test_missing_metadata_after_refresh_stays_skipped(setup, capsys, monkeypatch):
    from brigade import memory_hub_sync as sync

    target, hub, _ = setup
    legacy_replica(target)
    refreshed = []

    def unchanged(*args):
        refreshed.append(True)
        return 0

    monkeypatch.setattr(sync, "refresh_scan_metadata", unchanged)
    code, report = run(target, capsys, "run")
    assert refreshed == [True]
    assert code == 0 and report["skipped_reasons"] == {"non_clean": 1}
    assert writes(hub) == []


@pytest.mark.parametrize("failure", ["auth", "unavailable"])
def test_refresh_failures_do_not_propose(setup, capsys, monkeypatch, failure):
    target, hub, _ = setup
    legacy_replica(target)

    def refuse(body):
        if failure == "auth":
            return 403, {"code": "memory_grant_missing", "error": "private"}
        raise ConnectionRefusedError("unavailable")

    monkeypatch.setattr(hub, "do_card", refuse)
    code, report = run(target, capsys, "run")
    assert code == 1 and report["schedule"]["consecutive_failures"] == 1
    assert "private" not in json.dumps(report)
    assert writes(hub) == []


@pytest.mark.parametrize("status", [408, 429])
def test_unavailable_client_responses_are_not_persisted(setup, capsys, monkeypatch, status):
    target, hub, _ = setup
    monkeypatch.setattr(hub, "do_propose", lambda body: (status, {"code": "memory_rate_limited"}))
    for count in (1, 2):
        result, report = run(target, capsys, "run")
        assert result == 1 and report["error"] == "hub_unavailable"
        assert report["schedule"]["consecutive_failures"] == count
        state = json.loads((target / ".brigade/memory/reflect/schedule.json").read_text())
        assert all("refused" not in entry for entry in state["submissions"].values())
    assert len(writes(hub)) == 2
    assert writes(hub)[0]["client_event_id"] == writes(hub)[1]["client_event_id"]


def test_legacy_replica_dry_run_refreshes_without_hub_writes(setup, capsys):
    target, hub, _ = setup
    legacy_replica(target)
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0 and report["candidate_count"] == 1
    assert writes(hub) == []
    assert run(target, capsys, "run")[0] == 0
    assert len(writes(hub)) == 1


def test_conflicting_terminal_submission_state_is_invalid(setup, capsys):
    from brigade import memory_reflect_hub as reflect

    target, hub, _ = setup
    reflect._save(
        target,
        {
            "consecutive_failures": 0,
            "paused": False,
            "binding": "a" * 64,
            "submissions": {
                "b" * 64: {
                    "client_event_id": hubc.event_id(),
                    "proposal_id": hubc.event_id(),
                    "refused": "memory_proposal_invalid",
                }
            },
        },
    )
    assert "state_error" in run(target, capsys, "status")[1]["schedule"]
    assert run(target, capsys, "run")[0] == 2
    assert hub.calls == []
