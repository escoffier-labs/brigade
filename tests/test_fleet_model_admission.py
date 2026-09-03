from __future__ import annotations

import dataclasses
import http.client
import json
import os
import stat
import threading
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import aboyeur
from brigade import agents
from brigade import cli
from brigade import fleet_client
from brigade import fleet_hub
from brigade import fleet_model_admission
from brigade import fleet_model_roster
from brigade.roster import Agent, Roster
from tests.run_test_helpers import run_aboyeur_guarded

NODE_A = "11111111-1111-4111-8111-111111111111"
ADMIN_TOKEN = "test-admin-token"
ADMIT_REQUEST_ID = "c833a6f6-02fd-4eb2-92cb-d44d3cd29b66"
SEAT_BODY = {
    "action": "set",
    "provider": "cursor",
    "model": "cursor-grok-4.6-high-fast",
    "seat": "cursor_grok",
    "enabled": True,
    "reasoning": "high",
    "brigade_cli": "cursor-agent",
    "t3_instance_id": "cursor",
    "t3_service_tier": "standard",
}


def _roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "cursor_grok": Agent("cursor_grok", "cursor", "code", model="cursor-grok-4.6-high-fast"),
            "cursor_composer": Agent("cursor_composer", "cursor", "code", model="composer-2.5"),
            "cursor_spare": Agent("cursor_spare", "cursor", "code", model="kimi-k2.7"),
        },
    )


ADMISSION_PROVENANCE_KEYS = (
    "schema",
    "source",
    "roster_revision",
    "roster_digest",
    "seat",
    "provider",
    "model",
    "reasoning",
    "binding",
    "expires_at",
)
RETIRED_SPELLINGS = (
    ("codex", "gpt-5.4"),
    ("openai", "gpt-5.4"),
    ("openai-codex", "openai/gpt-5.4"),
    ("codex", "gpt-5.4-high"),
    ("openai", "gpt-5.5"),
    ("openai-codex", "gpt-5.5:preview"),
)


def _row(seat: str, provider: str, model: str, *, enabled: bool = True) -> dict[str, object]:
    return {
        "seat": seat,
        "provider": provider,
        "model": model,
        "enabled": enabled,
        "limit": None,
        "notes": None,
    }


def _versioned_seat(
    seat: str,
    provider: str,
    model: str,
    *,
    reasoning: str = "high",
    enabled: bool = True,
    instance_id: str = "cursor",
) -> dict[str, object]:
    return {
        "seat": seat,
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "enabled": enabled,
        "bindings": {
            "brigade": {"cli": instance_id},
            "t3_fleet": {"instance_id": instance_id, "service_tier": None},
        },
    }


def _versioned_snapshot(
    *seats: dict[str, object],
    source: str = "hub",
    revision: int = 2,
    digest: str | None = None,
    expires_at: str = "2026-08-30T14:15:00Z",
    state: str = "authoritative",
) -> dict[str, object]:
    digest = digest or ("sha256:" + ("ab" * 32))
    return {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "state": state,
        "source": source,
        "revision": revision,
        "roster_revision": revision,
        "document_sha256": digest,
        "expires_at": expires_at,
        "seats": list(seats),
        "consumer_defaults": {"brigade-run": seats[0]["seat"] if seats else None},
        "retired_models": [],
    }


def _mock_exact_runtime_admission(monkeypatch, snapshot: dict[str, object]) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def admit(**kwargs):
        calls.append(kwargs)
        row = next(item for item in snapshot["seats"] if item["seat"] == kwargs["seat"])
        return fleet_model_admission.ModelAdmissionDecision(
            True,
            0,
            "admitted",
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "authoritative",
                "source": snapshot["source"],
                "roster_revision": snapshot["revision"],
                "roster_digest": snapshot["document_sha256"],
                "seat": row["seat"],
                "provider": row["provider"],
                "model": row["model"],
                "reasoning": row["reasoning"],
                "binding": {"instance_id": row["bindings"]["brigade"]["cli"], "service_tier": None},
                "expires_at": snapshot["expires_at"],
            },
        )

    monkeypatch.setattr(fleet_model_admission, "admit_model", admit)
    return calls


def test_versioned_envelope_requires_document_sha256_and_nested_bindings():
    issued_at = fleet_model_admission.utc_now().replace(microsecond=0)
    envelope = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": 2,
        "revision_updated_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (issued_at + timedelta(seconds=fleet_model_roster.LKG_TTL_SECONDS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "audience_node_id": NODE_A,
        "seats": [
            {
                "seat": "cursor_grok",
                "provider": "cursor",
                "model": "cursor-grok-4.6-high-fast",
                "reasoning": "high",
                "enabled": True,
                "bindings": {
                    "brigade": {"cli": "cursor-agent"},
                    "t3_fleet": {"instance_id": "cursor", "service_tier": "standard"},
                },
            }
        ],
        "consumer_defaults": {"brigade-run": "cursor_grok", "t3-fleet": "cursor_grok"},
        "retired_models": [],
    }
    envelope["document_sha256"] = fleet_model_roster.roster_digest(envelope)
    envelope["mac"] = {
        "algorithm": fleet_model_roster.MAC_ALGORITHM,
        "value": fleet_model_roster.roster_mac("node-token", envelope),
    }

    assert fleet_model_admission._validate_envelope(envelope, token="node-token", audience=NODE_A) is None

    flat = json.loads(json.dumps(envelope))
    flat["seats"][0]["bindings"] = {"brigade_cli": "cursor-agent", "t3_instance_id": "cursor"}
    flat["document_sha256"] = fleet_model_roster.roster_digest(flat)
    flat["mac"]["value"] = fleet_model_roster.roster_mac("node-token", flat)
    assert fleet_model_admission._validate_envelope(flat, token="node-token", audience=NODE_A) == "malformed-roster"

    legacy_digest = json.loads(json.dumps(envelope))
    legacy_digest["roster_digest"] = legacy_digest.pop("document_sha256")
    legacy_digest["mac"]["value"] = fleet_model_roster.roster_mac("node-token", legacy_digest)
    assert (
        fleet_model_admission._validate_envelope(legacy_digest, token="node-token", audience=NODE_A)
        == "malformed-roster"
    )


def _admission_from_payload(payload: dict[str, object], seat: str) -> dict[str, object] | None:
    admission = payload.get("model_admission")
    if isinstance(admission, dict) and (admission.get("seat") == seat or not seat):
        return admission
    for item in payload.get("admissions") or []:
        if isinstance(item, dict) and item.get("seat") == seat:
            return item
    for item in payload.get("seat_routing") or []:
        if not isinstance(item, dict):
            continue
        nested = item.get("model_admission")
        if isinstance(nested, dict) and nested.get("seat") == seat:
            return nested
        if item.get("requested_seat") == seat and all(key in item for key in ADMISSION_PROVENANCE_KEYS):
            return item
    return None


def _assert_admission_provenance(
    payload: dict[str, object],
    *,
    seat: str,
    provider: str,
    model: str,
    reasoning: str,
    source: str,
    revision: int,
    digest: str,
) -> dict[str, object]:
    admission = _admission_from_payload(payload, seat)
    assert isinstance(admission, dict), f"missing model_admission for {seat} in {sorted(payload)}"
    for key in ADMISSION_PROVENANCE_KEYS:
        assert key in admission, f"missing {key} in model_admission"
    assert admission["schema"] == fleet_model_roster.ADMISSION_SCHEMA
    assert admission["source"] == source
    assert admission["roster_revision"] == revision
    assert admission["roster_digest"] == digest
    assert admission["seat"] == seat
    assert admission["provider"] == provider
    assert admission["model"] == model
    assert admission["reasoning"] == reasoning
    binding = admission["binding"]
    assert isinstance(binding, dict)
    assert isinstance(binding.get("instance_id"), str) and binding["instance_id"]
    assert isinstance(admission["expires_at"], str) and admission["expires_at"]
    return admission


def test_model_policy_model_slugifies_display_names_deterministically():
    assert agents.model_policy_model("antigravity", "Gemini 3.7 Flash (Low)") == "gemini-3.7-flash-low"
    assert agents.model_policy_model("antigravity", "  Gemini   3.7 / Flash ((Low))  ") == "gemini-3.7-flash-low"
    assert agents.model_policy_model("antigravity", "Gemini 3.8 Flash (Low)") == "gemini-3.8-flash-low"


def test_model_policy_model_preserves_valid_ids_exactly():
    for model in (
        "cursor-grok-4.6-high-fast",
        "composer-2.5",
        "gpt-5.6-terra",
        "configured",
        "gemini-3.7-flash-low",
    ):
        assert agents.model_policy_model("cursor", model) == model


def test_authoritative_policy_keeps_distinct_cursor_pins_and_filters_unlisted_seats():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        snapshot={
            "state": "authoritative",
            "models": [
                _row("chef", "anthropic", "opus-5"),
                _row("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
                _row("cursor_composer", "cursor", "composer-2.5"),
            ],
        },
    )

    assert resolution.error is None
    assert set(resolution.roster.agents) == {"chef", "cursor_grok", "cursor_composer"}
    assert resolution.receipt["state"] == "authoritative"
    decisions = {item["seat"]: item for item in resolution.receipt["decisions"]}
    assert decisions["cursor_grok"]["outcome"] == "enabled"
    assert decisions["cursor_composer"]["outcome"] == "enabled"
    assert decisions["cursor_spare"]["outcome"] == "omitted"


def test_direct_worker_model_override_requires_exact_enabled_registry_entry():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        model_override="composer-2.5",
        snapshot={
            "state": "authoritative",
            "models": [
                _row("cursor_grok", "cursor", "composer-2.5"),
                _row("cursor_composer", "cursor", "composer-2.5"),
            ],
        },
    )

    assert resolution.error is None
    assert resolution.roster.agents["cursor_grok"].model == "composer-2.5"
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == "cursor_grok")
    assert decision["requested_model"] == "composer-2.5"
    assert decision["outcome"] == "enabled"


def test_direct_worker_model_override_fails_closed_when_registry_model_differs():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        model_override="composer-2.5",
        snapshot={
            "state": "authoritative",
            "models": [_row("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")],
        },
    )

    assert resolution.error == (
        "fleet model policy denied seat 'cursor_grok': requested cursor/composer-2.5, "
        "registry allows cursor/cursor-grok-4.6-high-fast"
    )


def test_versioned_snapshot_fails_closed_without_exact_reasoning_and_binding():
    seat = _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")
    del seat["reasoning"]
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        snapshot=_versioned_snapshot(seat),
    )
    assert resolution.error is not None
    assert "cursor_grok" in resolution.error

    unbound = _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")
    unbound["bindings"] = {}
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        snapshot=_versioned_snapshot(unbound),
    )
    assert resolution.error is not None
    assert "cursor_grok" in resolution.error


def test_admitted_none_reasoning_is_not_applied_as_a_pin():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        snapshot=_versioned_snapshot(
            _versioned_seat("chef", "anthropic", "opus-5", reasoning="none", instance_id="claude"),
        ),
    )

    assert resolution.error is None
    assert resolution.roster.agents["chef"].reasoning is None
    admission = next(item for item in resolution.receipt["admissions"] if item["seat"] == "chef")
    assert admission["reasoning"] == "none"
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == "chef")
    assert decision["reasoning_applied"] is False
    assert decision["detail"] == "exact seat/provider/model/reasoning/binding entry is enabled"


def test_admitted_reasoning_is_dropped_for_adapters_without_reasoning_support():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        snapshot=_versioned_snapshot(
            _versioned_seat("chef", "anthropic", "opus-5", reasoning="high", instance_id="claude"),
        ),
    )

    assert resolution.error is None
    assert resolution.roster.agents["chef"].reasoning is None
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == "chef")
    assert decision["outcome"] == "enabled"
    assert decision["reasoning_applied"] is False
    assert decision["detail"] == (
        "exact seat/provider/model/reasoning/binding entry is enabled; reasoning pin not supported by claude"
    )
    admission = next(item for item in resolution.receipt["admissions"] if item["seat"] == "chef")
    assert admission["reasoning"] == "high"


def _reasoning_adapter_roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "coder": Agent("coder", "codex", "code", model="gpt-5.6-terra"),
        },
    )


def test_admitted_reasoning_is_preserved_for_reasoning_adapters():
    resolution = aboyeur.resolve_fleet_model_policy(
        _reasoning_adapter_roster(),
        worker="coder",
        snapshot=_versioned_snapshot(
            _versioned_seat("coder", "openai", "gpt-5.6-terra", reasoning="high", instance_id="codex"),
        ),
    )

    assert resolution.error is None
    assert resolution.roster.agents["coder"].reasoning == "high"
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == "coder")
    assert decision["reasoning_applied"] is True
    assert decision["detail"] == "exact seat/provider/model/reasoning/binding entry is enabled"
    admission = next(item for item in resolution.receipt["admissions"] if item["seat"] == "coder")
    assert admission["reasoning"] == "high"


def test_admitted_blank_reasoning_is_not_applied_as_a_pin():
    resolution = aboyeur.resolve_fleet_model_policy(
        _reasoning_adapter_roster(),
        worker="coder",
        snapshot=_versioned_snapshot(
            _versioned_seat("coder", "openai", "gpt-5.6-terra", reasoning="   ", instance_id="codex"),
        ),
    )

    assert resolution.error is None
    assert resolution.roster.agents["coder"].reasoning is None
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == "coder")
    assert decision["outcome"] == "enabled"
    assert decision["reasoning_applied"] is False
    assert decision["detail"] == "exact seat/provider/model/reasoning/binding entry is enabled"
    admission = next(item for item in resolution.receipt["admissions"] if item["seat"] == "coder")
    assert admission["reasoning"] == "   "


def test_admitted_none_reasoning_is_dropped_for_reasoning_adapters_too():
    resolution = aboyeur.resolve_fleet_model_policy(
        _reasoning_adapter_roster(),
        worker="coder",
        snapshot=_versioned_snapshot(
            _versioned_seat("coder", "openai", "gpt-5.6-terra", reasoning="none", instance_id="codex"),
        ),
    )

    assert resolution.error is None
    assert resolution.roster.agents["coder"].reasoning is None
    admission = next(item for item in resolution.receipt["admissions"] if item["seat"] == "coder")
    assert admission["reasoning"] == "none"


def test_unconfigured_hub_preserves_local_roster_behavior():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        snapshot={"state": "unconfigured", "models": []},
    )

    assert resolution.error is None
    assert resolution.roster == _roster()
    assert resolution.receipt["authoritative"] is False
    assert resolution.receipt["state"] == "unconfigured"


def test_configured_unavailable_hub_denies_new_dispatch():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        snapshot={"state": "unavailable", "models": []},
    )

    assert resolution.error == "fleet model policy hub is unavailable; refusing new dispatch"


def test_hub_auth_failure_denies_new_dispatch():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        snapshot={"state": "auth-failed", "models": []},
    )

    assert resolution.error == "fleet model policy could not authenticate this node; refusing new dispatch"


def test_model_policy_snapshot_distinguishes_unconfigured_outage_and_auth(monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": "", "token": ""},
    )
    assert fleet_client.load_model_policy_snapshot() == {"state": "unconfigured", "models": []}

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": "https://hub.invalid", "token": "node-token"},
    )
    monkeypatch.setattr(
        fleet_client,
        "_run_with_deadline",
        lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    assert fleet_client.load_model_policy_snapshot() == {"state": "unavailable", "models": []}

    monkeypatch.setattr(
        fleet_client,
        "_run_with_deadline",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError("https://hub.invalid/models", 401, "no", {}, None)
        ),
    )
    assert fleet_client.load_model_policy_snapshot() == {"state": "auth-failed", "models": []}


def test_configured_hub_snapshot_is_versioned_roster_not_legacy_models(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        snapshot = fleet_client.load_model_policy_snapshot()

    assert snapshot.get("state") == "authoritative"
    assert snapshot.get("source") == "hub"
    assert snapshot.get("schema") == fleet_model_roster.ROSTER_SCHEMA
    assert snapshot.get("roster_revision") == roster["revision"]
    assert snapshot.get("document_sha256") == roster["document_sha256"]
    assert snapshot.get("roster_digest") == roster["document_sha256"]
    assert isinstance(snapshot.get("expires_at"), str) and snapshot["expires_at"]
    seats = snapshot.get("seats")
    assert isinstance(seats, list) and seats
    match = next(item for item in seats if isinstance(item, dict) and item.get("seat") == "cursor_grok")
    assert match["provider"] == "cursor"
    assert match["model"] == "cursor-grok-4.6-high-fast"
    assert match["reasoning"] == "high"
    bindings = match["bindings"]
    assert isinstance(bindings, dict)
    assert bindings.get("brigade") == {"cli": "cursor-agent"}
    assert "models" not in match
    _secret_free(snapshot, node_token, ADMIN_TOKEN)


def test_versioned_snapshot_rejects_legacy_digest_without_document_sha256():
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
    )
    snapshot["roster_digest"] = snapshot.pop("document_sha256")

    resolution = aboyeur.resolve_fleet_model_policy(_roster(), worker="cursor_grok", snapshot=snapshot)

    assert resolution.error == "fleet model policy snapshot is malformed; refusing new dispatch"


def test_versioned_resolution_selects_hub_row_over_local_retired_default():
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "coder": Agent("coder", "codex", "code", model="gpt-5.4"),
        },
    )
    resolution = aboyeur.resolve_fleet_model_policy(
        roster,
        worker="coder",
        snapshot=_versioned_snapshot(
            _versioned_seat("coder", "openai", "gpt-5.6-terra", reasoning="medium", instance_id="codex"),
        ),
    )

    assert resolution.error is None
    agent = resolution.roster.agents["coder"]
    assert agent.cli == "codex"
    assert agent.model == "gpt-5.6-terra"
    assert agent.reasoning == "medium"
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == "coder")
    assert decision["outcome"] == "enabled"
    assert decision["policy_provider"] == "openai"
    assert decision["policy_model"] == "gpt-5.6-terra"


def test_inconsistent_binding_rejected_before_lease_or_process(tmp_path, monkeypatch):
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast", instance_id="codex"),
    )
    lease_calls: list[tuple[str, str, str]] = []
    provider_calls: list[str] = []
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda seat, provider, model: (
            lease_calls.append((seat, provider, model))
            or fleet_client.ModelLeaseDecision(True, "ok", "should-not-lease", "holder")
        ),
    )
    monkeypatch.setattr(
        agents,
        "run_agent",
        lambda *args, **kwargs: provider_calls.append("run_agent") or agents.AgentResult(text="no", ok=True),
    )
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )

    run = json.loads((output_dir / "run.json").read_text())
    assert run["failure"]["kind"] == "fleet-model-policy"
    assert run["failure"]["phase"] == "preflight"
    assert lease_calls == []
    assert provider_calls == []


def test_brigade_run_without_injected_snapshot_dispatches_hub_approved_seat(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        acquired: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            fleet_client,
            "acquire_model_lease",
            lambda seat, provider, model: (
                acquired.append((seat, provider, model))
                or fleet_client.ModelLeaseDecision(True, "ok", "lease", "holder")
            ),
        )
        monkeypatch.setattr(
            fleet_client,
            "release_model_lease",
            lambda lease_id, *, holder: fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder),
        )
        dispatched: list[dict[str, object]] = []

        def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
            dispatched.append({"cli": cli_ref, "model": kwargs.get("model"), "reasoning": kwargs.get("reasoning")})
            return agents.AgentResult(text="ok", ok=True)

        monkeypatch.setattr(agents, "run_agent", fake_run_agent)
        local = Roster(
            orchestrator="chef",
            agents={
                "chef": Agent("chef", "claude", "plan", model="opus-5"),
                "cursor_grok": Agent("cursor_grok", "cursor", "code", model="composer-2.5"),
            },
        )
        output_dir = tmp_path / "run"
        assert (
            run_aboyeur_guarded(
                "inspect",
                local,
                worker="cursor_grok",
                output_dir=output_dir,
                code_graph_enabled=False,
                route_enabled=False,
            )
            == 0
        )

        # cursor-agent has no reasoning flag: the Hub pin stays in the admission
        # receipt below, but the launch spec drops it instead of failing dispatch.
        assert dispatched == [{"cli": "cursor", "model": "cursor-grok-4.6-high-fast", "reasoning": None}]
        assert acquired == [("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")]
        policy = json.loads((output_dir / "model-policy.json").read_text())
        run = json.loads((output_dir / "run.json").read_text())
        roster_out = json.loads((output_dir / "roster.json").read_text())
        expected = dict(
            seat="cursor_grok",
            provider="cursor",
            model="cursor-grok-4.6-high-fast",
            reasoning="high",
            source="hub",
            revision=int(roster["revision"]),
            digest=str(roster["document_sha256"]),
        )
        for payload in (policy, run, roster_out):
            admission = _assert_admission_provenance(payload, **expected)
            assert admission["binding"]["instance_id"] == "cursor-agent"
            _secret_free(admission, node_token, ADMIN_TOKEN)


def test_target_admission_fails_closed_when_roster_changes_after_controller_admission(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        controller_snapshot = fleet_client.load_model_policy_snapshot()
        target_calls: list[dict[str, object]] = []
        original_admit = fleet_model_admission.admit_model

        def admit_target(**kwargs):
            target_calls.append(kwargs)
            decision = original_admit(**kwargs)
            if kwargs["phase"] == "brigade-run" and decision.ok:
                assert _admin_set(hub, enabled=False)[0] == 200
            return decision

        controller_reads = 0

        def snapshot_then_mutate():
            nonlocal controller_reads
            controller_reads += 1
            return controller_snapshot

        monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", snapshot_then_mutate)
        monkeypatch.setattr(fleet_model_admission, "admit_model", admit_target)
        lease_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            fleet_client,
            "acquire_model_lease",
            lambda seat, provider, model: (
                lease_calls.append((seat, provider, model))
                or fleet_client.ModelLeaseDecision(True, "ok", "lease", "holder")
            ),
        )
        dispatched: list[str] = []
        monkeypatch.setattr(
            agents,
            "run_agent",
            lambda cli_ref, *args, **kwargs: (
                dispatched.append(cli_ref) or agents.AgentResult(text="unexpected", ok=True)
            ),
        )

        output_dir = tmp_path / "run"
        assert (
            run_aboyeur_guarded(
                "inspect",
                _roster(),
                worker="cursor_grok",
                output_dir=output_dir,
                code_graph_enabled=False,
                route_enabled=False,
            )
            == 2
        )

    assert controller_reads == 1
    assert [call["phase"] for call in target_calls] == ["brigade-run", "target"]
    target_call = target_calls[1]
    assert target_call["consumer"] == "brigade-run"
    assert target_call["phase"] == "target"
    assert target_call["seat"] == "cursor_grok"
    assert target_call["expect_revision"] == int(roster["revision"])
    assert target_call["expect_digest"] == str(roster["document_sha256"])
    assert target_call["allow_lkg"] is False
    assert isinstance(target_call["request_id"], str) and target_call["request_id"]
    assert lease_calls == []
    assert dispatched == []


def test_authoritative_legacy_snapshot_without_admissions_cannot_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_model_policy_snapshot",
        lambda: {
            "state": "authoritative",
            "models": [_row("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")],
        },
    )
    lease_calls: list[str] = []
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda seat, provider, model: (
            lease_calls.append(seat) or fleet_client.ModelLeaseDecision(True, "ok", "lease", "holder")
        ),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        agents,
        "run_agent",
        lambda cli_ref, *args, **kwargs: dispatched.append(cli_ref) or agents.AgentResult(text="unexpected", ok=True),
    )

    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            output_dir=tmp_path / "run",
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )
    assert lease_calls == []
    assert dispatched == []


def test_target_admission_must_match_controller_resolution_before_launch(tmp_path, monkeypatch):
    digest = "sha256:" + ("cd" * 32)
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
        revision=8,
        digest=digest,
        expires_at="2099-08-30T14:15:00Z",
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    admission_calls: list[dict[str, object]] = []

    def admit(**kwargs):
        admission_calls.append(kwargs)
        payload = {
            "schema": fleet_model_roster.ADMISSION_SCHEMA,
            "state": "authoritative",
            "source": "hub",
            "roster_revision": 8,
            "roster_digest": digest,
            "seat": "cursor_grok",
            "provider": "cursor",
            "model": "cursor-grok-4.6-high-fast",
            "reasoning": "high",
            "binding": {"instance_id": "cursor", "service_tier": None},
            "expires_at": "2099-08-30T14:15:00Z",
        }
        if kwargs["phase"] == "target":
            payload["model"] = "forged-model"
        return fleet_model_admission.ModelAdmissionDecision(True, 0, "admitted", payload)

    monkeypatch.setattr(fleet_model_admission, "admit_model", admit)
    lease_calls: list[str] = []
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda seat, provider, model: (
            lease_calls.append(seat) or fleet_client.ModelLeaseDecision(True, "ok", "lease", "holder")
        ),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        agents,
        "run_agent",
        lambda cli_ref, *args, **kwargs: dispatched.append(cli_ref) or agents.AgentResult(text="unexpected", ok=True),
    )

    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            output_dir=tmp_path / "run",
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )
    assert [call["phase"] for call in admission_calls] == ["brigade-run", "target"]
    assert lease_calls == []
    assert dispatched == []


def test_run_writes_effective_policy_and_override_to_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_model_policy_snapshot",
        lambda: {
            "state": "authoritative",
            "models": [_row("cursor_grok", "cursor", "composer-2.5")],
        },
    )
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            model_override="composer-2.5",
            dry_run=True,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    policy = json.loads((output_dir / "model-policy.json").read_text())
    run = json.loads((output_dir / "run.json").read_text())
    roster = json.loads((output_dir / "roster.json").read_text())
    assert policy["state"] == "authoritative"
    assert policy["model_override"] == "composer-2.5"
    assert roster["agents"]["cursor_grok"]["model"] == "composer-2.5"
    assert any(
        decision.get("typed_cause") == "fleet-model-policy" and decision.get("requested_model") == "composer-2.5"
        for decision in run["seat_routing"]
    )


def test_direct_worker_acquires_only_its_invoked_seat_and_releases(monkeypatch, tmp_path):
    acquired: list[tuple[str, str, str]] = []
    released: list[tuple[str, str]] = []
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
        expires_at="2099-08-30T14:15:00Z",
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    _mock_exact_runtime_admission(monkeypatch, snapshot)

    def acquire(seat, provider, model):
        acquired.append((seat, provider, model))
        return fleet_client.ModelLeaseDecision(True, "ok", "direct-lease", "direct-holder")

    monkeypatch.setattr(fleet_client, "acquire_model_lease", acquire)
    monkeypatch.setattr(
        fleet_client,
        "release_model_lease",
        lambda lease_id, *, holder: (
            released.append((lease_id, holder)) or fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder)
        ),
    )
    monkeypatch.setattr(
        agents,
        "run_agent",
        lambda *args, **kwargs: agents.AgentResult(text="implemented", ok=True),
    )

    assert (
        run_aboyeur_guarded(
            "implement it",
            _roster(),
            worker="cursor_grok",
            output_dir=tmp_path / "run",
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert acquired == [("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")]
    assert released == [("direct-lease", "direct-holder")]


def test_direct_worker_runs_with_only_its_authoritative_policy_seat(monkeypatch, tmp_path):
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", fallback=("coder",)),
            "coder": Agent("coder", "codex", "code"),
            "reviewer": Agent("reviewer", "codex", "review"),
            "cursor_grok": Agent("cursor_grok", "cursor", "code", model="cursor-grok-4.6-high-fast"),
        },
    )
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
        expires_at="2099-08-30T14:15:00Z",
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    _mock_exact_runtime_admission(monkeypatch, snapshot)
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda *args, **kwargs: fleet_client.ModelLeaseDecision(True, "ok", "direct-lease", "direct-holder"),
    )
    monkeypatch.setattr(
        fleet_client,
        "release_model_lease",
        lambda lease_id, *, holder: fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder),
    )
    invocations: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
        invocations.append(cli_ref)
        return agents.AgentResult(text="implemented", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "implement it",
            roster,
            worker="cursor_grok",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    effective = json.loads((output_dir / "roster.json").read_text())
    assert "coder" not in effective["agents"]
    assert "reviewer" not in effective["agents"]
    assert invocations == ["cursor"]


def test_direct_worker_claude_seat_dispatches_under_none_reasoning_admission(monkeypatch, tmp_path):
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "claude_standby": Agent("claude_standby", "claude", "code", model="opus"),
        },
    )
    snapshot = _versioned_snapshot(
        _versioned_seat("claude_standby", "anthropic", "opus", reasoning="none", instance_id="claude"),
        expires_at="2099-08-30T14:15:00Z",
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    _mock_exact_runtime_admission(monkeypatch, snapshot)
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda *args, **kwargs: fleet_client.ModelLeaseDecision(True, "ok", "direct-lease", "direct-holder"),
    )
    monkeypatch.setattr(
        fleet_client,
        "release_model_lease",
        lambda lease_id, *, holder: fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder),
    )
    launched: list[list[str]] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):
        launched.append(
            agents.build_argv(
                cli_ref,
                prompt,
                read_only=True,
                model=kwargs.get("model"),
                reasoning=kwargs.get("reasoning"),
            )
        )
        return agents.AgentResult(text="implemented", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "implement it",
            roster,
            worker="claude_standby",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert launched, "worker seat never dispatched"
    argv = launched[0]
    assert not any(
        token in ("--reasoning", "--reasoning-effort", "--thinking", "--variant") or "model_reasoning_effort" in token
        for token in argv
    )
    effective = json.loads((output_dir / "roster.json").read_text())
    assert effective["agents"]["claude_standby"]["reasoning"] is None


def test_orchestrated_run_leases_only_planner_worker_and_synthesis_seats(monkeypatch, tmp_path):
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "coder": Agent("coder", "codex", "code", model="gpt-5.6-terra"),
            "reviewer": Agent("reviewer", "codex", "review", model="gpt-5.6-luna"),
        },
    )
    acquired: list[str] = []
    released: list[str] = []
    snapshot = _versioned_snapshot(
        _versioned_seat("chef", "anthropic", "opus-5", instance_id="claude"),
        _versioned_seat("coder", "openai", "gpt-5.6-terra", instance_id="codex"),
        _versioned_seat("reviewer", "openai", "gpt-5.6-luna", instance_id="codex"),
        expires_at="2099-08-30T14:15:00Z",
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    _mock_exact_runtime_admission(monkeypatch, snapshot)

    def acquire(seat, provider, model):  # noqa: ARG001
        acquired.append(seat)
        return fleet_client.ModelLeaseDecision(True, "ok", f"lease-{seat}-{len(acquired)}", f"holder-{seat}")

    monkeypatch.setattr(fleet_client, "acquire_model_lease", acquire)
    monkeypatch.setattr(
        fleet_client,
        "release_model_lease",
        lambda lease_id, *, holder: (
            released.append(lease_id) or fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder)
        ),
    )

    calls: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
        calls.append(cli_ref)
        if len(calls) == 1:
            return agents.AgentResult(text='{"assignments":[{"worker":"coder","task":"implement it"}]}', ok=True)
        if len(calls) == 2:
            return agents.AgentResult(text="implemented", ok=True)
        return agents.AgentResult(text="synthesized", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    assert (
        run_aboyeur_guarded(
            "implement it",
            roster,
            output_dir=tmp_path / "run",
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert acquired == ["chef", "coder", "chef"]
    assert len(released) == 3
    assert "reviewer" not in acquired


def test_run_cli_requires_worker_for_model_override(tmp_path, capsys):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(
        'orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\nmodel = "gpt-5.6-terra"\n'
    )

    rc = cli.main(
        [
            "run",
            "inspect",
            "--roster",
            str(roster_path),
            "--cwd",
            str(tmp_path),
            "--model",
            "gpt-5.6-luna",
        ]
    )

    assert rc == 2
    assert "--model requires --worker" in capsys.readouterr().err


def test_run_terminalizes_configured_hub_outage_as_model_policy_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_model_policy_snapshot",
        lambda: {"state": "unavailable", "models": []},
    )
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            dry_run=True,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )

    run = json.loads((output_dir / "run.json").read_text())
    assert run["status"] == "failed"
    assert run["failure"] == {
        "phase": "preflight",
        "kind": "fleet-model-policy",
        "detail": "fleet model policy hub is unavailable; refusing new dispatch",
        "seat": "cursor_grok",
    }


def test_direct_worker_versioned_admission_is_persisted_in_all_three_artifacts(tmp_path, monkeypatch):
    digest = "sha256:" + ("cd" * 32)
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast", reasoning="high"),
        source="hub",
        revision=7,
        digest=digest,
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            dry_run=True,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    policy = json.loads((output_dir / "model-policy.json").read_text())
    run = json.loads((output_dir / "run.json").read_text())
    roster = json.loads((output_dir / "roster.json").read_text())
    expected = dict(
        seat="cursor_grok",
        provider="cursor",
        model="cursor-grok-4.6-high-fast",
        reasoning="high",
        source="hub",
        revision=7,
        digest=digest,
    )
    for payload in (policy, run, roster):
        admission = _assert_admission_provenance(payload, **expected)
        _secret_free(admission, "Bearer", "prompt", "/tmp/", str(tmp_path))
        assert "hmac" not in json.dumps(admission)
        assert "mac" not in admission


def test_orchestrated_seats_persist_exact_admission_before_lease(tmp_path, monkeypatch):
    digest = "sha256:" + ("ef" * 32)
    snapshot = _versioned_snapshot(
        _versioned_seat("chef", "anthropic", "opus-5", reasoning="high", instance_id="claude"),
        _versioned_seat("coder", "openai", "gpt-5.6-terra", reasoning="medium", instance_id="codex"),
        _versioned_seat("reviewer", "openai", "gpt-5.6-luna", reasoning="low", instance_id="codex"),
        revision=4,
        digest=digest,
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    target_admissions: list[dict[str, object]] = []

    def admit_exact(**kwargs):
        target_admissions.append(kwargs)
        row = next(item for item in snapshot["seats"] if item["seat"] == kwargs["seat"])
        return fleet_model_admission.ModelAdmissionDecision(
            True,
            0,
            "admitted",
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "authoritative",
                "source": "hub",
                "roster_revision": 4,
                "roster_digest": digest,
                "seat": row["seat"],
                "provider": row["provider"],
                "model": row["model"],
                "reasoning": row["reasoning"],
                "binding": {"instance_id": row["bindings"]["brigade"]["cli"], "service_tier": None},
                "expires_at": snapshot["expires_at"],
            },
        )

    monkeypatch.setattr(
        fleet_model_admission,
        "admit_model",
        admit_exact,
    )
    acquired: list[str] = []
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda seat, provider, model: (
            acquired.append(seat) or fleet_client.ModelLeaseDecision(True, "ok", f"lease-{seat}", f"holder-{seat}")
        ),
    )
    monkeypatch.setattr(
        fleet_client,
        "release_model_lease",
        lambda lease_id, *, holder: fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder),
    )
    calls: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
        calls.append(cli_ref)
        if len(calls) == 1:
            return agents.AgentResult(text='{"assignments":[{"worker":"coder","task":"implement it"}]}', ok=True)
        if len(calls) == 2:
            return agents.AgentResult(text="implemented", ok=True)
        return agents.AgentResult(text="synthesized", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5", reasoning="high"),
            "coder": Agent("coder", "codex", "code", model="gpt-5.6-terra", reasoning="medium"),
            "reviewer": Agent("reviewer", "codex", "review", model="gpt-5.6-luna", reasoning="low"),
        },
    )
    output_dir = tmp_path / "run"
    assert (
        run_aboyeur_guarded(
            "implement it",
            roster,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    policy = json.loads((output_dir / "model-policy.json").read_text())
    run = json.loads((output_dir / "run.json").read_text())
    roster_payload = json.loads((output_dir / "roster.json").read_text())
    admissions = policy.get("admissions")
    assert isinstance(admissions, list)
    by_seat = {item["seat"]: item for item in admissions}
    assert set(by_seat) >= {"chef", "coder"}
    assert by_seat["chef"]["reasoning"] == "high"
    assert by_seat["coder"]["reasoning"] == "medium"
    assert by_seat["chef"]["roster_revision"] == 4
    assert by_seat["coder"]["roster_digest"] == digest
    _assert_admission_provenance(
        policy,
        seat="chef",
        provider="anthropic",
        model="opus-5",
        reasoning="high",
        source="hub",
        revision=4,
        digest=digest,
    )
    _assert_admission_provenance(
        run,
        seat="chef",
        provider="anthropic",
        model="opus-5",
        reasoning="high",
        source="hub",
        revision=4,
        digest=digest,
    )
    _assert_admission_provenance(
        roster_payload,
        seat="chef",
        provider="anthropic",
        model="opus-5",
        reasoning="high",
        source="hub",
        revision=4,
        digest=digest,
    )
    assert acquired == ["chef", "coder", "chef"]
    assert "reviewer" not in acquired
    assert [
        (item["phase"], item["seat"], item["expect_revision"], item["expect_digest"], item["allow_lkg"])
        for item in target_admissions
    ] == [
        ("brigade-run", "chef", 4, digest, True),
        ("target", "chef", 4, digest, False),
        ("brigade-run", "coder", 4, digest, True),
        ("target", "coder", 4, digest, False),
        ("brigade-run", "chef", 4, digest, True),
        ("target", "chef", 4, digest, False),
    ]
    request_ids = [item["request_id"] for item in target_admissions]
    assert len(request_ids) == len(set(request_ids))
    for payload in (policy, run, roster_payload):
        serialized = json.dumps(payload, sort_keys=True)
        assert '"phase": "target"' in serialized
        assert '"request_id":' in serialized


@pytest.mark.parametrize(("cli", "model"), RETIRED_SPELLINGS)
def test_permanent_floor_stops_retired_spellings_before_provider_or_lease(tmp_path, monkeypatch, cli, model):
    snapshot = _versioned_snapshot(
        _versioned_seat("coder", "openai", model, reasoning="medium", instance_id="codex"),
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    provider_calls: list[str] = []
    lease_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda seat, provider, leased_model: (
            lease_calls.append((seat, provider, leased_model))
            or fleet_client.ModelLeaseDecision(True, "ok", "should-not-lease", "holder")
        ),
    )
    monkeypatch.setattr(
        agents,
        "run_agent",
        lambda *args, **kwargs: provider_calls.append("run_agent") or agents.AgentResult(text="no", ok=True),
    )
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "coder": Agent("coder", cli, "code", model=model),
        },
    )
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "implement it",
            roster,
            worker="coder",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )

    run = json.loads((output_dir / "run.json").read_text())
    assert run["failure"]["kind"] == "fleet-model-policy"
    assert run["failure"]["phase"] == "preflight"
    assert provider_calls == []
    assert lease_calls == []


def test_configured_outage_accepts_valid_lkg_and_rejects_expired(tmp_path, monkeypatch):
    valid = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
        source="lkg",
        revision=3,
        digest="sha256:" + ("11" * 32),
        expires_at=(datetime.now(timezone.utc) + timedelta(seconds=800)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: valid)
    output_dir = tmp_path / "valid"
    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            dry_run=True,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )
    policy = json.loads((output_dir / "model-policy.json").read_text())
    run = json.loads((output_dir / "run.json").read_text())
    roster = json.loads((output_dir / "roster.json").read_text())
    for payload in (policy, run, roster):
        _assert_admission_provenance(
            payload,
            seat="cursor_grok",
            provider="cursor",
            model="cursor-grok-4.6-high-fast",
            reasoning="high",
            source="lkg",
            revision=3,
            digest="sha256:" + ("11" * 32),
        )

    expired = dict(valid)
    expired["expires_at"] = "2020-01-01T00:15:00Z"
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: expired)
    denied_dir = tmp_path / "expired"
    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            dry_run=True,
            output_dir=denied_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )
    denied = json.loads((denied_dir / "run.json").read_text())
    assert denied["failure"]["kind"] == "fleet-model-policy"
    assert denied["failure"]["phase"] == "preflight"


def test_denied_or_retired_seat_is_pruned_from_health_fallback_chain():
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5", fallback=("coder",)),
            "coder": Agent("coder", "codex", "code", model="gpt-5.5"),
            "reviewer": Agent("reviewer", "codex", "review", model="gpt-5.6-luna"),
        },
    )
    resolution = aboyeur.resolve_fleet_model_policy(
        roster,
        snapshot=_versioned_snapshot(
            _versioned_seat("chef", "anthropic", "opus-5", instance_id="claude"),
            _versioned_seat("reviewer", "openai", "gpt-5.6-luna", instance_id="codex"),
        ),
    )
    assert resolution.error is None
    assert "coder" not in resolution.roster.agents
    from brigade import seat_health

    assert "coder" not in seat_health._seat_chain_names(resolution.roster)
    unhealthy = (seat_health.SeatHealthCheck("declaration", "failed", "probe failed", cause_code="auth-required"),)
    routing = aboyeur.resolve_orchestrator_health_routing(
        resolution.roster,
        (
            seat_health.SeatHealthResult(
                "probe-chef",
                "chef",
                "fp",
                "unhealthy",
                {},
                unhealthy,
                0.0,
                0.0,
                0.0,
                0.0,
            ),
            seat_health.SeatHealthResult(
                "probe-reviewer",
                "reviewer",
                "fp",
                "healthy",
                {},
                (),
                0.0,
                0.0,
                0.0,
                0.0,
            ),
        ),
    )
    rendered = json.dumps(routing.receipt or {}, default=str)
    assert "coder" not in rendered
    assert routing.roster.orchestrator != "coder"
    assert "coder" not in routing.roster.agents


def _plant_home_identity(home: Path, node_id: str) -> None:
    from brigade import node as node_mod

    identity = node_mod.NodeIdentity(node_id=node_id, hostname="fleet-test", roles=(), platform="test")
    path = node_mod.node_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(node_mod._format_node_toml(identity), encoding="utf-8")


@contextmanager
def _hub(tmp_path):
    server = fleet_hub.make_server(
        "127.0.0.1", 0, tmp_path / "fleet.db", ADMIN_TOKEN
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_address[1], tmp_path / "fleet.db"  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, method: str, path: str, *, token: str | None = None, body: dict[str, object] | None = None):
    host, port, _db = hub
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(
        method, path, body=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers
    )
    response = connection.getresponse()
    result = response.status, json.loads(response.read().decode("utf-8"))
    connection.close()
    return result


def _enroll(hub, node_id: str = NODE_A) -> str:
    db = fleet_hub.open_db(hub[2])
    try:
        _node, node_token = fleet_hub.add_node(db, node_id, "node-a")
    finally:
        db.close()
    return node_token


def _current_revision(hub) -> int:
    status, payload = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
    assert status == 200
    return int(payload["revision"])


def _admin_set(hub, **fields: object):
    body = {"expected_revision": _current_revision(hub), **SEAT_BODY, **fields}
    return _request(hub, "POST", "/models", token=ADMIN_TOKEN, body=body)


def _configure_client(monkeypatch, tmp_path, hub, node_token: str) -> Path:
    home = tmp_path / "brigade-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", f"http://{hub[0]}:{hub[1]}")
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", node_token)
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", ADMIN_TOKEN)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    _plant_home_identity(home, NODE_A)
    return home


def _seed_hub(hub, node_token: str) -> dict[str, object]:
    assert _admin_set(hub)[0] == 200
    assert (
        _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "set-default",
                "consumer": "t3-fleet",
                "seat": "cursor_grok",
                "expected_revision": _current_revision(hub),
            },
        )[0]
        == 200
    )
    status, roster = _request(hub, "GET", "/models", token=node_token)
    assert status == 200
    return roster


def _secret_free(value: object, *secrets: str) -> None:
    rendered = json.dumps(value, default=str)
    for secret in secrets:
        if secret:
            assert secret not in rendered


def test_model_admission_decision_is_frozen_and_secret_free():
    from brigade import fleet_model_admission

    decision = fleet_model_admission.ModelAdmissionDecision(
        ok=True,
        exit_code=0,
        reason="admitted",
        payload={"schema": "brigade.model_admission.v1", "source": "hub"},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.ok = False  # type: ignore[misc]
    assert set(dataclasses.asdict(decision)) == {"ok", "exit_code", "reason", "payload"}
    _secret_free(decision, "Bearer", "/tmp/", "prompt")


def test_valid_node_roster_is_cached_after_mac_and_digest_checks(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        home = _configure_client(monkeypatch, tmp_path, hub, node_token)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is True
        assert decision.exit_code == 0
        assert decision.payload["schema"] == fleet_model_roster.ROSTER_SCHEMA
        assert decision.payload["revision"] == roster["revision"]
        assert decision.payload["document_sha256"] == roster["document_sha256"]
        assert decision.payload["audience_node_id"] == NODE_A
        lkg = fleet_model_admission.lkg_path()
        high_water = fleet_model_admission.high_water_path()
        assert lkg.is_file()
        assert high_water.is_file()
        assert lkg.is_relative_to(home)
        assert high_water.is_relative_to(home)
        assert stat.S_IMODE(os.stat(lkg.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(lkg).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(high_water).st_mode) == 0o600
        record = json.loads(lkg.read_text(encoding="utf-8"))
        assert record["highest_revision"] == roster["revision"]
        assert record["roster"]["mac"]["value"] == roster["mac"]["value"]
        assert record["roster"]["document_sha256"] == roster["document_sha256"]
        assert "cached_at" in record
        _secret_free(record, node_token, ADMIN_TOKEN)
        _secret_free(decision, node_token, ADMIN_TOKEN)


def test_admin_roster_read_cannot_seed_lkg(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN")
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        assert not fleet_model_admission.lkg_path().exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file identity and modes")
def test_lkg_and_high_water_reject_unsafe_files(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        home = _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        lkg = fleet_model_admission.lkg_path()
        high_water = fleet_model_admission.high_water_path()
        victim = tmp_path / "victim.json"
        victim.write_text("do not touch\n", encoding="utf-8")
        lkg.unlink()
        lkg.symlink_to(victim)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        assert victim.read_text(encoding="utf-8") == "do not touch\n"
        lkg.unlink()
        os.mkfifo(lkg)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        assert stat.S_ISFIFO(os.lstat(lkg).st_mode)
        lkg.unlink()
        high_water.write_bytes(b"x" * (fleet_model_admission.MODEL_ROSTER_MAX_BYTES + 1))
        os.chmod(high_water, 0o600)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        high_water.unlink()
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        os.chmod(lkg, 0o644)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        os.chmod(lkg, 0o600)
        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        _secret_free(decision, node_token, str(lkg), str(home))


@pytest.mark.skipif(os.name != "posix", reason="descriptor path-swap is POSIX-only")
def test_lkg_path_swap_between_validation_and_open_never_writes_decoy(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        lkg = fleet_model_admission.lkg_path()
        real_dir = lkg.parent
        decoy = tmp_path / "decoy-fleet"
        decoy.mkdir()
        os.chmod(decoy, 0o700)
        sentinel = decoy / lkg.name
        sentinel.write_text("do not touch\n", encoding="utf-8")
        real_ensure = fleet_client._ensure_private_dir

        def swap_after_open(path):
            fd = real_ensure(path)
            evacuated = tmp_path / "evacuated-fleet"
            path.rename(evacuated)
            decoy.rename(path)
            swap_after_open.evacuated = evacuated  # type: ignore[attr-defined]
            return fd

        monkeypatch.setattr(fleet_client, "_ensure_private_dir", swap_after_open)
        try:
            decision = fleet_model_admission.fetch_versioned_roster()
            assert decision.ok is True
        finally:
            if real_dir.exists() and decoy.exists() is False:
                real_dir.rename(decoy)
            evacuated = getattr(swap_after_open, "evacuated", None)
            if evacuated is not None and evacuated.exists():
                evacuated.rename(real_dir)
        assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
        assert sorted(p.name for p in decoy.iterdir()) == [sentinel.name]


def test_windows_without_nofollow_fails_closed(tmp_path, monkeypatch):
    """Hosts without safe cache primitives may use only the live Hub response."""
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        monkeypatch.setattr(fleet_model_admission, "nofollow_supported", lambda: False)

        def file_access_is_forbidden():
            raise AssertionError("cache or audit spool must not be accessed")

        monkeypatch.setattr(fleet_model_admission, "lkg_path", file_access_is_forbidden)
        monkeypatch.setattr(fleet_model_admission, "high_water_path", file_access_is_forbidden)
        monkeypatch.setattr(fleet_model_admission, "audit_spool_path", file_access_is_forbidden)
        online = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="13131313-1313-4313-8313-131313131313",
            phase="controller",
        )
        assert online.ok is True
        assert online.payload["source"] == "hub"

        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        offline = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="14141414-1414-4414-8414-141414141414",
            phase="controller",
        )
        assert offline.ok is False
        assert offline.reason == "cache-unsafe-platform"


def test_lkg_is_used_only_after_timeout_transport_or_http_5xx(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        cached = json.loads(fleet_model_admission.lkg_path().read_text(encoding="utf-8"))
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("deadline")),
        )
        timeout = fleet_model_admission.fetch_versioned_roster()
        assert timeout.ok is True
        assert timeout.payload["source"] == "lkg"
        assert timeout.payload["revision"] == roster["revision"]
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        transport = fleet_model_admission.fetch_versioned_roster()
        assert transport.ok is True
        assert transport.payload["source"] == "lkg"
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError("http://hub.invalid/models", 503, "down", {}, None)
            ),
        )
        server_error = fleet_model_admission.fetch_versioned_roster()
        assert server_error.ok is True
        assert server_error.payload["source"] == "lkg"
        assert json.loads(fleet_model_admission.lkg_path().read_text(encoding="utf-8")) == cached


@pytest.mark.parametrize(
    ("exc", "reason", "exit_code"),
    [
        (urllib.error.HTTPError("http://hub.invalid/models", 401, "no", {}, None), "auth-failed", 1),
        (urllib.error.HTTPError("http://hub.invalid/models", 403, "no", {}, None), "auth-failed", 1),
        (
            urllib.error.HTTPError("http://hub.invalid/models", 409, "conflict", {}, None),
            "revision-conflict",
            4,
        ),
    ],
)
def test_authoritative_auth_and_conflict_never_fall_back_to_lkg(tmp_path, monkeypatch, exc, reason, exit_code):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        before = fleet_model_admission.lkg_path().read_bytes()
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(exc),
        )
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == exit_code
        assert decision.reason == reason
        assert fleet_model_admission.lkg_path().read_bytes() == before
        _secret_free(decision, node_token, ADMIN_TOKEN)


def test_malformed_and_integrity_failures_never_rewrite_lkg(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        before = fleet_model_admission.lkg_path().read_bytes()

        def _return(payload):
            monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda *args, **kwargs: payload)

        _return("{not-json")
        malformed = fleet_model_admission.fetch_versioned_roster()
        assert malformed.ok is False
        assert malformed.exit_code in {1, 2}
        assert fleet_model_admission.lkg_path().read_bytes() == before

        bad = dict(roster)
        bad["document_sha256"] = "sha256:" + ("ab" * 32)
        _return(bad)
        digest = fleet_model_admission.fetch_versioned_roster()
        assert digest.ok is False
        assert digest.reason == "lkg-mac-invalid" or digest.reason == "digest-mismatch"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        bad_mac = dict(roster)
        bad_mac = json.loads(json.dumps(roster))
        bad_mac["mac"] = {"algorithm": roster["mac"]["algorithm"], "value": "ab" * 32}
        _return(bad_mac)
        mac = fleet_model_admission.fetch_versioned_roster()
        assert mac.ok is False
        assert mac.reason == "lkg-mac-invalid"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        drifted = json.loads(json.dumps(roster))
        drifted["audience_node_id"] = "22222222-2222-4222-8222-222222222222"
        drifted["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, drifted),
        }
        _return(drifted)
        audience = fleet_model_admission.fetch_versioned_roster()
        assert audience.ok is False
        assert audience.reason == "audience-mismatch"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        _return(json.loads(json.dumps(roster)))
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "rotated-node-token")
        rotated = fleet_model_admission.fetch_versioned_roster()
        assert rotated.ok is False
        assert rotated.reason in {"lkg-mac-invalid", "token-rotated"}
        assert fleet_model_admission.lkg_path().read_bytes() == before
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", node_token)

        future = json.loads(json.dumps(roster))
        future_issued = datetime.now(timezone.utc) + timedelta(seconds=fleet_model_roster.CLOCK_SKEW_SECONDS + 30)
        future["issued_at"] = future_issued.strftime("%Y-%m-%dT%H:%M:%SZ")
        future["expires_at"] = (future_issued + timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, future),
        }
        _return(future)
        skew = fleet_model_admission.fetch_versioned_roster()
        assert skew.ok is False
        assert skew.reason == "future-timestamp"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        expired = json.loads(json.dumps(roster))
        expired["issued_at"] = "2020-01-01T00:00:00Z"
        expired["expires_at"] = "2020-01-01T00:15:00Z"
        expired["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, expired),
        }
        _return(expired)
        expiry = fleet_model_admission.fetch_versioned_roster()
        assert expiry.ok is False
        assert expiry.reason == "lkg-expired"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        rollback = json.loads(json.dumps(roster))
        rollback["revision"] = int(roster["revision"]) - 1
        rollback["document_sha256"] = fleet_model_roster.roster_digest(rollback)
        rollback["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, rollback),
        }
        _return(rollback)
        rolled = fleet_model_admission.fetch_versioned_roster()
        assert rolled.ok is False
        assert rolled.reason == "revision-rollback"
        assert fleet_model_admission.lkg_path().read_bytes() == before
        _secret_free(
            [malformed, digest, mac, audience, rotated, skew, expiry, rolled],
            node_token,
            ADMIN_TOKEN,
            "rotated-node-token",
        )


def test_admit_hub_success_lkg_success_policy_denial_and_revision_conflict(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        admitted = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id=ADMIT_REQUEST_ID,
            phase="controller",
        )
        assert admitted.ok is True
        assert admitted.exit_code == 0
        assert admitted.payload == {
            "schema": "brigade.model_admission.v1",
            "state": "authoritative",
            "source": "hub",
            "roster_revision": roster["revision"],
            "roster_digest": roster["document_sha256"],
            "seat": "cursor_grok",
            "provider": "cursor",
            "model": "cursor-grok-4.6-high-fast",
            "reasoning": "high",
            "binding": {"instance_id": "cursor", "service_tier": "standard"},
            "expires_at": admitted.payload["expires_at"],
        }
        _secret_free(admitted, node_token, ADMIN_TOKEN, str(tmp_path), "prompt")
        assert not fleet_model_admission.audit_spool_path().exists()

        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        lkg = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="55555555-5555-4555-8555-555555555555",
            phase="controller",
        )
        assert lkg.ok is True
        assert lkg.exit_code == 0
        assert lkg.payload["source"] == "lkg"
        assert lkg.payload["schema"] == "brigade.model_admission.v1"
        spool = fleet_model_admission.audit_spool_path()
        assert spool.is_file()
        assert stat.S_IMODE(os.stat(spool).st_mode) == 0o600
        rows = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines() if line]
        assert len(rows) == 1
        assert rows[0]["source"] == "lkg"
        assert rows[0]["decision"] == "admitted"
        _secret_free(rows[0], node_token, ADMIN_TOKEN, str(tmp_path), "prompt")
        first_spool = spool.read_bytes()
        lkg_again = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="55555555-5555-4555-8555-555555555555",
            phase="controller",
        )
        assert lkg_again.payload == lkg.payload
        assert spool.read_bytes() == first_spool

        monkeypatch.undo()
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        denied = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="66666666-6666-4666-8666-666666666666",
            phase="controller",
            seat="missing-seat",
        )
        assert denied.ok is False
        assert denied.exit_code == 3
        assert denied.reason in {"seat-missing", "binding-missing"}
        _secret_free(denied, node_token, ADMIN_TOKEN, str(tmp_path))

        conflict = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="77777777-7777-4777-8777-777777777777",
            phase="controller",
            expect_revision=1,
        )
        assert conflict.ok is False
        assert conflict.exit_code == 4
        assert conflict.reason == "roster_revision_conflict"
        _secret_free(conflict, node_token, ADMIN_TOKEN)


def test_fleet_models_cli_admit_doctor_reconcile_retire_and_default(tmp_path, monkeypatch, capsys):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        home = _configure_client(monkeypatch, tmp_path, hub, node_token)
        (home / ".brigade").mkdir(parents=True, exist_ok=True)
        (home / ".brigade" / "roster.toml").write_text(
            'orchestrator = "chef"\n\n[agents.cursor_grok]\ncli = "cursor"\nrole = "code"\nmodel = "composer-2.5"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(home)
        admit_rc = cli.main(
            [
                "fleet",
                "models",
                "admit",
                "--consumer",
                "t3-fleet",
                "--request-id",
                ADMIT_REQUEST_ID,
                "--phase",
                "controller",
                "--json",
            ]
        )
        admit_out = capsys.readouterr()
        assert admit_rc == 0
        admitted = json.loads(admit_out.out)
        assert admitted["schema"] == "brigade.model_admission.v1"
        assert admitted["source"] == "hub"
        assert admitted["seat"] == "cursor_grok"
        assert json.dumps(admitted, indent=2, sort_keys=True) + "\n" == admit_out.out
        _secret_free(admitted, node_token, ADMIN_TOKEN, str(home))

        doctor_rc = cli.main(["fleet", "models", "doctor", "--consumer", "t3-fleet", "--json"])
        doctor_out = capsys.readouterr()
        assert doctor_rc == 0
        doctor = json.loads(doctor_out.out)
        assert doctor["consumer"] == "t3-fleet"
        assert doctor["hub"] == "reachable"
        assert doctor["roster_revision"] == roster["revision"]
        assert doctor["roster_digest"] == roster["document_sha256"]
        assert doctor["cache_valid"] is True
        assert doctor["consumer_default"] == "cursor_grok"
        assert doctor["provider"] == "cursor"
        assert doctor["model"] == "cursor-grok-4.6-high-fast"
        assert doctor["reasoning"] == "high"
        assert doctor["binding_present"] is True
        assert doctor["retired"] is False
        assert "local_roster_drift" in doctor
        assert "audit_spool" in doctor
        assert json.dumps(doctor, indent=2, sort_keys=True) + "\n" == doctor_out.out

        reconcile_rc = cli.main(["fleet", "models", "reconcile", "--consumer", "t3-fleet", "--json"])
        reconcile_out = capsys.readouterr()
        assert reconcile_rc == 0
        reconcile = json.loads(reconcile_out.out)
        assert reconcile["read_only"] is True
        assert "local_roster" in reconcile
        assert "project_default_model" in reconcile
        assert "project_default_drift" in reconcile
        assert "findings" in reconcile
        assert json.dumps(reconcile, indent=2, sort_keys=True) + "\n" == reconcile_out.out
        status, after = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        assert after["revision"] == roster["revision"]

        retire_rc = cli.main(
            [
                "fleet",
                "models",
                "retire",
                "openai",
                "gpt-5.4",
                "--permanent",
                "--expect-revision",
                str(roster["revision"]),
            ]
        )
        assert retire_rc == 3
        default_rc = cli.main(
            [
                "fleet",
                "models",
                "default",
                "set",
                "t3-fleet",
                "cursor_grok",
                "--expect-revision",
                str(roster["revision"]),
            ]
        )
        assert default_rc == 0
        stale_default = cli.main(
            [
                "fleet",
                "models",
                "default",
                "set",
                "t3-fleet",
                "cursor_grok",
                "--expect-revision",
                str(roster["revision"]),
            ]
        )
        assert stale_default == 4

        monkeypatch.setattr(
            fleet_model_admission,
            "admit_model",
            lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(
                False, 1, "hub-unavailable", {"reason": "hub-unavailable"}
            ),
        )
        assert (
            cli.main(
                [
                    "fleet",
                    "models",
                    "admit",
                    "--consumer",
                    "t3-fleet",
                    "--request-id",
                    "88888888-8888-4888-8888-888888888888",
                    "--phase",
                    "controller",
                    "--json",
                ]
            )
            == 1
        )
        monkeypatch.setattr(
            fleet_model_admission,
            "admit_model",
            lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(
                False, 2, "unsupported-schema", {"reason": "unsupported-schema"}
            ),
        )
        assert (
            cli.main(
                [
                    "fleet",
                    "models",
                    "admit",
                    "--consumer",
                    "t3-fleet",
                    "--request-id",
                    "88888888-8888-4888-8888-888888888888",
                    "--phase",
                    "not-a-phase",
                    "--json",
                ]
            )
            == 2
        )
        monkeypatch.setattr(
            fleet_model_admission,
            "admit_model",
            lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(
                False, 3, "retired-model", {"reason": "retired-model"}
            ),
        )
        assert (
            cli.main(
                [
                    "fleet",
                    "models",
                    "admit",
                    "--consumer",
                    "t3-fleet",
                    "--request-id",
                    "88888888-8888-4888-8888-888888888888",
                    "--phase",
                    "controller",
                    "--json",
                ]
            )
            == 3
        )


def test_models_set_fetches_admin_revision_and_sends_reasoning_bindings(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        policy = fleet_client.set_model_policy(
            "cursor",
            "cursor-grok-4.6-high-fast",
            "cursor_grok",
            enabled=True,
            limit=8,
            notes="primary",
            expected_revision=1,
        )
        assert policy["seat"] == "cursor_grok"
        assert policy["provider"] == "cursor"
        assert policy["model"] == "cursor-grok-4.6-high-fast"
        status, roster = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        seat = next(item for item in roster["seats"] if item["seat"] == "cursor_grok")
        assert seat["reasoning"] == "none"
        assert seat["bindings"]["brigade"]["cli"] == ""
        assert seat["bindings"]["t3_fleet"]["instance_id"] == ""


def test_model_roster_read_cap_is_one_mib_without_raising_cloud_cap(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    assert fleet_client.MAX_CLOUD_RESPONSE_BYTES == 64 * 1024
    assert fleet_model_admission.MODEL_ROSTER_MAX_BYTES == 1024 * 1024
    padding = "x" * (fleet_client.MAX_CLOUD_RESPONSE_BYTES + 1)
    body = json.dumps({"schema": "brigade.fleet_model_roster.v1", "padding": padding}).encode("utf-8")
    assert len(body) > fleet_client.MAX_CLOUD_RESPONSE_BYTES
    assert len(body) < fleet_model_admission.MODEL_ROSTER_MAX_BYTES

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            return body[:size] if size != -1 else body

    monkeypatch.setattr(fleet_client, "_hub_open", lambda request, timeout: _Response())
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": "http://127.0.0.1:9", "token": "node-token"},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda _base=None: NODE_A)
    decision = fleet_model_admission.fetch_versioned_roster(allow_lkg=False)
    assert "exceeded the size limit" not in decision.reason
    with pytest.raises(fleet_client.FleetClientError, match="size limit"):
        fleet_client.fetch_cloud()


def _admission_success_body(**overrides: object) -> dict[str, object]:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, object] = {
        "schema": fleet_model_roster.ADMISSION_SCHEMA,
        "state": "authoritative",
        "source": "hub",
        "roster_revision": 2,
        "roster_digest": "sha256:" + ("ab" * 32),
        "seat": "cursor_grok",
        "provider": "cursor",
        "model": "cursor-grok-4.6-high-fast",
        "reasoning": "high",
        "binding": {"instance_id": "cursor", "service_tier": "standard"},
        "expires_at": expires_at,
    }
    body.update(overrides)
    return body


def test_target_admission_rejects_fresh_roster_drift_before_hub_replay(monkeypatch):
    from brigade import fleet_model_admission

    current_digest = "sha256:" + ("cd" * 32)
    fetch_calls: list[dict[str, object]] = []

    def fetch_roster(**kwargs):
        fetch_calls.append(kwargs)
        return fleet_model_admission.ModelAdmissionDecision(
            True,
            0,
            "hub",
            _versioned_snapshot(
                _versioned_seat(
                    "cursor_grok",
                    "cursor",
                    "cursor-grok-4.6-high-fast",
                ),
                revision=3,
                digest=current_digest,
            ),
        )

    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        fetch_roster,
    )
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": "https://hub.invalid", "token": "node-token"},
    )
    post_calls: list[dict[str, object]] = []

    def replay_prior_admission(
        _hub: str,
        _token: str,
        body: dict[str, object],
        *,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        del timeout
        post_calls.append(body)
        return 200, _admission_success_body(expires_at="2099-08-30T14:15:00Z")

    monkeypatch.setattr(fleet_client, "_post_model_policy_blocking", replay_prior_admission)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda function, **_kwargs: function())

    decision = fleet_model_admission.admit_model(
        consumer="t3-fleet",
        request_id=ADMIT_REQUEST_ID,
        phase="target",
        seat="cursor_grok",
        expect_revision=2,
        expect_digest="sha256:" + ("ab" * 32),
    )

    assert decision.ok is False
    assert decision.exit_code == 4
    assert decision.reason == "roster_revision_conflict"
    assert decision.payload["roster_revision"] == 3
    assert decision.payload["roster_digest"] == current_digest
    assert fetch_calls == [{"allow_lkg": False}]
    assert post_calls == []


def test_validate_envelope_rejects_expires_at_not_after_issued_at(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        equal = json.loads(json.dumps(roster))
        issued = datetime.now(timezone.utc).replace(microsecond=0)
        stamp = issued.strftime("%Y-%m-%dT%H:%M:%SZ")
        equal["issued_at"] = stamp
        equal["expires_at"] = stamp
        equal["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, equal),
        }
        assert fleet_model_admission._validate_envelope(equal, token=node_token, audience=NODE_A) == "malformed-roster"

        inverted = json.loads(json.dumps(roster))
        inverted["issued_at"] = (issued + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        inverted["expires_at"] = issued.strftime("%Y-%m-%dT%H:%M:%SZ")
        inverted["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, inverted),
        }
        assert (
            fleet_model_admission._validate_envelope(inverted, token=node_token, audience=NODE_A) == "malformed-roster"
        )


def test_lkg_rejects_cached_at_beyond_allowed_future_skew(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        lkg = fleet_model_admission.lkg_path()
        record = json.loads(lkg.read_text(encoding="utf-8"))
        future = datetime.now(timezone.utc) + timedelta(seconds=fleet_model_roster.CLOCK_SKEW_SECONDS + 30)
        record["cached_at"] = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        lkg.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.chmod(lkg, 0o600)
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("deadline")),
        )
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.reason in {"future-timestamp", "malformed-roster"}


@pytest.mark.parametrize("highest_revision", ["not-an-integer", ["not-an-integer"]])
def test_lkg_rejects_malformed_highest_revision_without_raising(tmp_path, monkeypatch, highest_revision):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        lkg = fleet_model_admission.lkg_path()
        record = json.loads(lkg.read_text(encoding="utf-8"))
        record["highest_revision"] = highest_revision
        lkg.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.chmod(lkg, 0o600)
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("deadline")),
        )

        decision = fleet_model_admission.fetch_versioned_roster()

        assert decision.ok is False
        assert decision.exit_code == 1
        assert decision.reason == "lkg-unsafe"


def test_cli_t3_fleet_admit_fails_closed_when_audit_spool_read_raises_oserror(tmp_path, monkeypatch, capsys):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        monkeypatch.setattr(
            fleet_model_admission,
            "_read_audit_spool",
            lambda: (_ for _ in ()).throw(OSError("spool lock unavailable")),
        )

        result = cli.main(
            [
                "fleet",
                "models",
                "admit",
                "--consumer",
                "t3-fleet",
                "--request-id",
                "abababab-abab-4bab-8bab-abababababab",
                "--phase",
                "controller",
                "--json",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert result == 1
        assert payload["reason"] == "lkg-unsafe"
        assert payload["error"] == "lkg-unsafe"


@pytest.mark.parametrize("mutation", ["extra-field", "model", "expired"])
def test_cli_t3_fleet_replay_rejects_tampered_admission_payload(tmp_path, monkeypatch, capsys, mutation):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        request_id = "bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc"
        assert (
            fleet_model_admission.admit_model(
                consumer="t3-fleet",
                request_id=request_id,
                phase="controller",
            ).ok
            is True
        )
        spool = fleet_model_admission.audit_spool_path()
        row = json.loads(spool.read_text(encoding="utf-8"))
        admission = row["admission"]
        if mutation == "extra-field":
            admission["untrusted"] = "forged"
        elif mutation == "model":
            admission["model"] = "forged-model"
        else:
            admission["expires_at"] = "2020-01-01T00:00:00Z"
        spool.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(spool, 0o600)

        result = cli.main(
            [
                "fleet",
                "models",
                "admit",
                "--consumer",
                "t3-fleet",
                "--request-id",
                request_id,
                "--phase",
                "controller",
                "--json",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert result == 1
        assert payload["reason"] == "lkg-unsafe"
        assert payload["error"] == "lkg-unsafe"


def test_audit_replay_rejects_created_at_beyond_allowed_future_skew(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        request_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        first = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id=request_id,
            phase="controller",
        )
        assert first.ok is True
        spool = fleet_model_admission.audit_spool_path()
        rows = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines() if line]
        future = datetime.now(timezone.utc) + timedelta(seconds=fleet_model_roster.CLOCK_SKEW_SECONDS + 45)
        rows[-1]["created_at"] = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        spool.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        os.chmod(spool, 0o600)
        replay = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id=request_id,
            phase="controller",
        )
        assert replay.ok is False
        assert replay.reason in {"future-timestamp", "malformed-roster", "lkg-unsafe"}


def test_map_hub_admission_type_checks_exact_success_fields_before_ok():
    from brigade import fleet_model_admission

    for override in (
        {"seat": ""},
        {"provider": ""},
        {"model": ""},
        {"reasoning": ""},
        {"roster_revision": "2"},
        {"roster_digest": "sha256:not-a-digest"},
        {"roster_digest": "md5:" + ("ab" * 16)},
        {"expires_at": "not-a-timestamp"},
        {"binding": {"service_tier": "standard"}},
        {"binding": {"instance_id": ""}},
    ):
        body = _admission_success_body(**override)
        decision = fleet_model_admission._map_hub_admission(200, body)
        assert decision.ok is False, override
        assert decision.exit_code == 2
        assert decision.reason == "unsupported-schema"
        _secret_free(decision, "token")

    ok = fleet_model_admission._map_hub_admission(200, _admission_success_body())
    assert ok.ok is True
    assert set(ok.payload) == set(ADMISSION_PROVENANCE_KEYS) | {"state"}
    assert "raw" not in ok.payload
    assert "models" not in ok.payload


def test_map_hub_admission_rejects_non_authoritative_or_extra_fields_and_applies_retired_floor():
    from brigade import fleet_model_admission

    extra = _admission_success_body(token="secret-token")
    extra_decision = fleet_model_admission._map_hub_admission(200, extra)
    assert extra_decision.ok is False
    assert extra_decision.exit_code == 2
    assert extra_decision.reason == "unsupported-schema"
    _secret_free(extra_decision, "secret-token")

    wrong_schema = _admission_success_body(schema="brigade.model_admission.v0")
    schema_decision = fleet_model_admission._map_hub_admission(200, wrong_schema)
    assert schema_decision.ok is False
    assert schema_decision.exit_code == 2
    assert schema_decision.reason == "unsupported-schema"

    stale_state = _admission_success_body(state="denied")
    state_decision = fleet_model_admission._map_hub_admission(200, stale_state)
    assert state_decision.ok is False
    assert state_decision.exit_code == 2

    with_error = _admission_success_body()
    with_error["error"] = "retired-model"
    error_decision = fleet_model_admission._map_hub_admission(200, with_error)
    assert error_decision.ok is False
    assert error_decision.exit_code == 2

    retired = _admission_success_body(provider="codex", model="gpt-5.5", seat="coder")
    retired_decision = fleet_model_admission._map_hub_admission(200, retired)
    assert retired_decision.ok is False
    assert retired_decision.exit_code == 3
    assert retired_decision.reason == "retired-model"

    ok = fleet_model_admission._map_hub_admission(200, _admission_success_body())
    assert ok.ok is True
    assert ok.exit_code == 0
    assert set(ok.payload) == {
        "schema",
        "state",
        "source",
        "roster_revision",
        "roster_digest",
        "seat",
        "provider",
        "model",
        "reasoning",
        "binding",
        "expires_at",
    }


def test_reconcile_authority_failure_is_nonzero_and_doctor_does_not_write_cache(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert not fleet_model_admission.lkg_path().exists()
        assert not fleet_model_admission.high_water_path().exists()
        doctor = fleet_model_admission.doctor_model_roster(consumer="t3-fleet")
        assert doctor.ok is True
        assert not fleet_model_admission.lkg_path().exists()
        assert not fleet_model_admission.high_water_path().exists()
        assert doctor.payload["cache_valid"] is False

        seeded = fleet_model_admission.fetch_versioned_roster()
        assert seeded.ok is True
        lkg_before = fleet_model_admission.lkg_path().read_bytes()
        high_before = fleet_model_admission.high_water_path().read_bytes()
        doctor_again = fleet_model_admission.doctor_model_roster(consumer="t3-fleet")
        assert doctor_again.ok is True
        assert doctor_again.payload["cache_valid"] is True
        assert fleet_model_admission.lkg_path().read_bytes() == lkg_before
        assert fleet_model_admission.high_water_path().read_bytes() == high_before

        reconcile = fleet_model_admission.reconcile_model_roster(consumer="t3-fleet")
        assert reconcile.ok is True
        assert fleet_model_admission.lkg_path().read_bytes() == lkg_before
        assert fleet_model_admission.high_water_path().read_bytes() == high_before

        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError("http://hub.invalid/models", 401, "no", {}, None)
            ),
        )
        failed = fleet_model_admission.reconcile_model_roster(consumer="t3-fleet")
        assert failed.ok is False
        assert failed.exit_code == 1
        assert failed.reason == "auth-failed"
        assert failed.payload.get("findings") in (None, [])
        assert fleet_model_admission.lkg_path().read_bytes() == lkg_before


def test_doctor_cache_valid_revalidates_mac_not_existence(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        lkg = fleet_model_admission.lkg_path()
        record = json.loads(lkg.read_text(encoding="utf-8"))
        record["roster"]["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": "ab" * 32,
        }
        lkg.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.chmod(lkg, 0o600)
        doctor = fleet_model_admission.doctor_model_roster(consumer="t3-fleet")
        assert doctor.ok is True
        assert doctor.payload["cache_valid"] is False


def test_malformed_signed_rows_are_malformed_roster_without_mutation(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        before = fleet_model_admission.lkg_path().read_bytes()
        bad = json.loads(json.dumps(roster))
        bad["seats"] = [{"seat": "cursor_grok", "provider": "cursor"}]
        bad["document_sha256"] = fleet_model_roster.roster_digest(bad)
        bad["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, bad),
        }
        monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda *args, **kwargs: bad)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.reason == "malformed-roster"
        assert decision.exit_code == 1
        assert fleet_model_admission.lkg_path().read_bytes() == before


def test_lkg_enforces_independent_ttl_windows(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        before = fleet_model_admission.lkg_path().read_bytes()

        wide = json.loads(json.dumps(roster))
        issued = datetime.now(timezone.utc).replace(microsecond=0)
        wide["issued_at"] = issued.strftime("%Y-%m-%dT%H:%M:%SZ")
        wide["expires_at"] = (issued + timedelta(seconds=901)).strftime("%Y-%m-%dT%H:%M:%SZ")
        wide["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, wide),
        }
        monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda *args, **kwargs: wide)
        window = fleet_model_admission.fetch_versioned_roster(allow_lkg=False)
        assert window.ok is False
        assert window.reason in {"lkg-expired", "malformed-roster"}
        assert fleet_model_admission.lkg_path().read_bytes() == before

        record = json.loads(before.decode("ascii"))
        record["cached_at"] = (issued - timedelta(seconds=901)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fleet_model_admission.lkg_path().write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.chmod(fleet_model_admission.lkg_path(), 0o600)
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("deadline")),
        )
        aged = fleet_model_admission.fetch_versioned_roster()
        assert aged.ok is False
        assert aged.reason == "lkg-expired"


def _lkg_body(raw: bytes) -> dict:
    """LKG record without the cache timestamp, which refreshes on every write."""
    record = json.loads(raw.decode("ascii"))
    record.pop("cached_at", None)
    return record


def test_admit_malformed_and_client_errors_never_use_lkg(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        before = fleet_model_admission.lkg_path().read_bytes()

        def _deadline(fn, *, timeout):  # noqa: ARG001
            return fn()

        monkeypatch.setattr(fleet_client, "_run_with_deadline", _deadline)
        monkeypatch.setattr(
            fleet_client,
            "_get_models_blocking",
            lambda *args, **kwargs: json.loads(before.decode("ascii"))["roster"],
        )
        monkeypatch.setattr(
            fleet_client,
            "_post_model_policy_blocking",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                fleet_client.FleetClientError("fleet hub cloud response exceeded the size limit")
            ),
        )
        oversized = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="99999999-9999-4999-8999-999999999999",
            phase="controller",
        )
        assert oversized.ok is False
        assert oversized.exit_code == 1
        assert oversized.payload.get("source") != "lkg"
        # admit_model re-fetches the roster, so the cache is legitimately
        # rewritten with a fresh second-granularity ``cached_at``. Compare the
        # cached content instead of raw bytes.
        assert _lkg_body(fleet_model_admission.lkg_path().read_bytes()) == _lkg_body(before)

        monkeypatch.setattr(
            fleet_client,
            "_post_model_policy_blocking",
            lambda *args, **kwargs: (200, {"schema": "other", "token": "secret-token"}),
        )
        schema = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            phase="controller",
        )
        assert schema.ok is False
        assert schema.exit_code == 2
        assert schema.reason == "unsupported-schema"
        assert schema.payload.get("source") != "lkg"
        _secret_free(schema, "secret-token")


def test_admit_validates_request_id_and_seat_before_hub(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        calls: list[str] = []

        def _deadline(fn, *, timeout):  # noqa: ARG001
            calls.append("hub")
            return fn()

        monkeypatch.setattr(fleet_client, "_run_with_deadline", _deadline)
        bad_id = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="not a valid id",
            phase="controller",
        )
        assert bad_id.ok is False
        assert bad_id.exit_code == 2
        assert bad_id.reason == "unsupported-schema"
        assert calls == []

        bad_seat = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id=ADMIT_REQUEST_ID,
            phase="controller",
            seat="BadSeat",
        )
        assert bad_seat.ok is False
        assert bad_seat.exit_code == 2
        assert bad_seat.reason == "unsupported-schema"
        assert calls == []


def test_reconcile_missing_default_and_enabled_seats_are_instance_missing(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        assert (
            _admin_set(
                hub,
                seat="cursor_composer",
                model="composer-2.5",
                brigade_cli="cursor-agent",
                t3_instance_id="cursor",
            )[0]
            == 200
        )
        home = _configure_client(monkeypatch, tmp_path, hub, node_token)
        (home / ".brigade").mkdir(parents=True, exist_ok=True)
        (home / ".brigade" / "roster.toml").write_text(
            'orchestrator = "chef"\n\n[agents.chef]\ncli = "claude"\nrole = "plan"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(home)
        decision = fleet_model_admission.reconcile_model_roster(consumer="t3-fleet")
        assert decision.ok is True
        codes = {item["code"] for item in decision.payload["findings"]}
        seats = {item.get("seat") for item in decision.payload["findings"]}
        assert "instance-missing" in codes
        assert "cursor_grok" in seats
        assert "cursor_composer" in seats


def test_lkg_cache_omits_legacy_models_projection(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        record = json.loads(fleet_model_admission.lkg_path().read_text(encoding="utf-8"))
        assert "models" not in record["roster"]
        assert set(record["roster"]) <= set(fleet_model_roster.CACHE_ENVELOPE_KEYS) | {"mac"}


def test_audit_spool_bounds_replay_and_fails_closed_on_corrupt(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        admitted = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            phase="controller",
        )
        assert admitted.ok is True
        spool = fleet_model_admission.audit_spool_path()
        rows = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines() if line]
        assert "created_at" in rows[-1]
        assert rows[-1].get("node_id") == NODE_A
        _secret_free(rows[-1], node_token, ADMIN_TOKEN, str(tmp_path), "prompt")

        stale = {
            "source": "lkg",
            "request_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "phase": "controller",
            "consumer": "t3-fleet",
            "request_digest": "ab" * 32,
            "decision": "admitted",
            "admission": {},
            "created_at": "2020-01-01T00:00:00Z",
            "node_id": NODE_A,
        }
        current = json.loads(spool.read_text(encoding="utf-8").splitlines()[-1])
        spool.write_text(
            json.dumps(stale, sort_keys=True) + "\n" + json.dumps(current, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(spool, 0o600)
        replay = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            phase="controller",
        )
        assert replay.payload == admitted.payload
        extra = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
            phase="controller",
        )
        assert extra.ok is True
        kept = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines() if line]
        assert all(row.get("request_id") != stale["request_id"] for row in kept)

        spool.write_text("{not-json\n", encoding="utf-8")
        os.chmod(spool, 0o600)
        corrupt = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            phase="controller",
        )
        assert corrupt.ok is False
        assert corrupt.reason in {"lkg-unsafe", "malformed-roster"}


def test_admin_mutation_constructs_safe_output_and_maps_get_style_409(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        decision = fleet_model_admission.set_consumer_default(
            "t3-fleet", "cursor_grok", expected_revision=int(roster["revision"])
        )
        assert decision.ok is True
        assert decision.exit_code == 0
        assert set(decision.payload) <= {"updated", "revision", "consumer", "seat", "policy", "retired"}
        _secret_free(decision, node_token, ADMIN_TOKEN, str(tmp_path))

        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (409, {"error": "roster_revision_conflict", "token": "secret"}),
        )
        conflict = fleet_model_admission.set_consumer_default("t3-fleet", "cursor_grok", expected_revision=1)
        assert conflict.ok is False
        assert conflict.exit_code == 4
        _secret_free(conflict, "secret")


def test_configured_admin_token_roster_read_fails_closed_without_flattening(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN")
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.reason not in {"hub-unavailable", "unavailable"}
        assert decision.reason in {"node-token-required", "admin-token-not-cacheable"}
        snapshot = fleet_client.load_model_policy_snapshot()
        assert snapshot["state"] not in {"unavailable", "authoritative", "unconfigured"}
        assert snapshot["state"] == decision.reason
        assert not fleet_model_admission.lkg_path().exists()
        _secret_free(decision, node_token, ADMIN_TOKEN)


def test_old_hub_roster_read_fails_closed_with_unsupported_schema(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: {"models": [{"seat": "coder", "provider": "openai", "model": "gpt-5.6"}]},
        )
        decision = fleet_model_admission.fetch_versioned_roster(allow_lkg=False)
        assert decision.ok is False
        assert decision.reason == "unsupported-schema"
        assert decision.reason != "hub-unavailable"
        snapshot = fleet_client.load_model_policy_snapshot()
        assert snapshot["state"] == "unsupported-schema"
        _secret_free(decision, node_token, ADMIN_TOKEN)


def test_models_set_exposes_exact_fields_and_can_seed_enabled_seat(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        policy = fleet_client.set_model_policy(
            "cursor",
            "cursor-grok-4.6-high-fast",
            "cursor_grok",
            enabled=True,
            reasoning="high",
            brigade_cli="cursor-agent",
            t3_instance_id="cursor",
            t3_service_tier="standard",
            expected_revision=1,
        )
        for key in ("reasoning", "brigade_cli", "t3_instance_id", "t3_service_tier"):
            assert key in policy, key
        assert policy["reasoning"] == "high"
        assert policy["brigade_cli"] == "cursor-agent"
        assert policy["t3_instance_id"] == "cursor"
        assert policy["t3_service_tier"] == "standard"
        status, roster = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        seat = next(item for item in roster["seats"] if item["seat"] == "cursor_grok")
        assert seat["reasoning"] == "high"
        assert seat["bindings"]["brigade"]["cli"] == "cursor-agent"
        assert seat["enabled"] is True
        _secret_free(policy, node_token, ADMIN_TOKEN)


def test_models_set_limit_note_update_preserves_exact_fields_and_expected_revision(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        updated = fleet_client.set_model_policy(
            "cursor",
            "cursor-grok-4.6-high-fast",
            "cursor_grok",
            enabled=True,
            limit=4,
            notes="capacity",
            expected_revision=int(roster["revision"]),
        )
        assert updated["reasoning"] == "high"
        assert updated["brigade_cli"] == "cursor-agent"
        assert updated["t3_instance_id"] == "cursor"
        assert updated["t3_service_tier"] == "standard"
        status, after = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        seat = next(item for item in after["seats"] if item["seat"] == "cursor_grok")
        assert seat["reasoning"] == "high"
        assert seat["bindings"]["brigade"]["cli"] == "cursor-agent"
        assert seat["bindings"]["t3_fleet"]["instance_id"] == "cursor"
        stale = pytest.raises(fleet_client.FleetClientError)
        with stale:
            fleet_client.set_model_policy(
                "cursor",
                "cursor-grok-4.6-high-fast",
                "cursor_grok",
                enabled=True,
                limit=5,
                expected_revision=int(roster["revision"]),
            )
        _secret_free(updated, node_token, ADMIN_TOKEN)


def test_versioned_run_rejects_custom_command_and_cross_harness_binding_drift():
    custom = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "cursor_grok": Agent(
                "cursor_grok",
                "cursor",
                "code",
                model="cursor-grok-4.6-high-fast",
                command=("custom-cursor", "--flag"),
                env={"CURSOR_API_KEY": "local-secret"},
            ),
        },
    )
    snapshot = _versioned_snapshot(_versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"))
    denied = aboyeur.resolve_fleet_model_policy(custom, worker="cursor_grok", snapshot=snapshot)
    assert denied.error is not None
    assert "cursor_grok" in denied.error
    agent = denied.roster.agents.get("cursor_grok")
    if agent is not None:
        assert agent.command is None or denied.error

    drifted = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "cursor_grok": Agent(
                "cursor_grok",
                "codex",
                "code",
                model="cursor-grok-4.6-high-fast",
                env={"OPENAI_API_KEY": "keep-if-wrong"},
            ),
        },
    )
    drifted_resolution = aboyeur.resolve_fleet_model_policy(drifted, worker="cursor_grok", snapshot=snapshot)
    assert drifted_resolution.error is not None
    kept = drifted_resolution.roster.agents.get("cursor_grok")
    if kept is not None:
        assert kept.env != {"OPENAI_API_KEY": "keep-if-wrong"} or drifted_resolution.error

    matched = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "cursor_grok": Agent(
                "cursor_grok",
                "cursor",
                "code",
                model="cursor-grok-4.6-high-fast",
                env={"CURSOR_API_KEY": "keep-me"},
            ),
        },
    )
    allowed = aboyeur.resolve_fleet_model_policy(matched, worker="cursor_grok", snapshot=snapshot)
    assert allowed.error is None
    assert allowed.roster.agents["cursor_grok"].env == {"CURSOR_API_KEY": "keep-me"}
    assert allowed.roster.agents["cursor_grok"].command is None


def test_versioned_model_override_requires_exact_hub_match_and_never_discards():
    snapshot = _versioned_snapshot(_versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"))
    mismatch = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        model_override="composer-2.5",
        snapshot=snapshot,
    )
    assert mismatch.error is not None
    assert "composer-2.5" in mismatch.error or "cursor-grok-4.6-high-fast" in mismatch.error
    agent = mismatch.roster.agents.get("cursor_grok")
    if agent is not None:
        assert agent.model != "composer-2.5"

    matched = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        model_override="cursor-grok-4.6-high-fast",
        snapshot=snapshot,
    )
    assert matched.error is None
    assert matched.roster.agents["cursor_grok"].model == "cursor-grok-4.6-high-fast"
    assert matched.receipt["model_override"] == "cursor-grok-4.6-high-fast"


def test_versioned_model_override_denial_includes_set_command_and_list_hint():
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast", instance_id="cursor-agent"),
        revision=11,
    )
    mismatch = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        model_override="composer-2.5",
        snapshot=snapshot,
    )
    assert mismatch.error is not None
    assert "--model 'composer-2.5' does not match Hub model 'cursor-grok-4.6-high-fast'" in mismatch.error
    assert "brigade fleet models set cursor cursor-grok-4.6-high-fast cursor_grok --enable" in mismatch.error
    assert "--reasoning high" in mismatch.error
    assert "--brigade-cli cursor-agent" in mismatch.error
    assert "--expect-revision 11" in mismatch.error
    assert "brigade fleet models list --seat cursor_grok" in mismatch.error


def test_versioned_binding_missing_denial_includes_set_command_and_list_hint():
    seat = _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast", instance_id="cursor-agent")
    del seat["bindings"]["brigade"]
    snapshot = _versioned_snapshot(seat, revision=11)
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        snapshot=snapshot,
    )
    assert resolution.error is not None
    assert "registry entry is missing consumer binding" in resolution.error
    assert "brigade fleet models set cursor cursor-grok-4.6-high-fast cursor_grok --enable" in resolution.error
    assert "--brigade-cli" in resolution.error
    assert "--expect-revision 11" in resolution.error
    assert "brigade fleet models list --seat cursor_grok" in resolution.error


def test_resolve_from_roster_t3_binding_missing_suggests_t3_instance_id_in_remediation():
    from brigade import fleet_model_admission

    seat = _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast", instance_id="cursor-agent")
    seat["bindings"] = {"brigade": {"cli": "cursor-agent"}}
    roster = {
        "revision": 11,
        "document_sha256": "sha256:" + ("ab" * 32),
        "expires_at": "2026-08-30T14:15:00Z",
        "seats": [seat],
        "consumer_defaults": {"t3-fleet": "cursor_grok"},
        "retired_models": [],
    }
    decision = fleet_model_admission._resolve_from_roster(roster, consumer="t3-fleet", seat=None, source="hub")
    assert not decision.ok
    assert decision.reason == "binding-missing"
    remediation = decision.payload.get("remediation")
    assert isinstance(remediation, str)
    assert "brigade fleet models set cursor cursor-grok-4.6-high-fast cursor_grok --enable" in remediation
    assert "--t3-instance-id cursor-agent" in remediation
    assert "--brigade-cli" not in remediation
    assert "--expect-revision 11" in remediation
    assert "brigade fleet models list --seat cursor_grok" in remediation


def test_resolve_from_roster_brigade_run_binding_missing_suggests_brigade_cli_in_remediation():
    from brigade import fleet_model_admission

    seat = _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast", instance_id="cursor-agent")
    seat["bindings"] = {"t3_fleet": {"instance_id": "cursor-agent", "service_tier": "standard"}}
    roster = {
        "revision": 11,
        "document_sha256": "sha256:" + ("ab" * 32),
        "expires_at": "2026-08-30T14:15:00Z",
        "seats": [seat],
        "consumer_defaults": {"brigade-run": "cursor_grok"},
        "retired_models": [],
    }
    decision = fleet_model_admission._resolve_from_roster(roster, consumer="brigade-run", seat=None, source="hub")
    assert not decision.ok
    assert decision.reason == "binding-missing"
    remediation = decision.payload.get("remediation")
    assert isinstance(remediation, str)
    assert "brigade fleet models set cursor cursor-grok-4.6-high-fast cursor_grok --enable" in remediation
    assert "--brigade-cli cursor-agent" in remediation
    assert "--t3-instance-id" not in remediation
    assert "--expect-revision 11" in remediation
    assert "brigade fleet models list --seat cursor_grok" in remediation


def test_resolve_from_roster_t3_binding_missing_includes_service_tier_when_present():
    from brigade import fleet_model_admission

    seat = _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast", instance_id="cursor-agent")
    seat["bindings"] = {
        "brigade": {"cli": "cursor-agent"},
        "t3_fleet": {"instance_id": "", "service_tier": "premium"},
    }
    roster = {
        "revision": 11,
        "document_sha256": "sha256:" + ("ab" * 32),
        "expires_at": "2026-08-30T14:15:00Z",
        "seats": [seat],
        "consumer_defaults": {"t3-fleet": "cursor_grok"},
        "retired_models": [],
    }
    decision = fleet_model_admission._resolve_from_roster(roster, consumer="t3-fleet", seat=None, source="hub")
    assert not decision.ok
    assert decision.reason == "binding-missing"
    remediation = decision.payload.get("remediation")
    assert isinstance(remediation, str)
    assert "--t3-instance-id cursor-agent" in remediation
    assert "--t3-service-tier premium" in remediation
    assert "--brigade-cli" not in remediation


def test_validate_roster_rows_requires_nonempty_reasoning_and_hub_lkg_schema_match():
    from brigade import fleet_model_admission

    missing = _versioned_snapshot(_versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"))
    missing["seats"][0]["reasoning"] = ""
    assert fleet_model_roster.validate_roster_rows(missing) == "malformed-roster"
    absent = _versioned_snapshot(_versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"))
    del absent["seats"][0]["reasoning"]
    assert fleet_model_roster.validate_roster_rows(absent) == "malformed-roster"

    hub_ok = fleet_model_admission._map_hub_admission(200, _admission_success_body())
    lkg_ok = fleet_model_admission._resolve_from_roster(
        {
            "revision": 2,
            "roster_digest": "sha256:" + ("ab" * 32),
            "expires_at": "2026-08-30T14:15:00Z",
            "seats": [_versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")],
            "consumer_defaults": {"t3-fleet": "cursor_grok"},
            "retired_models": [],
        },
        consumer="t3-fleet",
        seat=None,
        source="lkg",
    )
    assert hub_ok.ok is True and lkg_ok.ok is True
    assert set(hub_ok.payload) == set(lkg_ok.payload)


def test_versioned_run_enforces_roster_declared_retirements_before_dispatch(tmp_path, monkeypatch):
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
    )
    snapshot["retired_models"] = [
        {
            "provider": "cursor",
            "family": "cursor-grok-4.6",
            "match_kind": "family-prefix",
            "permanent": False,
            "reason_code": "operator-retired",
        }
    ]
    lease_calls: list[tuple[str, str, str]] = []
    provider_calls: list[str] = []
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda seat, provider, model: (
            lease_calls.append((seat, provider, model))
            or fleet_client.ModelLeaseDecision(True, "ok", "should-not-lease", "holder")
        ),
    )
    monkeypatch.setattr(
        agents,
        "run_agent",
        lambda *args, **kwargs: provider_calls.append("run_agent") or agents.AgentResult(text="no", ok=True),
    )
    output_dir = tmp_path / "run"
    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )
    run = json.loads((output_dir / "run.json").read_text())
    assert run["failure"]["kind"] == "fleet-model-policy"
    assert run["failure"]["phase"] == "preflight"
    assert lease_calls == []
    assert provider_calls == []


def test_doctor_and_reconcile_remediate_migrated_empty_bindings(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        assert (
            _admin_set(
                hub,
                reasoning="none",
                brigade_cli="",
                t3_instance_id="",
                t3_service_tier="",
            )[0]
            == 200
        )
        assert (
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={
                    "action": "set-default",
                    "consumer": "t3-fleet",
                    "seat": "cursor_grok",
                    "expected_revision": _current_revision(hub),
                },
            )[0]
            == 200
        )
        home = _configure_client(monkeypatch, tmp_path, hub, node_token)
        (home / ".brigade").mkdir(parents=True, exist_ok=True)
        (home / ".brigade" / "roster.toml").write_text(
            'orchestrator = "chef"\n\n[agents.cursor_grok]\ncli = "cursor"\nrole = "code"\n'
            'model = "cursor-grok-4.6-high-fast"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(home)
        doctor = fleet_model_admission.doctor_model_roster(consumer="t3-fleet")
        assert doctor.ok is True
        assert doctor.payload.get("binding_present") is False
        remediation = doctor.payload.get("remediation") or ""
        assert "fleet models set" in remediation
        assert "--brigade-cli" in remediation or "--t3-instance-id" in remediation
        reconcile = fleet_model_admission.reconcile_model_roster(consumer="t3-fleet")
        assert reconcile.ok is True
        codes = {item["code"] for item in reconcile.payload["findings"]}
        assert "empty-binding" in codes
        empty = next(item for item in reconcile.payload["findings"] if item["code"] == "empty-binding")
        assert "fleet models set" in str(empty.get("remediation") or empty)
        _secret_free([doctor, reconcile], node_token, ADMIN_TOKEN)


def test_admit_compares_success_revision_and_digest_to_caller_expectations(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        drifted = _admission_success_body(
            roster_revision=int(roster["revision"]) + 3,
            roster_digest="sha256:" + ("cd" * 32),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        monkeypatch.setattr(fleet_client, "_post_model_policy_blocking", lambda *a, **k: (200, drifted))
        revision = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            phase="controller",
            expect_revision=int(roster["revision"]),
        )
        assert revision.ok is False
        assert revision.exit_code == 4
        assert revision.reason == "roster_revision_conflict"
        digest = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            phase="controller",
            expect_digest=str(roster["document_sha256"]),
        )
        assert digest.ok is False
        assert digest.exit_code == 4
        assert digest.reason == "roster_digest_conflict"
        _secret_free([revision, digest], node_token, ADMIN_TOKEN)


def test_admit_json_failures_include_reason_and_error(tmp_path, monkeypatch, capsys):
    from brigade import fleet_model_admission

    rc = cli.main(
        [
            "fleet",
            "models",
            "admit",
            "--consumer",
            "not-a-consumer",
            "--request-id",
            ADMIT_REQUEST_ID,
            "--phase",
            "controller",
            "--json",
        ]
    )
    out = capsys.readouterr()
    assert rc == 2
    payload = json.loads(out.out)
    assert payload["reason"] == "unsupported-schema"
    assert payload["error"] == "unsupported-schema"

    monkeypatch.setattr(
        fleet_model_admission,
        "admit_model",
        lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(False, 1, "auth-failed", {}),
    )
    assert (
        cli.main(
            [
                "fleet",
                "models",
                "admit",
                "--consumer",
                "t3-fleet",
                "--request-id",
                ADMIT_REQUEST_ID,
                "--phase",
                "controller",
                "--json",
            ]
        )
        == 1
    )
    failed = json.loads(capsys.readouterr().out)
    assert failed["reason"] == "auth-failed"
    assert failed["error"] == "auth-failed"


def test_map_hub_admission_rejects_already_expired_success():
    from brigade import fleet_model_admission

    expired = _admission_success_body(expires_at="2020-01-01T00:00:00Z")
    decision = fleet_model_admission._map_hub_admission(200, expired)
    assert decision.ok is False
    assert decision.reason in {"admission-expired", "unsupported-schema", "lkg-expired"}
    assert decision.exit_code in {1, 2}


def test_models_set_requires_explicit_expected_revision_in_client_and_cli(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        with pytest.raises(TypeError, match="expected_revision"):
            fleet_client.set_model_policy(
                "cursor",
                "cursor-grok-4.6-high-fast",
                "cursor_grok",
                enabled=True,
            )

        with pytest.raises(SystemExit) as error:
            cli.main(
                [
                    "fleet",
                    "models",
                    "set",
                    "cursor",
                    "cursor-grok-4.6-high-fast",
                    "cursor_grok",
                    "--enable",
                ]
            )
        assert error.value.code == 2


def test_admin_token_can_inspect_but_cannot_cache_or_admit(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN")

        doctor = fleet_model_admission.doctor_model_roster(consumer="t3-fleet")
        reconcile = fleet_model_admission.reconcile_model_roster(consumer="t3-fleet")
        denied = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="12121212-1212-4212-8212-121212121212",
            phase="controller",
        )

        assert doctor.ok is True
        assert doctor.payload["hub"] == "reachable"
        assert doctor.payload["cache_valid"] is False
        assert reconcile.ok is True
        assert reconcile.payload["hub"] == "reachable"
        assert denied.ok is False
        assert denied.reason == "node-token-required"
        assert not fleet_model_admission.lkg_path().exists()
        assert not fleet_model_admission.high_water_path().exists()


def test_oversized_audit_spool_fails_closed_without_rewrite(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        spool = fleet_model_admission.audit_spool_path()
        spool.write_bytes(b"x" * (fleet_model_admission.MODEL_ROSTER_MAX_BYTES + 1))
        os.chmod(spool, 0o600)
        before = spool.read_bytes()
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )

        decision = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="15151515-1515-4515-8515-151515151515",
            phase="controller",
        )
        assert decision.ok is False
        assert decision.reason == "lkg-oversized"
        assert spool.read_bytes() == before


def test_hub_admission_denies_blank_reasoning_as_missing_binding(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        db = fleet_hub.open_db(hub[2])
        try:
            db.execute("UPDATE model_policy SET reasoning='' WHERE seat='cursor_grok'")
            db.commit()
        finally:
            db.close()

        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=node_token,
            body={
                "action": "admit",
                "schema": fleet_model_roster.ADMISSION_REQUEST_SCHEMA,
                "consumer": "t3-fleet",
                "seat": "cursor_grok",
                "request_id": "16161616-1616-4616-8616-161616161616",
                "phase": "controller",
                "expect_revision": None,
                "expect_digest": None,
            },
        )
        assert status == 409
        assert payload["error"] == "binding-missing"
