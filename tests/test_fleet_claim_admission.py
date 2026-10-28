"""Explicit required admission never lends authority from a local fallback."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brigade import fleet_client
from tests.test_fleet_claims import lease_clock as lease_clock
from tests.test_fleet_nodes import ADMIN, NODE_A, _enroll, _plant_home_identity, _request, _serve


@pytest.fixture()
def enrolled_hub(tmp_path, monkeypatch):
    server, _db = _serve(tmp_path, allow_admin_writes=False)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    token = _enroll(url, NODE_A)
    home = tmp_path / "home"
    _plant_home_identity(home, NODE_A)
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", token)
    try:
        yield server, url
    finally:
        server.shutdown()
        server.server_close()


def test_required_claim_refuses_real_hub_outage(enrolled_hub, monkeypatch):
    server, _url = enrolled_hub
    server.shutdown()
    server.server_close()
    # Keep uncertain cleanup observable without leaving retries after the test.
    cleanup = []
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", lambda *a, **kw: cleanup.append(kw))
    entered = False
    with pytest.raises(fleet_client.FleetClaimHeldError, match="hub-unavailable") as refusal:
        with fleet_client.repo_claim("fake-repo", require_claim=True):
            entered = True
    assert isinstance(refusal.value, fleet_client.FleetClaimRequiredError)
    assert entered is False
    assert len(cleanup) == 1
    with fleet_client.repo_claim("fake-repo") as decision:
        assert decision.granted is False
        assert decision.reason == "hub-unavailable"
    assert len(cleanup) == 2


@pytest.fixture()
def machine(monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda **kw: {"hub_url": "https://hub.example.invalid", "token": "fake-original"},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path: NODE_A)


@pytest.mark.parametrize("reason", ["no-hub", "configuration-error", "no-identity", "missing"])
def test_required_claim_refuses_unconfirmed_admission(machine, monkeypatch, reason):
    attempts = []

    def acquire(*args, **kwargs):
        attempts.append(kwargs["holder"])
        return fleet_client.ClaimDecision(False, "missing")

    monkeypatch.setattr(fleet_client, "acquire_claim", acquire)
    if reason == "no-hub":
        monkeypatch.setattr(fleet_client, "load_fleet_config", lambda **kw: {"hub_url": "", "token": ""})
    elif reason == "configuration-error":

        def unreadable(**kwargs):
            raise OSError("fake-private-config-detail")

        monkeypatch.setattr(fleet_client, "load_fleet_config", unreadable)
    elif reason == "no-identity":
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path: "unknown")
    entered = False
    with pytest.raises(fleet_client.FleetClaimHeldError, match=reason) as refusal:
        with fleet_client.repo_claim("fake-repo", require_claim=True):
            entered = True
    assert isinstance(refusal.value, fleet_client.FleetClaimRequiredError)
    assert entered is False
    assert "fake-private-config-detail" not in str(refusal.value)
    assert refusal.value.__cause__ is None
    assert len(attempts) == (2 if reason == "missing" else 0)
    if attempts:
        assert attempts[0] == attempts[1]


@pytest.mark.parametrize("failure", ["malformed", "unreadable", "inaccessible-parent", "invalid-encoding"])
def test_required_claim_distinguishes_real_configuration_read_failures(tmp_path, monkeypatch, failure):
    if failure in {"unreadable", "inaccessible-parent"} and (os.name != "posix" or os.geteuid() == 0):
        pytest.skip("unreadable file requires POSIX permissions and a non-root user")
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    for name in ("BRIGADE_FLEET_HUB_URL", "BRIGADE_FLEET_TOKEN", "BRIGADE_FLEET_NODE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    config = home / "fleet.toml"
    if failure == "invalid-encoding":
        config.write_bytes(b"\xff")
    else:
        config.write_text(
            "[fleet]\ninvalid = [fake-private-config-detail" if failure == "malformed" else "[fleet]\n",
            encoding="utf-8",
        )
    if failure == "unreadable":
        config.chmod(0)
    elif failure == "inaccessible-parent":
        home.chmod(0)
    try:
        with pytest.raises(fleet_client.FleetClientError) as read_error:
            fleet_client.load_fleet_config(strict=True)
        assert str(read_error.value) == "fleet configuration could not be read"
        assert read_error.value.__cause__ is None
        with pytest.raises(fleet_client.FleetClaimRequiredError, match="configuration-error") as refusal:
            with fleet_client.repo_claim("fake-repo", require_claim=True):
                pytest.fail("invalid configuration admitted guarded work")
        assert str(refusal.value) == "fleet claim on 'fake-repo' required: configuration-error"
        assert refusal.value.__cause__ is None
        assert fleet_client.load_fleet_config() == {"hub_url": "", "token": ""}
        with fleet_client.repo_claim("fake-repo") as decision:
            assert not decision.granted
            assert decision.reason == "no-hub"
    finally:
        home.chmod(0o700)
        config.chmod(0o600)


def test_strict_loader_treats_config_disappearance_as_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    for name in ("BRIGADE_FLEET_HUB_URL", "BRIGADE_FLEET_TOKEN", "BRIGADE_FLEET_NODE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "fleet.toml"
    config.write_text("[fleet]\n", encoding="utf-8")
    read_text = Path.read_text

    def disappear(path, *args, **kwargs):
        if path == config:
            path.unlink()
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", disappear)
    assert fleet_client.load_fleet_config(strict=True) == {"hub_url": "", "token": ""}
    assert not config.exists()


def test_required_claim_accepts_real_grant_and_refuses_revoked_token(enrolled_hub):
    _server, url = enrolled_hub
    with fleet_client.repo_claim("fake-repo", require_claim=True) as decision:
        assert decision.granted is True
        assert decision.claim["owner_node"] == NODE_A
        assert len(fleet_client.fetch_claims()) == 1
    assert fleet_client.fetch_claims() == []
    assert _request(url, "/nodes", ADMIN, {"action": "revoke", "node_id": NODE_A})[0] == 200
    with pytest.raises(fleet_client.FleetClaimAuthError):
        with fleet_client.repo_claim("fake-repo", require_claim=True):
            pytest.fail("revoked credentials admitted guarded work")


@pytest.mark.parametrize("final_reason", ["hub-unavailable", "missing"])
def test_required_ambiguous_acquire_keeps_holder_cleanup(machine, monkeypatch, final_reason):
    attempts = []
    cleanup = []

    def acquire(*args, **kwargs):
        attempts.append(kwargs)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://later-hub.example.invalid")
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "fake-later")
        return fleet_client.ClaimDecision(False, "hub-unavailable" if len(attempts) == 1 else final_reason)

    monkeypatch.setattr(fleet_client, "acquire_claim", acquire)
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", lambda *a, **kw: cleanup.append(kw))
    with pytest.raises(fleet_client.FleetClaimHeldError, match=final_reason):
        with fleet_client.repo_claim("fake-repo", require_claim=True):
            pytest.fail("uncertain grant admitted guarded work")
    assert len(attempts) == 2
    assert len(cleanup) == 1
    assert attempts[0]["holder"] == attempts[1]["holder"] == cleanup[0]["holder"]
    assert cleanup[0]["node_id"] == NODE_A
    assert cleanup[0]["connect"]() == {"hub_url": "https://hub.example.invalid", "token": "fake-original"}
    assert all(attempt["hub_url"] == "https://hub.example.invalid" for attempt in attempts)
    assert all(attempt["token"] == "fake-original" for attempt in attempts)


@pytest.mark.parametrize("reason", ["auth-failed", "held"])
@pytest.mark.parametrize("ambiguous_first", [False, True])
def test_required_terminal_refusal_keeps_cleanup_policy(machine, monkeypatch, reason, ambiguous_first):
    attempts = []
    cleanup = []

    def acquire(*args, **kwargs):
        attempts.append(kwargs["holder"])
        outcome = "hub-unavailable" if ambiguous_first and len(attempts) == 1 else reason
        return fleet_client.ClaimDecision(False, outcome, owner={"owner_node": "fake-other"})

    monkeypatch.setattr(fleet_client, "acquire_claim", acquire)
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", lambda *a, **kw: cleanup.append(kw))
    error = fleet_client.FleetClaimAuthError if reason == "auth-failed" else fleet_client.FleetClaimHeldError
    with pytest.raises(error) as refusal:
        with fleet_client.repo_claim("fake-repo", require_claim=True):
            pytest.fail("terminal refusal admitted guarded work")
    assert not isinstance(refusal.value, fleet_client.FleetClaimRequiredError)
    assert len(attempts) == (2 if ambiguous_first else 1)
    assert len(set(attempts)) == 1
    assert cleanup == []


@pytest.mark.parametrize("explicit_policy", [None, "continue"])
@pytest.mark.parametrize("reason", ["held", "unexpected"])
def test_required_claim_enforces_abort_loss_policy(machine, monkeypatch, explicit_policy, reason, caplog):
    import threading

    monkeypatch.setenv(fleet_client.CLAIM_LOSS_ENV, "continue")
    monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0.01)
    monkeypatch.setattr(fleet_client, "acquire_claim", lambda *a, **kw: fleet_client.ClaimDecision(True, "ok"))
    monkeypatch.setattr(fleet_client, "renew_claim", lambda *a, **kw: fleet_client.ClaimDecision(False, reason))
    monkeypatch.setattr(fleet_client, "release_claim", lambda *a, **kw: fleet_client.ClaimDecision(True, "ok"))
    interrupted = threading.Event()
    monkeypatch.setattr(fleet_client._thread, "interrupt_main", interrupted.set)
    lost = []
    with fleet_client.repo_claim(
        "fake-repo", require_claim=True, claim_loss_policy=explicit_policy, on_claim_lost=lost.append
    ) as decision:
        assert decision.granted is True
        assert decision.cancel_event.wait(2), "required admission honored the solo continuation opt-out"
        assert interrupted.wait(2)
        assert lost == [reason]
    assert "opt out" not in caplog.text


def test_required_claim_refuses_expired_grant(machine, monkeypatch, lease_clock):
    released = []

    def acquire(*args, **kwargs):
        lease_clock(101)
        return fleet_client.ClaimDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "acquire_claim", acquire)
    monkeypatch.setattr(
        fleet_client, "release_claim", lambda *a, **kw: released.append(kw) or fleet_client.ClaimDecision(True, "ok")
    )
    with pytest.raises(fleet_client.FleetClaimExpiredError, match="lease-expired"):
        with fleet_client.repo_claim("fake-repo", require_claim=True, claim_loss_policy="continue", ttl_seconds=1):
            pytest.fail("expired grant admitted guarded work")
    assert len(released) == 1


@pytest.mark.parametrize("callback_mode", ["none", "record", "block"])
def test_required_claim_revocation_aborts_with_bounded_credential_callback(machine, monkeypatch, callback_mode):
    import threading

    monkeypatch.setenv(fleet_client.CLAIM_LOSS_ENV, "continue")
    monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0.01)
    monkeypatch.setattr(fleet_client, "CLAIM_LOST_CALLBACK_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(fleet_client, "acquire_claim", lambda *a, **kw: fleet_client.ClaimDecision(True, "ok"))
    monkeypatch.setattr(
        fleet_client,
        "renew_claim",
        lambda *a, **kw: fleet_client.ClaimDecision(False, "auth-failed", detail="fake-revoked"),
    )
    monkeypatch.setattr(fleet_client, "release_claim", lambda *a, **kw: fleet_client.ClaimDecision(True, "ok"))
    interrupted = threading.Event()
    unblock = threading.Event()
    monkeypatch.setattr(fleet_client._thread, "interrupt_main", interrupted.set)
    credential_details = []
    lost = []

    def credential_callback(detail):
        credential_details.append(detail)
        if callback_mode == "block":
            assert unblock.wait(5)

    with fleet_client.repo_claim(
        "fake-repo",
        require_claim=True,
        on_credential_failure=None if callback_mode == "none" else credential_callback,
        on_claim_lost=lost.append,
    ) as decision:
        try:
            assert decision.cancel_event.wait(2), "revocation stopped renewal without canceling required work"
            assert interrupted.wait(2), "credential callback suppressed the required abort"
            assert credential_details == ([] if callback_mode == "none" else ["fake-revoked"])
            assert lost == [], "credential rejection was also published as generic claim loss"
        finally:
            unblock.set()
