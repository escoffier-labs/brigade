"""Contract tests for the role-scoped Grok Bot Streamable HTTP adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import cli, fleet_client, grokbot_jobs, grokbot_mcp

REPORT_TEXT = "PRIVATE_SCOUT_FINDING_TOKEN_alpha\nScout findings for example/brigade.\n"


def test_grokbot_hub_failures_are_permissive_only_without_a_configured_hub(monkeypatch):
    unavailable = fleet_client.CloudDecision(False, "hub-unavailable")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "", "token": ""})
    assert grokbot_mcp._cloud_refused(unavailable) is False

    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example", "token": "node"})
    assert grokbot_mcp._cloud_refused(unavailable) is True
    assert grokbot_mcp._cloud_refused(None) is True


def _spec(role: str) -> dict[str, object]:
    return {
        "label": f"{role} job",
        "role": role,
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": ["src/brigade/grokbot_mcp.py"],
        "instructions": "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK",
        "verification_commands": ["pytest -q tests/test_grokbot_mcp.py"],
        "artifact": {"kind": "report" if role == "repository-scout" else "draft-pr"},
        "timeout_seconds": 900,
    }


def _report_artifact(text: str = REPORT_TEXT, path: str = "artifacts/report.md") -> dict[str, str]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"kind": "report", "path": path, "sha256": digest}


def _running_scout_job(tmp_path: Path) -> str:
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("repository-scout"), "scout-job")["job_id"]
    grokbot_jobs.claim(tmp_path, job_id, "grokbot-repository-scout", "lease-a", grokbot_mcp.LEASE_SECONDS)
    grokbot_jobs.transition(tmp_path, job_id, "grokbot-repository-scout", "lease-a", "running")
    return job_id


def _adapter(
    tmp_path: Path, role: str = "implementation-worker", *, hub_token: str | None = None
) -> grokbot_mcp.GrokbotAdapter:
    return grokbot_mcp.GrokbotAdapter(
        grokbot_mcp.ListenerConfig(
            target=tmp_path,
            instance=role,
            bind_host="127.0.0.1",
            bind_port=8766,
            allowed_hosts=(),
            allowed_origins=(),
            bearer="not-a-real-token",
            hub_token=hub_token,
        )
    )


def test_exact_role_tool_inventories_and_hidden_direct_calls_are_indistinguishable(tmp_path: Path):
    expected = {
        "operator": {
            "grokbot_queue_list",
            "grokbot_queue_status",
            "grokbot_queue_cancel",
            "grokbot_queue_expire",
            "grokbot_queue_report",
        },
        "repository-scout": {
            "grokbot_queue_list",
            "grokbot_queue_status",
            "grokbot_queue_claim",
            "grokbot_queue_renew",
            "grokbot_queue_start",
            "grokbot_queue_complete",
            "grokbot_queue_fail",
            "grokbot_queue_ack_cancel",
        },
        "implementation-worker": {
            "grokbot_queue_list",
            "grokbot_queue_status",
            "grokbot_queue_claim",
            "grokbot_queue_renew",
            "grokbot_queue_start",
            "grokbot_queue_complete",
            "grokbot_queue_fail",
            "grokbot_queue_ack_cancel",
        },
    }

    for role, tools in expected.items():
        adapter = _adapter(tmp_path, role)
        assert {item["name"] for item in adapter.tool_inventory()} == tools

    operator = _adapter(tmp_path, "operator")
    with pytest.raises(grokbot_mcp.AdapterError) as hidden:
        operator.call_tool("grokbot_queue_claim", {"job_id": "grokbot-" + "a" * 24, "lease_id": "lease-a"})
    with pytest.raises(grokbot_mcp.AdapterError) as unknown:
        operator.call_tool("not-a-real-tool", {})
    assert hidden.value.public_error() == unknown.value.public_error()

    worker = _adapter(tmp_path, "repository-scout")
    with pytest.raises(grokbot_mcp.AdapterError) as worker_hidden:
        worker.call_tool("grokbot_queue_report", {"job_id": "grokbot-" + "a" * 24})
    assert worker_hidden.value.public_error() == unknown.value.public_error()


def test_workers_only_list_claim_and_read_their_configured_role(tmp_path: Path, monkeypatch):
    implementation = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    scout = grokbot_jobs.enqueue(tmp_path, _spec("repository-scout"), "scout-job")["job_id"]
    worker = _adapter(tmp_path)

    listed = worker.call_tool("grokbot_queue_list", {})
    assert [job["job_id"] for job in listed["jobs"]] == [implementation]
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in json.dumps(listed)

    with pytest.raises(grokbot_mcp.AdapterError) as unauthorized:
        worker.call_tool("grokbot_queue_claim", {"job_id": scout, "lease_id": "lease-a"})
    assert unauthorized.value.public_error() == {
        "error": {"code": "invalid_request", "message": "Tool input failed validation"}
    }
    assert grokbot_jobs.get_job(tmp_path, scout)["state"] == "queued"

    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *args, **kwargs: fleet_client.CloudDecision(True, "ok", lease={"lease_id": implementation}),
    )
    monkeypatch.setattr(fleet_client, "bind_cloud", lambda *args, **kwargs: fleet_client.CloudDecision(True, "ok"))
    claimed = worker.call_tool("grokbot_queue_claim", {"job_id": implementation, "lease_id": "lease-a"})
    assert claimed["state"] == "claimed"
    assert claimed["execution_context"] == _spec("implementation-worker")
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in json.dumps(listed)
    assert "execution_context" not in worker.call_tool("grokbot_queue_status", {"job_id": implementation})
    assert grokbot_jobs.get_job(tmp_path, implementation)["role"] == "implementation-worker"


def test_worker_lifecycle_is_mirrored_to_the_fleet_without_queue_content(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    def claim(target, **kwargs):
        calls.append(("claim", {"target": target, **kwargs}))
        return fleet_client.ClaimDecision(granted=True, reason="ok", holder=str(kwargs["holder"]))

    monkeypatch.setattr(fleet_client, "acquire_claim", claim)

    def renew(target, **kwargs):
        calls.append(("renew", {"target": target, **kwargs}))
        return fleet_client.ClaimDecision(granted=True, reason="ok", holder=str(kwargs["holder"]))

    monkeypatch.setattr(fleet_client, "renew_claim", renew)
    monkeypatch.setattr(
        fleet_client, "release_claim", lambda target, **kwargs: calls.append(("release", {"target": target, **kwargs}))
    )
    monkeypatch.setattr(
        fleet_client,
        "report_external_event",
        lambda **kwargs: calls.append(("event", kwargs)),
        raising=False,
    )

    adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-a"})
    adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-a"})
    adapter.call_tool("grokbot_queue_start", {"job_id": job_id, "lease_id": "lease-a"})
    adapter.call_tool("grokbot_queue_fail", {"job_id": job_id, "lease_id": "lease-a"})

    claim_call = next(payload for kind, payload in calls if kind == "claim")
    holder = grokbot_mcp.fleet_holder(job_id, "lease-a")
    session = grokbot_mcp.fleet_session(job_id, "lease-a")
    assert holder != "lease-a" and session != "lease-a" and holder != session
    assert {key: claim_call[key] for key in ("harness", "role", "job", "session", "holder")} == {
        "harness": "grokbot",
        "role": "implementation-worker",
        "job": job_id,
        "session": session,
        "holder": holder,
    }
    events = [payload for kind, payload in calls if kind == "event"]
    assert [payload["state"] for payload in events] == [
        "external.claimed",
        "external.heartbeat",
        "external.running",
        "external.failed",
    ]
    assert all(payload["session"] == session for payload in events)
    dumped_events = json.dumps(events)
    assert "lease-a" not in dumped_events
    assert holder not in dumped_events
    assert [kind for kind, _payload in calls].count("release") == 1
    assert next(payload for kind, payload in calls if kind == "release")["holder"] == holder
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in json.dumps(calls)


def test_complete_and_cancel_ack_release_the_fleet_claim(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    def claim(target, **kwargs):
        calls.append(("claim", {"target": target, **kwargs}))
        return fleet_client.ClaimDecision(granted=True, reason="ok", holder=str(kwargs["holder"]))

    monkeypatch.setattr(fleet_client, "acquire_claim", claim)
    monkeypatch.setattr(
        fleet_client,
        "release_claim",
        lambda target, **kwargs: calls.append(("release", {"target": target, **kwargs})),
    )
    monkeypatch.setattr(
        fleet_client,
        "report_external_event",
        lambda **kwargs: calls.append(("event", kwargs)),
        raising=False,
    )

    adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-b"})
    adapter.call_tool("grokbot_queue_start", {"job_id": job_id, "lease_id": "lease-b"})
    adapter.call_tool(
        "grokbot_queue_complete",
        {
            "job_id": job_id,
            "lease_id": "lease-b",
            "artifact": {
                "kind": "draft-pr",
                "url": "https://github.com/example/brigade/pull/1",
                "branch": "feat/SECRET_ARTIFACT",
            },
        },
    )
    assert [payload["state"] for kind, payload in calls if kind == "event"] == [
        "external.claimed",
        "external.running",
        "external.completed",
    ]
    assert [kind for kind, _payload in calls].count("release") == 1
    dumped = json.dumps(calls)
    assert "SECRET_ARTIFACT" not in dumped
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in dumped
    assert "hub_url" not in dumped
    assert "token" not in dumped
    assert "bearer" not in dumped

    canceled = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "cancel-job")["job_id"]
    adapter.call_tool("grokbot_queue_claim", {"job_id": canceled, "lease_id": "lease-c"})
    grokbot_jobs.cancel(tmp_path, canceled)
    adapter.call_tool("grokbot_queue_ack_cancel", {"job_id": canceled, "lease_id": "lease-c"})
    assert [payload["state"] for kind, payload in calls if kind == "event"][-1] == "external.canceled"
    assert [kind for kind, _payload in calls].count("release") == 2


def test_fleet_holder_and_session_are_domain_separated_from_the_queue_lease():
    holder = grokbot_mcp.fleet_holder("job-a", "lease-a")
    session = grokbot_mcp.fleet_session("job-a", "lease-a")
    assert holder == grokbot_mcp.fleet_holder("job-a", "lease-a")
    assert session == grokbot_mcp.fleet_session("job-a", "lease-a")
    assert holder != session
    assert "lease-a" not in holder
    assert "lease-a" not in session
    assert "job-a" not in holder
    assert "job-a" not in session
    assert grokbot_mcp.fleet_holder("job-a", "lease-b") != holder
    assert grokbot_mcp.fleet_session("job-a", "lease-b") != session
    assert len(holder) == 64
    assert 0 < len(session) < len(holder)


def test_same_lease_id_on_different_jobs_derives_distinct_fleet_identities():
    lease_id = "shared-lease"
    holder_a = grokbot_mcp.fleet_holder("job-a", lease_id)
    holder_b = grokbot_mcp.fleet_holder("job-b", lease_id)
    session_a = grokbot_mcp.fleet_session("job-a", lease_id)
    session_b = grokbot_mcp.fleet_session("job-b", lease_id)
    assert holder_a != holder_b
    assert session_a != session_b
    assert holder_a == grokbot_mcp.fleet_holder("job-a", lease_id)
    assert session_a == grokbot_mcp.fleet_session("job-a", lease_id)
    assert holder_a != session_a
    assert holder_b != session_b


def test_same_lease_id_on_different_jobs_cannot_share_or_release_fleet_claims(tmp_path: Path, monkeypatch):
    first = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "first-job")["job_id"]
    second = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "second-job")["job_id"]
    adapter = _adapter(tmp_path)
    held: list[str] = []
    releases: list[str] = []

    def claim(target, **kwargs):
        holder = str(kwargs["holder"])
        if held and held[0] != holder:
            return fleet_client.ClaimDecision(granted=False, reason="held", holder=holder)
        if not held:
            held.append(holder)
        return fleet_client.ClaimDecision(granted=True, reason="ok", holder=holder)

    def release(target, **kwargs):
        holder = str(kwargs["holder"])
        releases.append(holder)
        if held and held[0] == holder:
            held.clear()

    monkeypatch.setattr(fleet_client, "acquire_claim", claim)
    monkeypatch.setattr(fleet_client, "release_claim", release)
    monkeypatch.setattr(fleet_client, "report_external_event", lambda **kwargs: None, raising=False)

    adapter.call_tool("grokbot_queue_claim", {"job_id": first, "lease_id": "shared-lease"})
    with pytest.raises(grokbot_mcp.AdapterError):
        adapter.call_tool("grokbot_queue_claim", {"job_id": second, "lease_id": "shared-lease"})
    assert grokbot_jobs.get_job(tmp_path, first)["state"] == "claimed"
    assert grokbot_jobs.get_job(tmp_path, second)["state"] == "queued"
    first_holder = grokbot_mcp.fleet_holder(first, "shared-lease")
    second_holder = grokbot_mcp.fleet_holder(second, "shared-lease")
    first_session = grokbot_mcp.fleet_session(first, "shared-lease")
    second_session = grokbot_mcp.fleet_session(second, "shared-lease")
    assert first_holder != second_holder
    assert first_session != second_session
    assert held == [first_holder]

    with pytest.raises(grokbot_mcp.AdapterError):
        adapter.call_tool("grokbot_queue_fail", {"job_id": second, "lease_id": "shared-lease"})
    assert releases == []
    assert held == [first_holder]
    assert grokbot_jobs.get_job(tmp_path, first)["state"] == "claimed"
    assert grokbot_jobs.get_job(tmp_path, second)["state"] == "queued"

    adapter.call_tool("grokbot_queue_fail", {"job_id": first, "lease_id": "shared-lease"})
    assert releases == [first_holder]
    assert held == []


def test_known_fleet_refusal_does_not_claim_or_emit(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    for reason in ("held", "auth-failed", "invalid", "missing"):
        calls: list[tuple[str, dict[str, object]]] = []

        def refuse(target, *, _reason=reason, **kwargs):
            return fleet_client.ClaimDecision(granted=False, reason=_reason, holder=str(kwargs["holder"]))

        def record_event(*, _calls=calls, **kwargs):
            _calls.append(("event", kwargs))

        monkeypatch.setattr(fleet_client, "acquire_claim", refuse)
        monkeypatch.setattr(fleet_client, "report_external_event", record_event, raising=False)
        with pytest.raises(grokbot_mcp.AdapterError):
            adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-refuse"})
        assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "queued"
        assert calls == []


def test_best_effort_fleet_gaps_still_claim_the_queue(tmp_path: Path, monkeypatch):
    for reason in ("no-hub", "no-identity", "hub-unavailable"):
        job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), f"{reason}-job")["job_id"]
        adapter = _adapter(tmp_path)

        def gap(target, *, _reason=reason, **kwargs):
            return fleet_client.ClaimDecision(granted=False, reason=_reason, holder=str(kwargs["holder"]))

        monkeypatch.setattr(fleet_client, "acquire_claim", gap)
        claimed = adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": f"lease-{reason}"})
        assert claimed["state"] == "claimed"
        assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "claimed"


def test_known_fleet_renew_refusal_does_not_heartbeat_or_extend_the_lease(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(
        fleet_client,
        "acquire_claim",
        lambda target, **kwargs: fleet_client.ClaimDecision(granted=True, reason="ok", holder=str(kwargs["holder"])),
    )
    events: list[dict[str, object]] = []
    monkeypatch.setattr(fleet_client, "report_external_event", lambda **kwargs: events.append(kwargs), raising=False)
    adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-renew"})
    events.clear()
    before = grokbot_jobs.get_job(tmp_path, job_id)["lease_expires_at"]
    monkeypatch.setattr(
        fleet_client,
        "renew_claim",
        lambda target, **kwargs: fleet_client.ClaimDecision(granted=False, reason="held", holder=str(kwargs["holder"])),
    )
    with pytest.raises(grokbot_mcp.AdapterError):
        adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-renew"})
    assert grokbot_jobs.get_job(tmp_path, job_id)["lease_expires_at"] == before
    assert events == []


def test_queue_renew_failure_releases_a_granted_fleet_claim(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    def granted(target, **kwargs):
        calls.append(("granted", {"target": target, **kwargs}))
        return fleet_client.ClaimDecision(granted=True, reason="ok", holder=str(kwargs["holder"]))

    monkeypatch.setattr(fleet_client, "acquire_claim", granted)
    monkeypatch.setattr(fleet_client, "renew_claim", granted)
    monkeypatch.setattr(
        fleet_client,
        "release_claim",
        lambda target, **kwargs: calls.append(("release", {"target": target, **kwargs})),
    )
    monkeypatch.setattr(
        fleet_client,
        "report_external_event",
        lambda **kwargs: calls.append(("event", kwargs)),
        raising=False,
    )
    adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-renew-rollback"})
    before = grokbot_jobs.get_job(tmp_path, job_id)["lease_expires_at"]
    monkeypatch.setattr(
        grokbot_jobs,
        "renew",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(grokbot_jobs.GrokbotJobError("lease-conflict")),
    )
    with pytest.raises(grokbot_mcp.AdapterError):
        adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-renew-rollback"})
    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "claimed"
    assert grokbot_jobs.get_job(tmp_path, job_id)["lease_expires_at"] == before
    assert [kind for kind, _payload in calls if kind == "release"] == ["release"]
    assert next(payload for kind, payload in calls if kind == "release")["holder"] == grokbot_mcp.fleet_holder(
        job_id, "lease-renew-rollback"
    )
    assert [payload["state"] for kind, payload in calls if kind == "event"] == ["external.claimed"]


def test_fleet_outage_then_missing_renew_recovers_via_acquire(tmp_path: Path, monkeypatch):
    for reason in ("no-hub", "no-identity", "hub-unavailable"):
        job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), f"{reason}-recover")["job_id"]
        adapter = _adapter(tmp_path)
        calls: list[tuple[str, dict[str, object]]] = []
        lease_id = f"lease-{reason}-recover"

        def gap_then_grant(target, *, _reason=reason, _calls=calls, **kwargs):
            _calls.append(("acquire", {"target": target, **kwargs}))
            if len([kind for kind, _payload in _calls if kind == "acquire"]) == 1:
                return fleet_client.ClaimDecision(granted=False, reason=_reason, holder=str(kwargs["holder"]))
            return fleet_client.ClaimDecision(granted=True, reason="ok", holder=str(kwargs["holder"]))

        def missing_renew(target, *, _calls=calls, **kwargs):
            _calls.append(("renew", {"target": target, **kwargs}))
            return fleet_client.ClaimDecision(granted=False, reason="missing", holder=str(kwargs["holder"]))

        def record_event(*, _calls=calls, **kwargs):
            _calls.append(("event", kwargs))

        monkeypatch.setattr(fleet_client, "acquire_claim", gap_then_grant)
        monkeypatch.setattr(fleet_client, "renew_claim", missing_renew)
        monkeypatch.setattr(fleet_client, "report_external_event", record_event, raising=False)
        adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": lease_id})
        result = adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": lease_id})
        assert result["state"] == "claimed"
        assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "claimed"
        assert [kind for kind, _payload in calls if kind == "renew"] == ["renew"]
        acquire_calls = [payload for kind, payload in calls if kind == "acquire"]
        assert len(acquire_calls) == 2
        recovered = acquire_calls[1]
        assert recovered["holder"] == grokbot_mcp.fleet_holder(job_id, lease_id)
        assert recovered["session"] == grokbot_mcp.fleet_session(job_id, lease_id)
        assert recovered["job"] == job_id
        assert [payload["state"] for kind, payload in calls if kind == "event"] == [
            "external.claimed",
            "external.heartbeat",
        ]


def test_fleet_outage_then_missing_renew_allows_best_effort_acquire(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "best-effort-recover")["job_id"]
    adapter = _adapter(tmp_path)
    events: list[dict[str, object]] = []

    def gap(target, **kwargs):
        return fleet_client.ClaimDecision(granted=False, reason="no-hub", holder=str(kwargs["holder"]))

    monkeypatch.setattr(fleet_client, "acquire_claim", gap)
    monkeypatch.setattr(
        fleet_client,
        "renew_claim",
        lambda target, **kwargs: fleet_client.ClaimDecision(
            granted=False, reason="missing", holder=str(kwargs["holder"])
        ),
    )
    monkeypatch.setattr(fleet_client, "report_external_event", lambda **kwargs: events.append(kwargs), raising=False)
    adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-best-effort"})
    events.clear()
    monkeypatch.setattr(
        fleet_client,
        "acquire_claim",
        lambda target, **kwargs: fleet_client.ClaimDecision(
            granted=False, reason="hub-unavailable", holder=str(kwargs["holder"])
        ),
    )
    result = adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-best-effort"})
    assert result["state"] == "claimed"
    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "claimed"
    assert [payload["state"] for payload in events] == ["external.heartbeat"]


def test_fleet_outage_then_missing_renew_refuses_known_acquire_refusal(tmp_path: Path, monkeypatch):
    for reason in ("held", "auth-failed", "invalid", "missing"):
        job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), f"{reason}-refuse")["job_id"]
        adapter = _adapter(tmp_path)
        events: list[dict[str, object]] = []
        monkeypatch.setattr(
            fleet_client,
            "acquire_claim",
            lambda target, **kwargs: fleet_client.ClaimDecision(
                granted=False, reason="no-hub", holder=str(kwargs["holder"])
            ),
        )

        def record_event(*, _events=events, **kwargs):
            _events.append(kwargs)

        monkeypatch.setattr(fleet_client, "report_external_event", record_event, raising=False)
        adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": f"lease-{reason}-refuse"})
        events.clear()
        before = grokbot_jobs.get_job(tmp_path, job_id)["lease_expires_at"]
        monkeypatch.setattr(
            fleet_client,
            "renew_claim",
            lambda target, **kwargs: fleet_client.ClaimDecision(
                granted=False, reason="missing", holder=str(kwargs["holder"])
            ),
        )
        monkeypatch.setattr(
            fleet_client,
            "acquire_claim",
            lambda target, *, _reason=reason, **kwargs: fleet_client.ClaimDecision(
                granted=False, reason=_reason, holder=str(kwargs["holder"])
            ),
        )
        with pytest.raises(grokbot_mcp.AdapterError):
            adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": f"lease-{reason}-refuse"})
        assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "claimed"
        assert grokbot_jobs.get_job(tmp_path, job_id)["lease_expires_at"] == before
        assert events == []


def test_queue_claim_failure_releases_a_granted_fleet_claim(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    def claim(target, **kwargs):
        calls.append(("claim", {"target": target, **kwargs}))
        return fleet_client.ClaimDecision(granted=True, reason="ok", holder=str(kwargs["holder"]))

    monkeypatch.setattr(fleet_client, "acquire_claim", claim)
    monkeypatch.setattr(
        fleet_client,
        "release_claim",
        lambda target, **kwargs: calls.append(("release", {"target": target, **kwargs})),
    )
    monkeypatch.setattr(
        fleet_client,
        "report_external_event",
        lambda **kwargs: calls.append(("event", kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        grokbot_jobs,
        "claim_execution_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(grokbot_jobs.GrokbotJobError("lease-conflict")),
    )
    with pytest.raises(grokbot_mcp.AdapterError):
        adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-rollback"})
    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "queued"
    assert [kind for kind, _payload in calls] == ["claim", "release"]
    assert calls[1][1]["holder"] == grokbot_mcp.fleet_holder(job_id, "lease-rollback")


def test_fleet_outage_does_not_fail_queue_claim(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("hub down")

    monkeypatch.setattr(fleet_client, "acquire_claim", boom)
    monkeypatch.setattr(fleet_client, "report_external_event", boom, raising=False)
    claimed = adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-d"})
    assert claimed["state"] == "claimed"
    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "claimed"


def test_configured_hub_outage_fails_closed_and_does_not_advance_local_lifecycle(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example", "token": "node"})
    monkeypatch.setattr(
        fleet_client,
        "acquire_claim",
        lambda *args, **kwargs: fleet_client.ClaimDecision(granted=False, reason="hub-unavailable", holder="x"),
    )

    with pytest.raises(grokbot_mcp.AdapterError):
        adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-d"})
    raw = json.loads((tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs" / f"{job_id}.json").read_text())
    assert raw["state"] == "queued"


def test_workers_cannot_retrieve_reports_and_operator_can(tmp_path: Path):
    spec = _spec("repository-scout")
    job_id = grokbot_jobs.enqueue(tmp_path, spec, "scout-report")["job_id"]
    worker = _adapter(tmp_path, "repository-scout")
    operator = _adapter(tmp_path, "operator")
    grokbot_jobs.claim(tmp_path, job_id, worker.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    grokbot_jobs.transition(tmp_path, job_id, worker.config.bot_id, "lease-a", "running")
    report = "Scout findings stay private."
    digest = __import__("hashlib").sha256(report.encode()).hexdigest()
    grokbot_jobs.complete_report(
        tmp_path,
        job_id,
        worker.config.bot_id,
        "lease-a",
        {"kind": "report", "path": "docs/scout.md", "sha256": digest},
        report,
    )
    with pytest.raises(grokbot_mcp.AdapterError):
        worker.call_tool("grokbot_queue_report", {"job_id": job_id})
    retrieved = operator.call_tool("grokbot_queue_report", {"job_id": job_id})
    assert retrieved["text"] == report
    assert retrieved["sha256"] == digest
    assert report not in json.dumps(operator.call_tool("grokbot_queue_status", {"job_id": job_id}))
GENERIC_CLAIM_ERROR = {
    "error": {"code": "invalid_request", "message": "Tool input failed validation"}
}


@pytest.mark.parametrize(
    "kind",
    (
        "terminal_completed",
        "terminal_failed",
        "expired",
        "conflicting",
        "malformed_job_id",
        "malformed_extra_field",
    ),
)
def test_rejected_claims_return_generic_error_and_hide_execution_context(tmp_path: Path, kind: str):
    worker = _adapter(tmp_path)
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), f"{kind}-job")["job_id"]
    bot_id = worker.config.bot_id
    now = datetime.now(timezone.utc)
    arguments = {"job_id": job_id, "lease_id": "lease-b"}

    if kind == "terminal_completed":
        grokbot_jobs.claim(tmp_path, job_id, bot_id, "lease-a", 300, now=now)
        grokbot_jobs.transition(tmp_path, job_id, bot_id, "lease-a", "running", now=now)
        grokbot_jobs.transition(
            tmp_path,
            job_id,
            bot_id,
            "lease-a",
            "completed",
            artifact={
                "kind": "draft-pr",
                "url": "https://github.com/example/brigade/pull/1",
                "branch": "grokbot/job",
            },
            now=now,
        )
    elif kind == "terminal_failed":
        grokbot_jobs.claim(tmp_path, job_id, bot_id, "lease-a", 300, now=now)
        grokbot_jobs.transition(tmp_path, job_id, bot_id, "lease-a", "failed", now=now)
    elif kind == "expired":
        grokbot_jobs.expire(tmp_path, job_id, now=now + timedelta(seconds=901))
        arguments = {"job_id": job_id, "lease_id": "lease-a"}
    elif kind == "conflicting":
        worker.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-a"})
    elif kind == "malformed_job_id":
        arguments = {"job_id": "not-a-job", "lease_id": "lease-a"}
    else:
        arguments = {"job_id": job_id, "lease_id": "lease-a", "role": "operator"}

    with pytest.raises(grokbot_mcp.AdapterError) as error:
        worker.call_tool("grokbot_queue_claim", arguments)

    payload = error.value.public_error()
    rendered = json.dumps(payload)
    assert payload == GENERIC_CLAIM_ERROR
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in rendered
    assert "execution_context" not in rendered


def test_worker_inputs_cannot_select_target_role_bot_identity_or_lease_duration(tmp_path: Path):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    worker = _adapter(tmp_path)

    for forbidden in ("target", "role", "bot_id", "lease_seconds"):
        with pytest.raises(grokbot_mcp.AdapterError) as error:
            worker.call_tool(
                "grokbot_queue_claim",
                {"job_id": job_id, "lease_id": "lease-a", forbidden: "attacker"},
            )
        assert error.value.public_error()["error"]["code"] == "invalid_request"


def test_claim_is_admitted_before_queue_mutation_and_binds_the_deterministic_hub_lease(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    def admit(provider: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("admit", {"provider": provider, **kwargs}))
        assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "queued"
        return fleet_client.CloudDecision(True, "ok", lease={"lease_id": job_id})

    def bind(lease_id: str, provider_task_id: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("bind", {"lease_id": lease_id, "provider_task_id": provider_task_id, **kwargs}))
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "admit_cloud", admit)
    monkeypatch.setattr(fleet_client, "bind_cloud", bind)

    claimed = adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-a"})

    assert claimed["state"] == "claimed"
    assert [call[0] for call in calls] == ["admit", "bind"]
    admitted = calls[0][1]
    assert admitted == {
        "provider": "grokbot-cloud",
        "repo": "example/brigade",
        "label": f"grokbot-cloud:example/brigade@{claimed['task_hash'].removeprefix('sha256:')[:12]}",
        "prompt_hash": claimed["task_hash"].removeprefix("sha256:"),
        "conductor": "implementation-worker",
        "ttl_seconds": grokbot_mcp.LEASE_SECONDS,
        "lease_id": job_id,
        "holder": "lease-a",
    }
    assert calls[1][1] == {"lease_id": job_id, "provider_task_id": job_id, "holder": "lease-a"}


def test_claim_denial_leaves_the_local_job_queued(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *args, **kwargs: fleet_client.CloudDecision(False, "refused"),
    )
    monkeypatch.setattr(fleet_client, "bind_cloud", lambda *args, **kwargs: pytest.fail("bind must not run"))

    with pytest.raises(grokbot_mcp.AdapterError):
        _adapter(tmp_path).call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-a"})

    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "queued"


def test_claim_releases_an_admitted_hub_lease_when_the_local_claim_fails(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    releases: list[dict[str, object]] = []
    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *args, **kwargs: fleet_client.CloudDecision(True, "ok", lease={"lease_id": job_id}),
    )
    monkeypatch.setattr(
        grokbot_jobs,
        "claim_execution_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(grokbot_jobs.GrokbotJobError("lease-conflict")),
    )
    monkeypatch.setattr(
        fleet_client,
        "release_cloud",
        lambda lease_id, **kwargs: (
            releases.append({"lease_id": lease_id, **kwargs}) or fleet_client.CloudDecision(True, "ok")
        ),
    )

    with pytest.raises(grokbot_mcp.AdapterError):
        _adapter(tmp_path).call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-a"})

    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "queued"
    assert releases == [{"lease_id": job_id, "state": "released", "holder": "lease-a"}]


def test_renew_reconciles_a_legacy_running_job_and_normal_renewals_refresh_the_hub(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, adapter.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    grokbot_jobs.transition(tmp_path, job_id, adapter.config.bot_id, "lease-a", "running")
    calls: list[tuple[str, dict[str, object]]] = []

    def renew(lease_id: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("renew", {"lease_id": lease_id, **kwargs}))
        return fleet_client.CloudDecision(False, "refused")

    def admit(provider: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("admit", {"provider": provider, **kwargs}))
        return fleet_client.CloudDecision(True, "ok", lease={"lease_id": job_id})

    def bind(lease_id: str, provider_task_id: str, **kwargs: object) -> fleet_client.CloudDecision:
        calls.append(("bind", {"lease_id": lease_id, "provider_task_id": provider_task_id, **kwargs}))
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "renew_cloud", renew)
    monkeypatch.setattr(fleet_client, "admit_cloud", admit)
    monkeypatch.setattr(fleet_client, "bind_cloud", bind)

    renewed = adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-a"})

    assert renewed["state"] == "running"
    assert [name for name, _ in calls] == ["renew", "admit", "bind"]
    assert calls[0][1] == {"lease_id": job_id, "ttl_seconds": grokbot_mcp.LEASE_SECONDS, "holder": "lease-a"}
    assert calls[-1][1] == {"lease_id": job_id, "provider_task_id": job_id, "holder": "lease-a"}

    calls.clear()
    monkeypatch.setattr(
        fleet_client,
        "renew_cloud",
        lambda lease_id, **kwargs: (
            calls.append(("renew", {"lease_id": lease_id, **kwargs})) or fleet_client.CloudDecision(True, "ok")
        ),
    )
    adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-a"})
    assert [name for name, _ in calls] == ["renew"]


def test_start_reconciles_a_missing_hub_lease_without_rolling_back_the_local_transition(tmp_path: Path, monkeypatch):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, adapter.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    calls: list[str] = []
    monkeypatch.setattr(
        fleet_client,
        "renew_cloud",
        lambda *args, **kwargs: calls.append("renew") or fleet_client.CloudDecision(False, "refused"),
    )
    monkeypatch.setattr(
        fleet_client,
        "admit_cloud",
        lambda *args, **kwargs: (
            calls.append("admit") or fleet_client.CloudDecision(True, "ok", lease={"lease_id": job_id})
        ),
    )
    monkeypatch.setattr(
        fleet_client,
        "bind_cloud",
        lambda *args, **kwargs: calls.append("bind") or fleet_client.CloudDecision(True, "ok"),
    )

    started = adapter.call_tool("grokbot_queue_start", {"job_id": job_id, "lease_id": "lease-a"})

    assert started["state"] == "running"
    assert calls == ["renew", "admit", "bind"]


@pytest.mark.parametrize(
    ("tool", "arguments", "prepare", "expected_state"),
    [
        ("grokbot_queue_fail", {}, None, "failed"),
        (
            "grokbot_queue_complete",
            {
                "artifact": {
                    "kind": "draft-pr",
                    "url": "https://github.com/example/brigade/pull/123",
                    "branch": "grokbot/job-1",
                }
            },
            "running",
            "completed",
        ),
        ("grokbot_queue_ack_cancel", {}, "cancel", "canceled"),
    ],
)
def test_terminal_worker_transitions_release_hub_leases(
    tmp_path: Path, monkeypatch, tool: str, arguments: dict[str, object], prepare: str | None, expected_state: str
):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, adapter.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    if prepare == "running":
        grokbot_jobs.transition(tmp_path, job_id, adapter.config.bot_id, "lease-a", "running")
    elif prepare == "cancel":
        grokbot_jobs.cancel(tmp_path, job_id)
    releases: list[dict[str, object]] = []
    monkeypatch.setattr(
        fleet_client,
        "release_cloud",
        lambda lease_id, **kwargs: (
            releases.append({"lease_id": lease_id, **kwargs}) or fleet_client.CloudDecision(True, "ok")
        ),
    )

    result = adapter.call_tool(tool, {"job_id": job_id, "lease_id": "lease-a", **arguments})

    assert result["state"] == expected_state
    assert releases == [{"lease_id": job_id, "state": expected_state, "holder": "lease-a"}]


def test_hub_mirror_failures_do_not_undo_a_local_terminal_transition_or_leak_private_values(
    tmp_path: Path, monkeypatch
):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    adapter = _adapter(tmp_path)
    grokbot_jobs.claim(tmp_path, job_id, adapter.config.bot_id, "lease-a", grokbot_mcp.LEASE_SECONDS)
    monkeypatch.setattr(
        fleet_client,
        "release_cloud",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("lease-a PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK")),
    )

    failed = adapter.call_tool("grokbot_queue_fail", {"job_id": job_id, "lease_id": "lease-a"})

    assert failed["state"] == "failed"
    projection = _adapter(tmp_path).call_tool("grokbot_queue_status", {"job_id": job_id})
    rendered = json.dumps(projection)
    assert "lease-a" not in rendered
    assert "PRIVATE_INSTRUCTIONS_MUST_NOT_LEAK" not in rendered


def test_safe_error_projection_never_exposes_queue_error_or_private_content(tmp_path: Path):
    adapter = _adapter(tmp_path)
    with pytest.raises(grokbot_mcp.AdapterError) as error:
        adapter.call_tool("grokbot_queue_status", {"job_id": "grokbot-" + "a" * 24})

    rendered = json.dumps(error.value.public_error())
    assert rendered == '{"error": {"code": "invalid_request", "message": "Tool input failed validation"}}'
    assert "PRIVATE" not in rendered
    assert "storage" not in rendered


def test_bearer_comparison_is_constant_time_and_header_shape_is_strict(tmp_path: Path):
    adapter = _adapter(tmp_path)
    assert adapter.authorized("Bearer not-a-real-token") is True
    assert adapter.authorized("Bearer not-a-real-tokeN") is False
    assert adapter.authorized("not-a-real-token") is False
    assert adapter.authorized("Basic not-a-real-token") is False
    assert adapter.authorized("Bearer not-a-real-tok\N{SNOWMAN}") is False


def test_non_ascii_configured_bearer_is_rejected(tmp_path: Path):
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_mcp.ListenerConfig(
            instance="operator",
            target=tmp_path,
            bind_host="127.0.0.1",
            bind_port=8766,
            allowed_hosts=(),
            allowed_origins=(),
            bearer="not-a-real-tok\N{SNOWMAN}",
        ).validate()


def test_listener_configuration_defaults_to_loopback_and_requires_allowlists_for_real_hostnames(tmp_path: Path):
    assert grokbot_mcp.parse_bind("127.0.0.1:8766") == ("127.0.0.1", 8766)
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_mcp.ListenerConfig(
            target=tmp_path,
            instance="operator",
            bind_host="mcp.example.test",
            bind_port=8766,
            allowed_hosts=(),
            allowed_origins=(),
            bearer="not-a-real-token",
        ).validate()
    config = grokbot_mcp.ListenerConfig(
        target=tmp_path,
        instance="operator",
        bind_host="mcp.example.test",
        bind_port=8766,
        allowed_hosts=("mcp.example.test",),
        allowed_origins=("https://console.example.test",),
        bearer="not-a-real-token",
    )
    config.validate()


def test_security_gate_checks_host_origin_auth_and_bounded_request_size(tmp_path: Path):
    gate = grokbot_mcp.RequestGate(_adapter(tmp_path).config, max_request_bytes=32)
    assert gate.reject_reason({"host": "127.0.0.1:8766", "authorization": "Bearer not-a-real-token"}, 31) is None
    assert gate.reject_reason({"host": "attacker.test", "authorization": "Bearer not-a-real-token"}, 1) == "forbidden"
    assert (
        gate.reject_reason(
            {"host": "127.0.0.1", "origin": "https://attacker.test", "authorization": "Bearer not-a-real-token"}, 1
        )
        == "forbidden"
    )
    assert gate.reject_reason({"host": "127.0.0.1", "authorization": "Bearer wrong"}, 1) == "unauthorized"
    assert gate.reject_reason({"host": "127.0.0.1", "authorization": "Bearer not-a-real-token"}, 33) == "too-large"


def test_health_output_is_minimal_and_never_includes_config_or_queue_data(tmp_path: Path):
    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    payload = _adapter(tmp_path).health_payload()

    assert payload == {"ok": True, "service": "grokbot-mcp", "role": "implementation-worker"}
    rendered = json.dumps(payload)
    for forbidden in (job_id, "target", "bearer", "host", "error", "PRIVATE"):
        assert forbidden not in rendered


def test_streamable_http_app_enforces_bearer_origin_and_body_limit(tmp_path: Path):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient", reason="requires the grokbot extra").TestClient

    with TestClient(grokbot_mcp.build_app(_adapter(tmp_path).config), base_url="http://127.0.0.1:8766") as client:
        assert client.get("/health").status_code == 401
        response = client.get("/health", headers={"authorization": "Bearer not-a-real-token"})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "service": "grokbot-mcp", "role": "implementation-worker"}
        assert (
            client.post(
                "/mcp",
                content=b"x" * (grokbot_mcp.MAX_REQUEST_BYTES + 1),
                headers={"authorization": "Bearer not-a-real-token", "content-type": "application/json"},
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/mcp",
                json={},
                headers={"authorization": "Bearer not-a-real-token", "origin": "https://attacker.test"},
            ).status_code
            == 403
        )


def test_asgi_rejects_non_ascii_authorization_without_reaching_the_app(tmp_path: Path):
    adapter = _adapter(tmp_path)

    async def downstream(scope, receive, send):
        raise AssertionError("unauthorized request reached downstream")

    async def request() -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await grokbot_mcp._GateASGI(downstream, grokbot_mcp.RequestGate(adapter.config), adapter)(
            {
                "type": "http",
                "method": "GET",
                "path": "/health",
                "headers": [(b"host", b"127.0.0.1:8766"), (b"authorization", b"Bearer not-a-real-tok\xff")],
            },
            receive,
            send,
        )
        return sent

    sent = asyncio.run(request())
    assert sent[0]["status"] in {401, 403}


def test_gate_bounded_reads_every_http_method_and_preserves_bodyless_requests(tmp_path: Path):
    adapter = _adapter(tmp_path)
    delivered: list[tuple[str, list[dict[str, object]]]] = []

    async def downstream(scope, receive, send):
        messages = []
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") == "http.disconnect":
                break
        delivered.append((scope["method"], messages))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def request(method: str, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        pending = iter(messages)
        sent: list[dict[str, object]] = []

        async def receive():
            return next(pending, {"type": "http.disconnect"})

        async def send(message):
            sent.append(message)

        await grokbot_mcp._GateASGI(downstream, grokbot_mcp.RequestGate(adapter.config), adapter)(
            {
                "type": "http",
                "method": method,
                "path": "/health",
                "headers": [
                    (b"host", b"127.0.0.1:8766"),
                    (b"authorization", b"Bearer not-a-real-token"),
                ],
            },
            receive,
            send,
        )
        return sent

    for method in ("GET", "DELETE"):
        sent = asyncio.run(
            request(
                method,
                [
                    {"type": "http.request", "body": b"x" * grokbot_mcp.MAX_REQUEST_BYTES, "more_body": True},
                    {"type": "http.request", "body": b"x", "more_body": False},
                ],
            )
        )
        assert sent[0]["status"] == 413

    for method in ("GET", "DELETE"):
        sent = asyncio.run(request(method, [{"type": "http.request", "body": b"", "more_body": False}]))
        assert sent[0]["status"] == 200
    assert [method for method, _ in delivered] == ["GET", "DELETE"]


def test_loopback_canary_path_keeps_sdk_loopback_hosts_with_a_public_allowlist(tmp_path: Path):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient", reason="requires the grokbot extra").TestClient

    config = grokbot_mcp.ListenerConfig(
        target=tmp_path,
        instance="implementation-worker",
        bind_host="127.0.0.1",
        bind_port=8766,
        allowed_hosts=("mcp.example.test",),
        allowed_origins=(),
        bearer="not-a-real-token",
    )
    non_loopback = grokbot_mcp.ListenerConfig(
        target=tmp_path,
        instance="implementation-worker",
        bind_host="mcp.example.test",
        bind_port=8766,
        allowed_hosts=("mcp.example.test",),
        allowed_origins=("https://console.example.test",),
        bearer="not-a-real-token",
    )
    assert set(grokbot_mcp._transport_allowed_hosts(config)) >= {
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "::1",
        "[::1]:*",
    }
    assert grokbot_mcp._transport_allowed_hosts(non_loopback) == ["mcp.example.test"]

    with TestClient(grokbot_mcp.build_app(config), base_url="http://127.0.0.1:8766") as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "grokbot_queue_list", "arguments": {}},
            },
            headers={"authorization": "Bearer not-a-real-token"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False


def test_mcp_asgi_rejects_extra_and_malformed_raw_tool_arguments(tmp_path: Path):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient", reason="requires the grokbot extra").TestClient

    headers = {"authorization": "Bearer not-a-real-token", "content-type": "application/json"}
    invalid_calls = (
        ("grokbot_queue_list", {"bot_id": "caller-selected"}),
        ("grokbot_queue_status", {"job_id": []}),
        ("grokbot_queue_complete", {"job_id": "grokbot-" + "a" * 24, "lease_id": "lease-a", "artifact": []}),
    )
    with TestClient(grokbot_mcp.build_app(_adapter(tmp_path).config), base_url="http://127.0.0.1:8766") as client:
        for name, arguments in invalid_calls:
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                headers=headers,
            )
            assert response.status_code == 400
            assert response.json() == {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32602, "message": "Invalid request"},
            }

        valid = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "grokbot_queue_list", "arguments": {}},
            },
            headers=headers,
        )
        omitted_arguments = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "grokbot_queue_list"},
            },
            headers=headers,
        )
    assert valid.status_code == 200
    assert valid.json()["result"]["isError"] is False
    assert omitted_arguments.status_code == 200
    assert omitted_arguments.json()["result"]["isError"] is False


def test_complete_with_report_text_retrieves_via_operator_without_leaking(tmp_path: Path, monkeypatch):
    job_id = _running_scout_job(tmp_path)
    scout = _adapter(tmp_path, "repository-scout")
    operator = _adapter(tmp_path, "operator")
    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        fleet_client,
        "acquire_claim",
        lambda target, **kwargs: fleet_client.ClaimDecision(granted=True, reason="ok", holder=str(kwargs["holder"])),
    )
    monkeypatch.setattr(
        fleet_client,
        "report_external_event",
        lambda **kwargs: events.append(kwargs),
        raising=False,
    )
    monkeypatch.setattr(fleet_client, "release_claim", lambda target, **kwargs: None, raising=False)

    completed = scout.call_tool(
        "grokbot_queue_complete",
        {
            "job_id": job_id,
            "lease_id": "lease-a",
            "artifact": _report_artifact(),
            "report_text": REPORT_TEXT,
        },
    )
    assert completed["state"] == "completed"
    assert REPORT_TEXT not in json.dumps(completed)

    report = operator.call_tool("grokbot_queue_report", {"job_id": job_id})
    assert report == {
        "job_id": job_id,
        "text": REPORT_TEXT,
        "bytes": len(REPORT_TEXT.encode("utf-8")),
        "sha256": hashlib.sha256(REPORT_TEXT.encode("utf-8")).hexdigest(),
    }

    listed = operator.call_tool("grokbot_queue_list", {})
    status = operator.call_tool("grokbot_queue_status", {"job_id": job_id})
    dumped = json.dumps({"listed": listed, "status": status, "events": events})
    assert REPORT_TEXT not in dumped
    assert "PRIVATE_SCOUT_FINDING_TOKEN_alpha" not in dumped

    with pytest.raises(grokbot_mcp.AdapterError) as error:
        operator.call_tool("grokbot_queue_report", {"job_id": "grokbot-" + "b" * 24})
    assert REPORT_TEXT not in json.dumps(error.value.public_error())


def test_metadata_only_scout_completion_operator_report_is_public_validation(tmp_path: Path):
    job_id = _running_scout_job(tmp_path)
    scout = _adapter(tmp_path, "repository-scout")
    operator = _adapter(tmp_path, "operator")

    completed = scout.call_tool(
        "grokbot_queue_complete",
        {"job_id": job_id, "lease_id": "lease-a", "artifact": _report_artifact()},
    )
    assert completed["state"] == "completed"
    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "completed"

    with pytest.raises(grokbot_mcp.AdapterError) as error:
        operator.call_tool("grokbot_queue_report", {"job_id": job_id})
    assert error.value.public_error() == {
        "error": {"code": "invalid_request", "message": "Tool input failed validation"}
    }


@pytest.mark.parametrize("report_text", [None, [], 42])
def test_direct_complete_rejects_present_non_string_report_text(tmp_path: Path, report_text: object):
    job_id = _running_scout_job(tmp_path)
    scout = _adapter(tmp_path, "repository-scout")

    with pytest.raises(grokbot_mcp.AdapterError):
        scout.call_tool(
            "grokbot_queue_complete",
            {
                "job_id": job_id,
                "lease_id": "lease-a",
                "artifact": _report_artifact(),
                "report_text": report_text,
            },
        )

    assert grokbot_jobs.get_job(tmp_path, job_id)["state"] == "running"


def test_mcp_raw_schema_accepts_report_text_only_on_complete(tmp_path: Path):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient", reason="requires the grokbot extra").TestClient

    headers = {"authorization": "Bearer not-a-real-token", "content-type": "application/json"}
    invalid_calls = (
        ("grokbot_queue_report", {"job_id": "grokbot-" + "a" * 24, "report_text": REPORT_TEXT}),
        (
            "grokbot_queue_complete",
            {"job_id": "grokbot-" + "a" * 24, "lease_id": "lease-a", "artifact": {}, "report_text": []},
        ),
        (
            "grokbot_queue_complete",
            {
                "job_id": "grokbot-" + "a" * 24,
                "lease_id": "lease-a",
                "artifact": {},
                "report_text": REPORT_TEXT,
                "extra": "forbidden",
            },
        ),
    )
    with TestClient(grokbot_mcp.build_app(_adapter(tmp_path).config), base_url="http://127.0.0.1:8766") as client:
        for name, arguments in invalid_calls:
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                headers=headers,
            )
            assert response.status_code == 400
            assert response.json() == {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32602, "message": "Invalid request"},
            }


def _worst_case_escaped_complete_body(report_text: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "grokbot_queue_complete",
                "arguments": {
                    "job_id": "grokbot-" + "a" * 24,
                    "lease_id": "L" * 128,
                    "artifact": {
                        "kind": "report",
                        "path": "p" * 512,
                        "sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
                    },
                    "report_text": report_text,
                },
            },
        }
    ).encode("utf-8")


def test_asgi_admits_worst_case_json_escaped_max_report(tmp_path: Path):
    adapter = _adapter(tmp_path, "repository-scout")
    report_text = "\x01" * grokbot_jobs.MAX_REPORT_BYTES
    body = _worst_case_escaped_complete_body(report_text)
    reached: list[int] = []

    async def downstream(scope, receive, send):
        messages = []
        while True:
            message = await receive()
            messages.append(message)
            if not message.get("more_body", False) or message.get("type") != "http.request":
                break
        reached.append(sum(len(message.get("body", b"")) for message in messages))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def request() -> list[dict[str, object]]:
        pending = iter(
            [
                {"type": "http.request", "body": body, "more_body": False},
            ]
        )
        sent: list[dict[str, object]] = []

        async def receive():
            return next(pending, {"type": "http.disconnect"})

        async def send(message):
            sent.append(message)

        await grokbot_mcp._GateASGI(downstream, grokbot_mcp.RequestGate(adapter.config), adapter)(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": [
                    (b"host", b"127.0.0.1:8766"),
                    (b"authorization", b"Bearer not-a-real-token"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"content-type", b"application/json"),
                ],
            },
            receive,
            send,
        )
        return sent

    assert grokbot_jobs.MAX_REPORT_BYTES == 12000
    assert len(report_text.encode("utf-8")) == grokbot_jobs.MAX_REPORT_BYTES
    assert grokbot_jobs._report_bytes(report_text) == report_text.encode("utf-8")
    assert len(body) <= grokbot_mcp.MAX_REQUEST_BYTES
    sent = asyncio.run(request())
    assert sent[0]["status"] != 413
    assert sent[0]["status"] == 200
    assert reached == [len(body)]


def test_asgi_replay_delegates_to_original_receive_after_buffer(tmp_path: Path):
    adapter = _adapter(tmp_path)
    released = asyncio.Event()
    disconnect = {"type": "http.disconnect"}
    reads = 0
    delegated: list[dict[str, object]] = []

    async def downstream(scope, receive, send):
        first = await receive()
        assert first == {"type": "http.request", "body": b"payload", "more_body": False}
        second_task = asyncio.create_task(receive())
        await asyncio.sleep(0.01)
        assert not second_task.done()
        released.set()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        delegated.append(await second_task)

    async def outer_receive() -> dict[str, object]:
        nonlocal reads
        reads += 1
        if reads == 1:
            return {"type": "http.request", "body": b"payload", "more_body": False}
        await released.wait()
        return disconnect

    async def request() -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await grokbot_mcp._GateASGI(
            downstream,
            grokbot_mcp.RequestGate(adapter.config),
            adapter,
        )(
            {
                "type": "http",
                "method": "POST",
                "path": "/health",
                "headers": [
                    (b"host", b"127.0.0.1:8766"),
                    (b"authorization", b"Bearer not-a-real-token"),
                ],
            },
            outer_receive,
            send,
        )
        return sent

    sent = asyncio.run(request())
    assert sent[0]["status"] == 200
    assert delegated == [disconnect]


def test_cli_serve_reports_missing_optional_extra_without_printing_secret(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    monkeypatch.setattr(grokbot_mcp, "_load_mcp", lambda: (_ for _ in ()).throw(grokbot_mcp.OptionalDependencyError()))

    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "serve",
                "--instance",
                "operator",
                "--target",
                str(tmp_path),
                "--bearer-env",
                "TEST_GROKBOT_BEARER",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "pip install brigade-cli[grokbot]" in captured.err
    assert "not-a-real-token" not in captured.err + captured.out


def test_hub_authority_mcp_does_not_call_generic_fleet_paths(tmp_path: Path, monkeypatch):
    from brigade import fleet_client_grokbot

    job_id = grokbot_jobs.enqueue(tmp_path, _spec("implementation-worker"), "implementation-job")["job_id"]
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    live = {
        "job_id": job_id,
        "state": "claimed",
        "role": "implementation-worker",
        "item_revision": 2,
        "lease_generation": 1,
        "execution_context": _spec("implementation-worker"),
    }
    monkeypatch.setattr(grokbot_jobs, "claim_execution_context", lambda *args, **kwargs: live)
    monkeypatch.setattr(grokbot_jobs, "renew", lambda *args, **kwargs: {**live, "state": "claimed"})
    monkeypatch.setattr(grokbot_jobs, "transition", lambda *args, **kwargs: {**live, "state": args[-1]})
    monkeypatch.setattr(grokbot_jobs, "acknowledge_cancel", lambda *args, **kwargs: {**live, "state": "canceled"})
    monkeypatch.setattr(grokbot_jobs, "complete_report", lambda *args, **kwargs: {**live, "state": "completed"})
    monkeypatch.setattr(
        grokbot_jobs, "get_job", lambda *args, **kwargs: {"job_id": job_id, "role": "implementation-worker"}
    )
    calls: list[str] = []

    def _record(name: str, result: object):
        def _impl(*args, **kwargs):
            calls.append(name)
            return result

        return _impl

    monkeypatch.setattr(fleet_client, "acquire_claim", _record("acquire", fleet_client.ClaimDecision(True, "ok")))
    monkeypatch.setattr(fleet_client, "renew_claim", _record("renew", fleet_client.ClaimDecision(True, "ok")))
    monkeypatch.setattr(fleet_client, "release_claim", _record("release", None))
    monkeypatch.setattr(fleet_client, "admit_cloud", _record("admit", fleet_client.CloudDecision(True, "ok")))
    monkeypatch.setattr(fleet_client, "renew_cloud", _record("cloud-renew", fleet_client.CloudDecision(True, "ok")))
    monkeypatch.setattr(fleet_client, "release_cloud", _record("cloud-release", fleet_client.CloudDecision(True, "ok")))
    monkeypatch.setattr(fleet_client, "bind_cloud", _record("bind", fleet_client.CloudDecision(True, "ok")))
    monkeypatch.setattr(fleet_client, "report_external_event", _record("event", None), raising=False)
    monkeypatch.setattr(fleet_client_grokbot, "hub_configured", lambda: True)
    monkeypatch.setattr(
        fleet_client_grokbot,
        "whoami",
        lambda: fleet_client_grokbot.GrokbotHubDecision(
            True,
            "ok",
            job={"actor_kind": "implementation-worker", "role": "implementation-worker"},
        ),
    )
    adapter = _adapter(tmp_path, hub_token="listener-node-token")
    assert adapter.call_tool("grokbot_queue_claim", {"job_id": job_id, "lease_id": "lease-hub"})["state"] == "claimed"
    assert adapter.call_tool("grokbot_queue_renew", {"job_id": job_id, "lease_id": "lease-hub"})["state"] == "claimed"
    assert adapter.call_tool("grokbot_queue_start", {"job_id": job_id, "lease_id": "lease-hub"})["state"] == "running"
    assert adapter.call_tool("grokbot_queue_fail", {"job_id": job_id, "lease_id": "lease-hub"})["state"] == "failed"
    assert (
        adapter.call_tool("grokbot_queue_ack_cancel", {"job_id": job_id, "lease_id": "lease-hub"})["state"]
        == "canceled"
    )
    assert (
        adapter.call_tool(
            "grokbot_queue_complete",
            {"job_id": job_id, "lease_id": "lease-hub", "artifact": {"kind": "draft-pr"}},
        )["state"]
        == "completed"
    )
    assert (
        adapter.call_tool(
            "grokbot_queue_complete",
            {
                "job_id": job_id,
                "lease_id": "lease-hub",
                "artifact": {"kind": "report"},
                "report_text": "private",
            },
        )["state"]
        == "completed"
    )
    assert calls == []


def _write_hub_token_file(path: Path, token: str) -> Path:
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_three_listeners_use_distinct_hub_node_tokens_and_public_args_cannot_override(tmp_path: Path, monkeypatch):
    from brigade import fleet_client_grokbot

    tokens = {
        "operator": "operator-node-token",
        "repository-scout": "scout-node-token",
        "implementation-worker": "worker-node-token",
    }
    seen: list[str] = []

    def _whoami():
        token = fleet_client_grokbot.current_listener_token()
        seen.append(token or "")
        kind = {
            "operator-node-token": "operator",
            "scout-node-token": "repository-scout",
            "worker-node-token": "implementation-worker",
        }[token or ""]
        return fleet_client_grokbot.GrokbotHubDecision(
            True,
            "ok",
            job={"actor_kind": kind, "role": None if kind == "operator" else kind},
        )

    monkeypatch.setattr(fleet_client_grokbot, "whoami", _whoami)
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub", "token": "machine-token"})
    for instance, token in tokens.items():
        token_file = _write_hub_token_file(tmp_path / f"{instance}.hub-token", token)
        monkeypatch.setenv(f"BRIGADE_GROKBOT_{instance.replace('-', '_').upper()}_HUB_TOKEN_FILE", str(token_file))
        bearer = tmp_path / f"{instance}.bearer"
        bearer.write_text("not-a-real-token\n", encoding="utf-8")
        bearer.chmod(0o600)
        config = grokbot_mcp.build_listener_config(
            target=tmp_path,
            instance=instance,
            bind="127.0.0.1:8766",
            allowed_hosts=[],
            allowed_origins=[],
            bearer_file=bearer,
            bearer_env=None,
        )
        assert config.hub_token == token
        adapter = grokbot_mcp.GrokbotAdapter(config)
        adapter.ensure_hub_actor()
        with pytest.raises(grokbot_mcp.AdapterError):
            adapter.call_tool("grokbot_queue_status", {"job_id": "grokbot-" + "a" * 24, "hub_token": "stolen"})
    assert seen == list(tokens.values())
    assert fleet_client.load_fleet_config()["token"] == "machine-token"


def test_mismatched_hub_actor_credential_refuses_listener_service(tmp_path: Path, monkeypatch):
    from brigade import fleet_client_grokbot

    token_file = _write_hub_token_file(tmp_path / "wrong.hub-token", "wrong-actor-token")
    monkeypatch.setenv("BRIGADE_GROKBOT_IMPLEMENTATION_WORKER_HUB_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda _target=None: True)
    monkeypatch.setattr(
        fleet_client_grokbot,
        "whoami",
        lambda: fleet_client_grokbot.GrokbotHubDecision(True, "ok", job={"actor_kind": "operator", "role": None}),
    )
    bearer = tmp_path / "bearer"
    bearer.write_text("not-a-real-token\n", encoding="utf-8")
    bearer.chmod(0o600)
    config = grokbot_mcp.build_listener_config(
        target=tmp_path,
        instance="implementation-worker",
        bind="127.0.0.1:8766",
        allowed_hosts=[],
        allowed_origins=[],
        bearer_file=bearer,
        bearer_env=None,
    )
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_mcp.GrokbotAdapter(config).ensure_hub_actor()
