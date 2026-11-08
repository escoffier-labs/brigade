"""Real-sized scopes and operator proposal command boundaries."""

import json
import shlex

import pytest

from brigade import cli, memory_hub_client as hubc, memory_reflect_hub as reflect
from tests.test_memory_reflect_hub import add_group, pytestmark as pytestmark, run, setup as setup, writes


def test_scope_narrows_reads_and_candidates_before_limits(setup, capsys):
    target, hub, _ = setup
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["fleet","repo:example"]\n')
    add_group(hub, scope="repo:example")
    for i in range(1100):
        hub._commit("card.put", "fleet", f"card-{i}", f"Unique {i}", "live", hubc.event_id())
    code, report = run(target, capsys, "run", "--scope", "repo:example")
    assert code == 0 and len(report["proposals"]) == 1
    assert {call["scope"] for call in hub.calls if call["action"] == "card"} == {"repo:example"}
    assert {call["ops"][0]["scope"] for call in writes(hub)} == {"repo:example"}


@pytest.mark.parametrize("card_count", [1882, 20_002])
def test_real_sized_scope_can_propose(setup, capsys, card_count):
    target, hub, _ = setup
    for i in range(card_count - 2):
        hub._commit("card.put", "fleet", f"card-{i}", f"Unique {i}", "live", hubc.event_id())
    code, report = run(target, capsys, "run", "--scope", "fleet")
    assert code == 0 and len(report["proposals"]) == 1


@pytest.mark.parametrize("bound,value", [("MAX_SCOPE_CARDS", 1), ("MAX_SCOPE_BYTES", 1)])
def test_scope_bound_fails_closed_with_code_and_reason(setup, capsys, monkeypatch, bound, value):
    target, hub, _ = setup
    monkeypatch.setattr(reflect, bound, value, raising=False)
    assert cli.main(["memory", "reflect", "run", "--target", str(target)]) == 1
    output = capsys.readouterr().out
    assert "hub_reflect_scope_limit" in output
    assert "per-scope" in output
    assert "Same claim" not in output
    assert writes(hub) == []


def test_reflect_care_scope_is_pinned_in_service(setup, capsys):
    target, _, home = setup
    assert (
        cli.main(
            [
                "memory",
                "care",
                "install",
                "--entry",
                "memory-reflect",
                "--scope",
                "fleet",
                "--target",
                str(target),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    [unit] = (home / ".config/systemd/user").glob("*memory-reflect.service")
    argv = shlex.split(next(line[10:] for line in unit.read_text().splitlines() if line.startswith("ExecStart=")))
    assert argv[-2:] == ["--scope", "fleet"]
    assert "BRIGADE_MEMORY_HUB" not in unit.read_text()


@pytest.mark.parametrize("scope", ["bad", "repo:missing"])
def test_care_scope_must_be_configured_before_writes(setup, capsys, scope):
    target, _, home = setup
    assert (
        cli.main(
            [
                "care",
                "install",
                "--entry",
                "memory-reflect",
                "--scope",
                scope,
                "--target",
                str(target),
            ]
        )
        == 2
    )
    capsys.readouterr()
    assert not list((home / ".config/systemd/user").glob("*memory-reflect.service"))


class ProposalReads:
    def __init__(self, hub):
        self.hub = hub
        self.calls = []
        self.ident = hubc.event_id()

    def __call__(self, url, token, body, timeout):
        self.calls.append(body)
        action = body["action"]
        if action == "proposal_list":
            return 200, {"proposals": [{"proposal_id": self.ident, "state": "pending"}], "next_after": None}
        if action == "proposal_show":
            return 200, {"proposal_id": self.ident, "state": "pending", "diff": None}
        if action == "decide":
            assert body["expected_policy_digest"] == "a" * 64
            return 200, {"proposal_id": self.ident, "state": "applied" if body["decision"] == "accept" else "rejected"}
        return self.hub(url, token, body, timeout)


def proposals(target, capsys, *args):
    code = cli.main(["memory", "proposals", *args, "--target", str(target), "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_operator_list_defaults_pending_and_all_is_explicit(setup, capsys, monkeypatch):
    target, hub, _ = setup
    transport = ProposalReads(hub)
    monkeypatch.setattr(hubc, "urllib_transport", transport)
    assert proposals(target, capsys, "list")[0] == 0
    assert [c for c in transport.calls if c["action"] == "proposal_list"][-1]["all"] is False
    assert proposals(target, capsys, "list", "--all")[0] == 0
    assert [c for c in transport.calls if c["action"] == "proposal_list"][-1]["all"] is True
    assert not any(c["action"] in {"log", "card", "decide"} for c in transport.calls)


def test_show_preserves_hub_redaction(setup, capsys, monkeypatch):
    target, hub, _ = setup
    transport = ProposalReads(hub)
    monkeypatch.setattr(hubc, "urllib_transport", transport)
    code, report = proposals(target, capsys, "show", transport.ident)
    assert code == 0 and report["diff"] is None
    assert not any(c["action"] in {"log", "card", "decide"} for c in transport.calls)


@pytest.mark.parametrize("decision,state", [("accept", "applied"), ("reject", "rejected")])
def test_decide_sends_explicit_policy_digest(setup, capsys, monkeypatch, decision, state):
    target, hub, _ = setup
    transport = ProposalReads(hub)
    monkeypatch.setattr(hubc, "urllib_transport", transport)
    code, report = proposals(
        target, capsys, "decide", transport.ident, f"--{decision}", "--expected-policy-digest", "a" * 64
    )
    assert code == 0 and report["state"] == state
    [request] = [c for c in transport.calls if c["action"] == "decide"]
    assert request["decision"] == decision
    assert request["base_epoch"] == hub.epoch
    assert hubc.event_id_ms(request["client_event_id"]) is not None


def test_care_reinstall_retains_scope_without_repeating_option(setup, capsys):
    target, _, home = setup
    base = ["care", "install", "--entry", "memory-reflect", "--target", str(target), "--json"]
    assert cli.main([*base, "--scope", "fleet"]) == 0
    capsys.readouterr()
    [unit] = (home / ".config/systemd/user").glob("*memory-reflect.service")
    before = unit.read_text()
    assert cli.main(base) == 0
    capsys.readouterr()
    assert unit.read_text() == before


def test_decide_policy_mismatch_never_retries_or_prints_hub_text(setup, capsys, monkeypatch):
    target, hub, _ = setup
    transport = ProposalReads(hub)
    calls = []

    def refusal(url, token, body, timeout):
        if body["action"] == "decide":
            calls.append(body)
            return 409, {
                "code": "memory_policy_digest_mismatch",
                "error": "private card text",
                "policy_digest": "b" * 64,
            }
        return transport(url, token, body, timeout)

    monkeypatch.setattr(hubc, "urllib_transport", refusal)
    code, report = proposals(
        target, capsys, "decide", transport.ident, "--accept", "--expected-policy-digest", "a" * 64
    )
    assert code == 2 and report["error"] == "memory_policy_digest_mismatch"
    assert report["policy_digest"] == "b" * 64
    assert len(calls) == 1 and calls[0]["expected_policy_digest"] == "a" * 64
    assert "private card text" not in json.dumps(report)


def test_operator_credential_file_pair_is_explicit_and_never_falls_back(setup, capsys, monkeypatch):
    from tests.test_memory_hub_client import HUB_URL, NODE_CREDENTIAL
    from tests.test_memory_reflect_hub import ORIGINAL_OVERRIDE

    target, hub, home = setup
    home.mkdir(parents=True, exist_ok=True)
    token_file = home / "operator-token"
    token_file.write_text(NODE_CREDENTIAL)
    token_file.chmod(0o600)
    transport = ProposalReads(hub)
    monkeypatch.setattr(hubc, "urllib_transport", transport)
    # Exercise the real file reader rather than the fixture's environment override.
    monkeypatch.setattr(hubc, "_memory_hub_override", ORIGINAL_OVERRIDE)
    code, report = proposals(target, capsys, "list", "--hub-url", HUB_URL, "--token-file", str(token_file))
    assert code == 0 and report["proposals"]


def test_decide_can_pin_absent_policy_after_removal(setup, capsys, monkeypatch):
    target, hub, _ = setup
    transport = ProposalReads(hub)
    calls = []

    def decide(url, token, body, timeout):
        if body["action"] == "decide":
            calls.append(body)
            return 200, {"proposal_id": transport.ident, "state": "rejected"}
        return transport(url, token, body, timeout)

    monkeypatch.setattr(hubc, "urllib_transport", decide)
    assert proposals(target, capsys, "decide", transport.ident, "--reject", "--expected-policy-digest", "none")[0] == 0
    assert len(calls) == 1 and calls[0]["expected_policy_digest"] is None


def test_launchd_scope_is_retained_on_reinstall(setup, capsys):
    import plistlib

    target, _, home = setup
    base = ["care", "install", "--backend", "launchd", "--entry", "memory-reflect", "--target", str(target), "--json"]
    assert cli.main([*base, "--scope", "fleet"]) == 0
    capsys.readouterr()
    [registration] = (home / "Library/LaunchAgents").glob("*memory-reflect.plist")
    before = registration.read_bytes()
    assert plistlib.loads(before)["ProgramArguments"][-2:] == ["--scope", "fleet"]
    assert cli.main(base) == 0
    capsys.readouterr()
    assert registration.read_bytes() == before


def test_accept_requires_policy_pin_before_any_hub_call(setup, capsys):
    target, hub, _ = setup
    ident = hubc.event_id()
    code, report = proposals(target, capsys, "decide", ident, "--accept")
    assert code == 2
    assert "--expected-policy-digest" in report["error"]
    assert f"memory proposals show {ident}" in report["error"]
    assert hub.calls == []


def test_reject_without_policy_pin_is_allowed(setup, capsys, monkeypatch):
    target, hub, _ = setup
    transport = ProposalReads(hub)
    calls = []

    def reject(url, token, body, timeout):
        if body["action"] == "decide":
            calls.append(body)
            return 200, {"proposal_id": transport.ident, "state": "rejected"}
        return transport(url, token, body, timeout)

    monkeypatch.setattr(hubc, "urllib_transport", reject)
    assert proposals(target, capsys, "decide", transport.ident, "--reject")[0] == 0
    assert len(calls) == 1 and "expected_policy_digest" not in calls[0]


@pytest.mark.parametrize("directory", ["replica", "replica.building"])
def test_oversized_disk_heads_are_refused_before_parsing(setup, capsys, monkeypatch, directory):
    from brigade import memory_hub_store as store

    target, hub, _ = setup
    path = store.root(target) / directory / "heads.json"
    path.parent.mkdir(parents=True)
    with path.open("wb") as file:
        file.truncate(66 * 1024 * 1024)
    original = store.read_json

    def forbid_heads(file):
        assert file.name != "heads.json", "oversized heads must never be parsed"
        return original(file)

    monkeypatch.setattr(store, "read_json", forbid_heads)
    code, report = run(target, capsys, "run")
    assert code == 1 and report["error"] == "hub_reflect_replica_limit"
    assert writes(hub) == []
    assert hub.calls == []


def test_scoped_non_dry_run_refuses_shared_replica_without_eviction(setup, capsys):
    from brigade import memory_hub_store as store, memory_hub_sync as sync

    target, hub, _ = setup
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["fleet","repo:example"]\n')
    add_group(hub, scope="repo:example")
    settings = hubc.load_settings(target)
    sync.pull(target, hubc.client_for(settings), settings)
    path = store.root(target) / "replica" / "heads.json"
    before = path.read_bytes()
    hub.calls.clear()
    code, report = run(target, capsys, "run", "--scope", "fleet")
    assert code == 1 and report["error"] == "hub_reflect_dedicated_replica_required"
    assert "docs/memory-operations.md#dedicated-reflect-folder" in report["reason"]
    assert path.read_bytes() == before
    assert hub.calls == []


def test_care_scope_returns_to_unscoped_only_after_uninstall(setup, capsys):
    target, _, home = setup
    base = ["care", "install", "--entry", "memory-reflect", "--target", str(target), "--json"]
    assert cli.main([*base, "--scope", "fleet"]) == 0
    capsys.readouterr()
    assert cli.main(["care", "uninstall", "--entry", "memory-reflect", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(base) == 0
    capsys.readouterr()
    [unit] = (home / ".config/systemd/user").glob("*memory-reflect.service")
    assert "--scope" not in unit.read_text()


def test_scoped_dry_run_can_refresh_shared_replica_without_hub_writes(setup, capsys):
    from brigade import memory_hub_sync as sync

    target, hub, _ = setup
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["fleet","repo:example"]\n')
    add_group(hub, scope="repo:example")
    settings = hubc.load_settings(target)
    sync.pull(target, hubc.client_for(settings), settings)
    code, report = run(target, capsys, "run", "--scope", "fleet", "--dry-run")
    assert code == 0 and report["candidate_count"] == 1
    assert writes(hub) == []
