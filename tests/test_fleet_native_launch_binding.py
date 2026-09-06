"""Native brigade-run launch bindings: exact adapter argv, canonical Hub identity.

A seat can carry a canonical hyphen slug the Hub admits and a reviewed native id
the CLI actually accepts (`opencode run -m opencode/muse-spark-1.3-contributor-free`).
Only the launched argument changes; admission, the lease request, and the proof
identity stay on the canonical slug.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest

from brigade import (
    aboyeur,
    agents,
    cli,
    fleet_client,
    fleet_hub,
    fleet_hub_model_roster,
    fleet_hub_policy,
    fleet_model_admission,
    fleet_model_roster,
    fleet_policy,
    fleet_policy_migration,
    fleet_session_bootstrap,
)
from brigade.roster import Agent, Roster

NODE_A = "11111111-1111-4111-8111-111111111111"
SEAT = "muse13"
PROVIDER = "opencode"
CANONICAL = "opencode-muse-spark-1.3-contributor-free"
NATIVE = "opencode/muse-spark-1.3-contributor-free"
REPO = "repo/public"
DIGEST = "sha256:" + ("ab" * 32)


def _seat_row() -> dict[str, object]:
    return {
        "seat": SEAT,
        "provider": PROVIDER,
        "model": CANONICAL,
        "reasoning": "high",
        "enabled": True,
        "bindings": {
            "brigade": {"cli": "opencode"},
            "t3_fleet": {"instance_id": "opencode", "service_tier": None},
        },
    }


def _snapshot(*, native: str | None = NATIVE) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "state": "authoritative",
        "source": "hub",
        "revision": 2,
        "roster_revision": 2,
        "document_sha256": DIGEST,
        "expires_at": "2099-08-30T14:15:00Z",
        "seats": [_seat_row()],
        "consumer_defaults": {"brigade-run": SEAT},
        "retired_models": [],
        "fleet_policy": {"active": True, "version": 2, "digest": DIGEST},
    }
    if native is not None:
        snapshot["consumer_launch_bindings"] = {
            "brigade-run": {
                SEAT: {
                    "brigade": {"cli": "opencode", "model": native},
                    "t3_fleet": {},
                    "native": {},
                }
            }
        }
    return snapshot


def _roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            SEAT: Agent(SEAT, "opencode", "code", model=CANONICAL),
        },
    )


def test_projected_native_binding_launches_exact_adapter_argv():
    snapshot = _snapshot()
    resolution = aboyeur.resolve_fleet_model_policy(_roster(), worker=SEAT, snapshot=snapshot)
    assert resolution.error is None
    agent = resolution.roster.agents[SEAT]
    assert agent.model == NATIVE
    assert agents.build_argv("opencode", "do work", model=agent.model) == [
        "opencode",
        "run",
        "-m",
        NATIVE,
        "do work",
    ]
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == SEAT)
    assert decision["policy_model"] == CANONICAL
    assert decision["launch_model"] == NATIVE
    admission = decision["model_admission"]
    assert admission["model"] == CANONICAL
    assert admission["binding"]["model"] == NATIVE
    assert admission["binding"]["instance_id"] == "opencode"


def test_admission_decision_keeps_canonical_model_with_native_binding():
    decision = fleet_model_admission._resolve_from_roster(_snapshot(), consumer="brigade-run", seat=SEAT, source="hub")
    assert decision.ok is True
    assert decision.payload["model"] == CANONICAL
    assert decision.payload["binding"]["model"] == NATIVE


def test_seat_without_native_binding_keeps_canonical_argv():
    snapshot = _snapshot(native=None)
    resolution = aboyeur.resolve_fleet_model_policy(_roster(), worker=SEAT, snapshot=snapshot)
    assert resolution.error is None
    agent = resolution.roster.agents[SEAT]
    assert agent.model == CANONICAL
    assert agents.build_argv("opencode", "do work", model=agent.model) == [
        "opencode",
        "run",
        "-m",
        CANONICAL,
        "do work",
    ]
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == SEAT)
    assert decision["launch_model"] == CANONICAL
    assert "model" not in decision["model_admission"]["binding"]


def test_native_launch_is_never_inferred_from_the_canonical_slug():
    """No delimiter or alias rewriting: an unbound seat launches the slug verbatim."""
    row = _seat_row()
    assert fleet_model_roster.effective_brigade_launch_model(row) is None
    assert (
        fleet_model_roster.effective_brigade_launch_model(row, launch_groups={"brigade": {"cli": "opencode"}}) is None
    )
    groups = {"brigade": {"cli": "opencode", "model": NATIVE}}
    assert fleet_model_roster.effective_brigade_launch_model(row, launch_groups=groups) == NATIVE


def test_retired_floor_covers_a_projected_native_launch_model():
    snapshot = _snapshot(native="openai/gpt-5.5")
    snapshot["seats"] = [{**_seat_row(), "provider": "openai", "model": "model-ok-1"}]
    decision = fleet_model_admission._resolve_from_roster(snapshot, consumer="brigade-run", seat=SEAT, source="hub")
    assert decision.ok is False
    assert decision.reason == "retired-model"


def test_lease_request_keeps_canonical_model_and_records_native_launch(monkeypatch):
    captured: dict[str, object] = {}

    class _Lease:
        granted = True
        lease_id = "lease-1"
        holder = "holder-1"
        reason = ""

    def _acquire(seat, provider, model, **kwargs):
        captured["seat"] = seat
        captured["provider"] = provider
        captured["model"] = model
        captured["launch_model"] = kwargs.get("launch_model")
        return _Lease()

    monkeypatch.setattr(fleet_client, "acquire_model_lease", _acquire)
    monkeypatch.setattr(
        fleet_model_admission,
        "admit_model",
        lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(True, 0, "admitted", {}),
    )
    ctx = fleet_session_bootstrap.LaunchContext(
        session_id="session-native-1",
        consumer="brigade-run",
        provider=PROVIDER,
        model=NATIVE,
        instance_id="opencode",
        version=2,
        digest=DIGEST,
        instructions="",
        sources={},
        loaded_at="2026-09-06T00:00:00Z",
        repo_identity=REPO,
        selected={"seat": SEAT, "model": CANONICAL, "launch_model": NATIVE},
    )
    ctx.applied = True
    ctx.context_hash = "sha256:" + ("cd" * 32)
    fleet_session_bootstrap.authorize_launch(ctx, cli_ref="opencode", model=NATIVE)
    assert captured["model"] == CANONICAL
    assert captured["launch_model"] == NATIVE


def test_model_override_denial_names_the_native_binding_fix():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(), worker=SEAT, model_override=NATIVE, snapshot=_snapshot(native=None)
    )
    assert resolution.error is not None
    assert "does not match Hub model" in resolution.error
    assert f"--brigade-model {NATIVE}" in resolution.error
    assert f"brigade fleet models set {PROVIDER} {CANONICAL} {SEAT}" in resolution.error


def test_bound_native_override_is_admitted_without_remediation():
    resolution = aboyeur.resolve_fleet_model_policy(_roster(), worker=SEAT, model_override=NATIVE, snapshot=_snapshot())
    assert resolution.error is None
    assert resolution.roster.agents[SEAT].model == NATIVE


# --- Hub write path -------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _set_seat(conn, *, brigade_model: object = NATIVE, expected_revision: int = 1) -> dict[str, object]:
    body: dict[str, object] = {
        "action": "set",
        "expected_revision": expected_revision,
        "seat": SEAT,
        "provider": PROVIDER,
        "model": CANONICAL,
        "reasoning": "high",
        "enabled": True,
        "limit": 1,
        "brigade_cli": "opencode",
        "t3_instance_id": "opencode",
        "t3_service_tier": "",
        "notes": "",
    }
    if brigade_model is not None:
        body["brigade_model"] = brigade_model
    status, payload = fleet_hub_model_roster.handle_model_policy(conn, body)
    assert status == 200, payload
    return payload


def _set_default(conn, *, consumer: str, expected_revision: int) -> None:
    status, payload = fleet_hub_model_roster.handle_model_policy(
        conn,
        {
            "action": "set-default",
            "expected_revision": expected_revision,
            "consumer": consumer,
            "seat": SEAT,
        },
    )
    assert status == 200, payload


def _document() -> dict[str, object]:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_training": True}},
        "machines": {"worker-linux-1": {"os": "linux", "concurrency": 2, "node_id": NODE_A}},
        "seats": {},
        "consumers": {
            "brigade-run": {"reload": "refreshable", "coverage": "unverified", "notes": "kept-brigade"},
            "t3-fleet": {"reload": "none", "coverage": "unverified", "notes": "kept-t3"},
        },
        "repositories": {REPO: {"privacy": "public"}},
    }


def _activate(conn) -> None:
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="seed")
    preview = fleet_policy_migration.preview_migration(
        conn,
        expected_policy_version=fleet_hub_policy.current_policy(conn)["revision"],
        expected_roster_revision=int(
            conn.execute("SELECT revision FROM model_roster_meta WHERE singleton=1").fetchone()[0]
        ),
        annotations={"seats": {SEAT: {"pinned": False, "training_allowed": True}}},
    )
    assert preview["ok"] is True, preview
    fleet_policy_migration.activate_migration(
        conn,
        expected_policy_version=preview["sources"]["policy_revision"],
        expected_roster_revision=preview["sources"]["roster_revision"],
        preview_digest=preview["preview_digest"],
        actor="operator",
        reason="adopt",
        annotations=preview.get("annotations"),
    )


def test_models_set_brigade_model_round_trips_to_the_signed_roster(conn):
    policy = _set_seat(conn)
    assert policy["policy"]["brigade_model"] == NATIVE
    assert policy["policy"]["model"] == CANONICAL
    seats = fleet_hub_model_roster.raw_seats(conn)
    assert seats[0]["model"] == CANONICAL
    assert seats[0]["bindings"]["brigade"] == {"cli": "opencode", "model": NATIVE}
    assert (
        fleet_model_roster.validate_roster_rows(
            {
                "seats": seats,
                "consumer_defaults": {"brigade-run": SEAT},
                "retired_models": [],
            }
        )
        is None
    )


def test_clearing_brigade_model_restores_the_canonical_launch(conn):
    _set_seat(conn)
    _set_seat(conn, brigade_model="", expected_revision=2)
    seats = fleet_hub_model_roster.raw_seats(conn)
    assert seats[0]["bindings"]["brigade"] == {"cli": "opencode"}
    assert fleet_model_roster.brigade_launch_model(seats[0]) is None


def test_operator_roster_page_toggle_keeps_the_native_binding(conn):
    """Enabling or disabling a seat on the deck must not silently drop the binding."""
    from brigade import fleet_command_deck, fleet_hub_roster_page

    _set_seat(conn)
    view = fleet_hub_roster_page.load_view(conn, fleet_command_deck.DeckConfig())
    row = next(item for item in view.seats if item.seat == SEAT)
    assert row.brigade_model == NATIVE
    fleet_hub_roster_page.fleet_hub_model_roster._write_set(
        conn,
        {
            "seat": row.seat,
            "provider": row.provider,
            "model": row.model,
            "reasoning": row.reasoning,
            "enabled": False,
            "limit": row.limit,
            "brigade_cli": row.brigade_cli,
            "brigade_model": row.brigade_model,
            "t3_instance_id": row.t3_instance_id,
            "t3_service_tier": row.t3_service_tier,
            "notes": row.notes,
        },
    )
    seats = fleet_hub_model_roster.raw_seats(conn)
    assert seats[0]["enabled"] is False
    assert seats[0]["bindings"]["brigade"]["model"] == NATIVE


def test_brigade_model_projects_through_bindings_cli(conn, monkeypatch):
    _set_seat(conn)
    _set_default(conn, consumer="brigade-run", expected_revision=2)
    _set_default(conn, consumer="t3-fleet", expected_revision=3)
    _activate(conn)
    projected = fleet_hub_model_roster.project_consumer_launch_bindings(conn)
    assert projected["brigade-run"][SEAT]["brigade"]["model"] == NATIVE
    roster = fleet_hub_model_roster.project_roster(conn, audience_node_id=NODE_A)
    assert roster["consumer_launch_bindings"]["brigade-run"][SEAT]["brigade"]["model"] == NATIVE
    seat_row = next(item for item in roster["seats"] if item["seat"] == SEAT)
    assert seat_row["model"] == CANONICAL

    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(True, 0, "hub", {**roster, "source": "hub"}),
    )
    out = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", StringIO())
    rc = cli.main(["fleet", "models", "bindings", "--consumer", "brigade-run", "--json"])
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["bindings"][SEAT]["model"] == CANONICAL
    assert payload["bindings"][SEAT]["brigade"] == {"cli": "opencode", "model": NATIVE}


def test_cleared_brigade_model_disappears_from_the_projection(conn):
    _set_seat(conn)
    _set_seat(conn, brigade_model="", expected_revision=2)
    _set_default(conn, consumer="brigade-run", expected_revision=3)
    _set_default(conn, consumer="t3-fleet", expected_revision=4)
    _activate(conn)
    projected = fleet_hub_model_roster.project_consumer_launch_bindings(conn)
    assert "model" not in projected["brigade-run"][SEAT]["brigade"]


@pytest.mark.parametrize(
    "native",
    (
        "opencode/muse\x00spark",
        "opencode/muse\nspark",
        "a" * 257,
        "/leading-separator",
        "opencode/muse spark",
    ),
)
def test_hub_rejects_unsafe_native_launch_ids(conn, native):
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub_model_roster._validate_set(
            {
                "action": "set",
                "expected_revision": 1,
                "seat": SEAT,
                "provider": PROVIDER,
                "model": CANONICAL,
                "reasoning": "high",
                "enabled": True,
                "brigade_model": native,
            }
        )


def test_signed_roster_rejects_unsafe_projected_native_ids():
    for native in ("opencode/muse\x00spark", "a" * 257, "opencode/muse spark"):
        bad = {
            "brigade-run": {
                SEAT: {"brigade": {"cli": "opencode", "model": native}, "t3_fleet": {}, "native": {}},
            }
        }
        assert fleet_model_roster.validate_consumer_launch_bindings(bad) == "malformed-roster"


def test_models_set_cli_sends_and_clears_the_native_binding(monkeypatch):
    calls: list[dict[str, object]] = []

    def _set_model_policy(provider, model, seat, **kwargs):
        calls.append({"provider": provider, "model": model, "seat": seat, **kwargs})
        return {"provider": provider, "model": model, "seat": seat, "enabled": True, **kwargs}

    monkeypatch.setattr(fleet_client, "set_model_policy", _set_model_policy)
    monkeypatch.setattr("sys.stdout", StringIO())
    monkeypatch.setattr("sys.stderr", StringIO())
    assert (
        cli.main(
            [
                "fleet",
                "models",
                "set",
                PROVIDER,
                CANONICAL,
                SEAT,
                "--enable",
                "--reasoning",
                "high",
                "--brigade-cli",
                "opencode",
                "--brigade-model",
                NATIVE,
                "--expect-revision",
                "2",
            ]
        )
        == 0
    )
    assert calls[-1]["brigade_model"] == NATIVE
    assert (
        cli.main(
            [
                "fleet",
                "models",
                "set",
                PROVIDER,
                CANONICAL,
                SEAT,
                "--enable",
                "--clear-brigade-model",
                "--expect-revision",
                "3",
            ]
        )
        == 0
    )
    assert calls[-1]["brigade_model"] == ""


def test_models_set_cli_rejects_setting_and_clearing_together(monkeypatch):
    monkeypatch.setattr("sys.stdout", StringIO())
    monkeypatch.setattr("sys.stderr", StringIO())
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "fleet",
                "models",
                "set",
                PROVIDER,
                CANONICAL,
                SEAT,
                "--enable",
                "--brigade-model",
                NATIVE,
                "--clear-brigade-model",
                "--expect-revision",
                "2",
            ]
        )
    assert excinfo.value.code != 0


def test_client_preserves_and_clears_the_native_binding(monkeypatch):
    posted: list[dict[str, object]] = []
    existing = {
        "seats": [
            {
                "seat": SEAT,
                "provider": PROVIDER,
                "model": CANONICAL,
                "reasoning": "high",
                "enabled": True,
                "bindings": {
                    "brigade": {"cli": "opencode", "model": NATIVE},
                    "t3_fleet": {"instance_id": "opencode", "service_tier": None},
                },
            }
        ],
        "revision": 4,
    }
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "admin_token": "admin-token"},
    )
    monkeypatch.setattr(fleet_client, "_get_models_blocking", lambda *args, **kwargs: existing)

    def _post(hub, token, body, **kwargs):
        posted.append(body)
        return 200, {"policy": {"seat": SEAT, "provider": PROVIDER, "model": CANONICAL, "enabled": True}}

    monkeypatch.setattr(fleet_client, "_post_model_policy_blocking", _post)
    fleet_client.set_model_policy(PROVIDER, CANONICAL, SEAT, enabled=True, expected_revision=4)
    assert posted[-1]["brigade_model"] == NATIVE
    fleet_client.set_model_policy(PROVIDER, CANONICAL, SEAT, enabled=True, brigade_model="", expected_revision=4)
    assert posted[-1]["brigade_model"] == ""
    assert posted[-1]["model"] == CANONICAL
