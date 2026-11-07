"""Scheduled hub reflection only opens reviewed proposals."""

from __future__ import annotations

import json
import shlex

import pytest

from brigade import care_cmd, cli, memory_hub_client as hubc
from tests._home import set_home
from tests.test_memory_hub_client import FakeHub, HUB_URL, NODE_CREDENTIAL

ORIGINAL_OVERRIDE = hubc._memory_hub_override


class ReflectHub(FakeHub):
    def __init__(self):
        super().__init__()
        self.reject = False

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
    hub.reject = True
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
    hub.reject = False
    assert run(target, capsys, "run", "--dry-run")[0] == 0
    assert run(target, capsys, "status")[1]["schedule"]["consecutive_failures"] == 0
    assert run(target, capsys, "run")[0] == 0
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


def test_no_valid_pins_fails_without_proposing(setup, capsys):
    target, hub, _ = setup
    for ident in ("a", "b"):
        hub._commit("card.put", "fleet", ident, "Same claim.\n", "live", hubc.event_id())
    assert run(target, capsys, "run")[0] == 1
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


def test_dry_run_exit_counts_without_hub_writes(setup, capsys):
    target, hub, _ = setup
    hub.mode = "down"
    for count in (1, 2, 3):
        assert run(target, capsys, "run", "--dry-run")[0] == 1
        assert run(target, capsys, "status")[1]["schedule"]["consecutive_failures"] == count
    assert run(target, capsys, "status")[1]["schedule"]["paused"]
    hub.mode = "up"
    assert run(target, capsys, "run", "--dry-run")[0] == 0
    assert not run(target, capsys, "status")[1]["schedule"]["paused"]
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
