"""Bound cloud leases outlive the 5 minute admission TTL (#1794 item 1)."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from brigade import care_cmd, cloud_tracker, codex_cloud, fleet_client, fleet_command_deck as deck, fleet_hub, proc
from brigade.cli import main
from brigade.inbox_lock import held_file_lock


NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
CLOCK_START = 1_700_000_000.0


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    yield connection
    connection.close()


@pytest.fixture()
def clock(monkeypatch):
    state = {"now": CLOCK_START}
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: state["now"])
    return state


def _admit(lease_id: str = "lease-a", **extra: object) -> dict[str, object]:
    return {
        "action": "admit",
        "provider": "cursor",
        "lease_id": lease_id,
        "node_id": NODE_A,
        "holder": "holder-a",
        "repo": "repo-a",
        "label": "safe label",
        "prompt_hash": "a" * 64,
        **extra,
    }


def _bind(lease_id: str = "lease-a", **extra: object) -> dict[str, object]:
    return {
        "action": "bind",
        "lease_id": lease_id,
        "node_id": NODE_A,
        "holder": "holder-a",
        "provider_task_id": "task-1",
        **extra,
    }


def _handle(conn, body: dict[str, object]):
    return fleet_hub.handle_cloud(conn, body, caller_node=NODE_A, config=deck.DeckConfig())


def _admitted(conn, **extra: object) -> dict[str, Any]:
    status, payload = _handle(conn, _admit(ttl_seconds=300, **extra))
    assert status == 200 and payload["admitted"] is True
    return payload["lease"]


def test_bind_with_ttl_extends_the_lease(conn, clock):
    admitted = _admitted(conn)
    clock["now"] += 100
    status, payload = _handle(conn, _bind(ttl_seconds=21600))
    assert status == 200 and payload["bound"] is True
    lease = payload["lease"]
    assert lease["state"] == "bound"
    assert lease["ttl_seconds"] == 21600
    assert lease["renewed_at"] != admitted["renewed_at"]
    assert lease["expires_at"] == fleet_hub._epoch_to_iso(CLOCK_START + 100 + 21600)
    # Still visible long after the original 5 minute window closed.
    clock["now"] += 3600
    assert [row["lease_id"] for row in fleet_hub.list_cloud_leases(conn)] == ["lease-a"]


def test_bind_without_ttl_leaves_the_lease_clock_untouched(conn, clock):
    admitted = _admitted(conn)
    clock["now"] += 100
    status, payload = _handle(conn, _bind())
    assert status == 200 and payload["bound"] is True
    lease = payload["lease"]
    assert lease["ttl_seconds"] == 300
    assert lease["renewed_at"] == admitted["renewed_at"]
    assert lease["expires_at"] == admitted["expires_at"]


@pytest.mark.parametrize("bad", [0, -1, 86401, "3600", 3600.5, True, None])
def test_bind_rejects_an_invalid_ttl(conn, clock, bad):
    _admitted(conn)
    with pytest.raises(fleet_hub.FleetHubError, match="ttl_seconds"):
        _handle(conn, _bind(ttl_seconds=bad))


def test_bind_with_ttl_still_refuses_an_expired_lease(conn, clock):
    _admitted(conn)
    clock["now"] += 301
    status, payload = _handle(conn, _bind(ttl_seconds=21600))
    assert status == 409 and payload["bound"] is False
    assert fleet_hub.list_cloud_leases(conn) == []


def test_bind_with_ttl_still_refuses_a_released_or_fenced_lease(conn, clock):
    _admitted(conn)
    status, payload = _handle(conn, _bind(ttl_seconds=21600, holder="intruder"))
    assert status == 409 and payload["bound"] is False
    status, payload = _handle(conn, {**_bind(), "action": "release", "state": "released"})
    assert status == 200 and payload["released"] is True
    status, payload = _handle(conn, _bind(ttl_seconds=21600))
    assert status == 409 and payload["bound"] is False


def test_older_hub_parser_ignores_unknown_bind_fields(conn, clock):
    """Pins why no client retry exists: an older hub never rejects ``ttl_seconds`` on bind.

    The request parser reads named fields only, so a bind carrying an unknown
    key validates exactly like one without it. An older hub therefore binds and
    keeps the admission TTL instead of failing the launch.
    """
    _admitted(conn)
    request = fleet_hub._validate_cloud_request({**_bind(), "field_from_a_newer_client": 7})
    assert request["action"] == "bind" and request["provider_task_id"] == "task-1"
    assert "field_from_a_newer_client" not in request


def _capture_bind_bodies(monkeypatch) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def capture_post(_hub, _token, body, *, timeout):
        bodies.append(dict(body))
        return 200, {"bound": True, "lease": {"lease_id": body["lease_id"], "provider": "cursor"}}

    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub", "token": "token"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda _base=None: NODE_A)
    monkeypatch.setattr(fleet_client, "_post_cloud_blocking", capture_post)
    return bodies


def test_bind_cloud_sends_ttl_only_when_given(monkeypatch):
    bodies = _capture_bind_bodies(monkeypatch)
    assert fleet_client.bind_cloud("lease-a", "task-1", holder="holder-a").granted is True
    assert "ttl_seconds" not in bodies[0]
    assert fleet_client.bind_cloud("lease-a", "task-1", holder="holder-a", ttl_seconds=21600).granted is True
    assert bodies[1]["ttl_seconds"] == 21600


def test_bound_ttl_constant_matches_the_tracker_stale_threshold():
    assert fleet_client.CLOUD_BOUND_TTL_SECONDS == 21600
    assert fleet_client.CLOUD_BOUND_TTL_SECONDS == cloud_tracker.DEFAULT_STALE_READY_HOURS * 3600


def test_sync_renews_active_leases_with_the_bound_ttl(monkeypatch, tmp_path: Path):
    now = cloud_tracker._parse_time("2026-08-12T20:00:00Z")
    cloud_tracker.register(
        tmp_path,
        provider="cursor-cloud",
        task_id="sess-long",
        label="cursor-cloud:owner/repo@001122334455",
        prompt_hash="sha256:" + "b" * 64,
        lease_holder="holder-a",
        dispatched_at="2026-08-12T19:59:00Z",
    )
    renew_calls: list[dict[str, Any]] = []

    def capture_renew(lease_id: str, **kwargs: Any):
        renew_calls.append({"lease_id": lease_id, **kwargs})
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "renew_cloud", capture_renew)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=now,
        provider_tasks={"sess-long": {"state": "running"}},
        github={"branches": [], "prs": []},
        hub_leases=[{"lease_id": "lease-long", "provider_task_id": "sess-long"}],
    )
    assert payload["counts"]["renewed"] == 1
    assert renew_calls == [
        {"lease_id": "lease-long", "holder": "holder-a", "ttl_seconds": fleet_client.CLOUD_BOUND_TTL_SECONDS}
    ]


@pytest.fixture()
def codex_sync(monkeypatch, tmp_path, conn, clock, capsys):
    task_id = "task_123"
    cloud_tracker.register(
        tmp_path, provider="codex-cloud", task_id=task_id, label="tracked task", lease_holder="holder-a"
    )
    _admitted(conn, provider="codex")
    assert _handle(conn, _bind(provider_task_id=task_id))[0] == 200
    mutations = []

    def post(_hub, _token, body, *, timeout):
        mutations.append(body["action"])
        return _handle(conn, body)

    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub", "token": "token"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda _base=None: NODE_A)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda fn, *, timeout: fn())
    monkeypatch.setattr(fleet_client, "fetch_cloud", lambda: {"leases": fleet_hub.list_cloud_leases(conn)})
    monkeypatch.setattr(fleet_client, "_post_cloud_blocking", post)
    monkeypatch.setattr(codex_cloud.time, "time", lambda: clock["now"])
    elapsed = 0.0

    def sync(
        state: str,
        *,
        stderr: str = "",
        probe_seconds: int | Iterator[int] = 0,
        code: int = 0,
        timed_out: bool = False,
        missing: bool = False,
    ):
        def status(argv, **kwargs):
            nonlocal elapsed
            assert argv == ["codex", "cloud", "status", task_id]
            duration = probe_seconds if isinstance(probe_seconds, int) else next(probe_seconds)
            clock["now"] += duration
            elapsed += duration
            if missing:
                raise FileNotFoundError("codex")
            return proc.Result(code, state, stderr, timed_out=timed_out)

        monkeypatch.setattr(codex_cloud.time, "monotonic", lambda: elapsed)
        monkeypatch.setattr(codex_cloud.proc, "run", status)
        rc = main(["run", "cloud", "sync", "--codex-leases-only", "--target", str(tmp_path), "--json"])
        return rc, json.loads(capsys.readouterr().out)

    return sync, mutations


def test_scheduled_codex_sync_renews_until_terminal(conn, clock, codex_sync):
    sync, mutations = codex_sync
    clock["now"] += 240
    assert sync("[RUNNING]")[1]["renewed"] == 1
    clock["now"] += 61
    assert [row["lease_id"] for row in fleet_hub.list_cloud_leases(conn)] == ["lease-a"]
    clock["now"] = CLOCK_START + fleet_client.CLOUD_BOUND_TTL_SECONDS
    assert sync("[RUNNING]")[1]["renewed"] == 1
    assert sync("[ERROR] task failed")[1]["released"] == 1
    assert sync("[RUNNING]")[1]["renewed"] == 0
    assert mutations == ["renew", "renew", "release"]
    assert fleet_hub.list_cloud_leases(conn) == []


def test_scheduled_codex_sync_stops_renewing_without_fresh_status(conn, clock, codex_sync):
    sync, mutations = codex_sync
    clock["now"] += 240
    assert sync("[RUNNING]")[1]["renewed"] == 1
    clock["now"] += 120
    assert sync("authentication failed", code=1)[0] == 1
    clock["now"] = CLOCK_START + 240 + fleet_client.CLOUD_BOUND_TTL_SECONDS
    assert fleet_hub.list_cloud_leases(conn) == []
    assert sync("[RUNNING]")[1]["renewed"] == 0
    assert mutations == ["renew"]


def test_scheduled_codex_sync_lease_expiring_during_probe_is_not_renewed(conn, clock, codex_sync):
    sync, mutations = codex_sync
    clock["now"] += 299
    assert sync("[RUNNING]", probe_seconds=1)[1]["renewed"] == 0
    assert mutations == []
    assert fleet_hub.list_cloud_leases(conn) == []


def test_scheduled_codex_sync_ignores_stderr_as_task_status(codex_sync):
    sync, mutations = codex_sync
    rc, result = sync("Status: RUNNING", stderr="[ERROR] connection interrupted")
    assert rc == 0 and result["renewed"] == 1 and result["released"] == 0
    assert mutations == ["renew"]


@pytest.mark.parametrize(
    ("stdout", "options"),
    [
        ("[COMPLETED]", {"stderr": "[ERROR] authentication failed"}),
        ("[COMPLETED]", {"code": 1}),
        ("[ERROR]", {"code": 1}),
        ("[COMPLETED]", {"timed_out": True}),
        ("", {"missing": True}),
        ("unknown output", {"stderr": "[ERROR] connection interrupted"}),
    ],
)
def test_scheduled_codex_sync_keeps_lease_on_bad_provider_result(codex_sync, stdout, options):
    sync, mutations = codex_sync
    rc, result = sync(stdout, **options)
    assert rc == 1 and result["errors"] == 1 and result["released"] == 0
    assert mutations == []


@pytest.mark.parametrize("operation", ["fetch", "renew"])
def test_scheduled_codex_sync_keeps_lease_when_hub_is_unavailable(monkeypatch, codex_sync, operation):
    sync, mutations = codex_sync
    if operation == "fetch":

        def unavailable():
            raise fleet_client.FleetClientError("unavailable")

        monkeypatch.setattr(fleet_client, "fetch_cloud", unavailable)
    else:
        monkeypatch.setattr(
            fleet_client, "renew_cloud", lambda *args, **kwargs: fleet_client.CloudDecision(False, "hub-unavailable")
        )
    rc, result = sync("[RUNNING]")
    assert rc == 1 and result["errors"] == 1 and result["released"] == 0
    assert mutations == []


def test_scheduled_codex_sync_fails_on_unclaimable_node(monkeypatch, codex_sync):
    sync, mutations = codex_sync
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "unknown")
    rc, result = sync("[COMPLETED]")
    assert rc == 1 and result["errors"] == 1 and result["reason"] == "no-identity"
    assert mutations == []


def test_scheduled_codex_sync_reports_foreign_leases(monkeypatch, conn, codex_sync):
    sync, mutations = codex_sync
    lease = {**fleet_hub.list_cloud_leases(conn)[0], "owner_node": NODE_B}
    monkeypatch.setattr(fleet_client, "fetch_cloud", lambda: {"leases": [lease]})
    rc, result = sync("[COMPLETED]")
    assert rc == 0 and result["skipped"] == 1 and result["reason"] == "no-matching-node"
    assert mutations == []


def test_scheduled_codex_sync_leaves_unbound_leases_alone(monkeypatch, conn, codex_sync):
    sync, mutations = codex_sync
    lease = {**fleet_hub.list_cloud_leases(conn)[0], "state": "admitted"}
    monkeypatch.setattr(fleet_client, "fetch_cloud", lambda: {"leases": [lease]})
    rc, result = sync("[COMPLETED]")
    assert rc == 0 and result["released"] == 0
    assert mutations == []


def test_scheduled_codex_sync_skips_overlapping_run(monkeypatch, tmp_path, codex_sync):
    sync, mutations = codex_sync
    monkeypatch.setattr(fleet_client, "fetch_cloud", lambda: pytest.fail("overlapping sync fetched leases"))
    with held_file_lock(tmp_path / ".brigade/cloud/codex-sync.lock", deadline_seconds=0):
        rc, result = sync("[COMPLETED]")
    assert rc == 0 and result["skipped"] == 1 and result["reason"] == "already-running"
    assert mutations == []


def test_scheduled_codex_sync_budget_discards_terminal_results_and_stops(monkeypatch, conn, codex_sync):
    sync, mutations = codex_sync
    lease = fleet_hub.list_cloud_leases(conn)[0]
    leases = [{**lease, "lease_id": lease_id} for lease_id in ("earlier", "late", "unprobed")]
    monkeypatch.setattr(fleet_client, "fetch_cloud", lambda: {"leases": leases})
    probes = iter([5, 105])
    # The first verified terminal observation fits, but the second finishes
    # after the scan deadline. The third task must never be probed.
    rc, result = sync("[COMPLETED]", probe_seconds=probes)
    assert rc == 1 and result["reason"] == "budget-exceeded" and result["released"] == 0
    assert list(probes) == []
    assert mutations == []


def test_codex_cloud_sync_care_entry_is_opt_in_and_schedules_the_lease_only_command(tmp_path, monkeypatch, capsys):
    from brigade import runbook_cmd

    monkeypatch.setattr(runbook_cmd, "_resolve_step_argv0", lambda command, cwd: sys.executable)
    assert "codex-cloud-sync" not in {entry.entry_id for entry in care_cmd.CARE_ENTRIES}
    assert care_cmd.install(target=tmp_path, backend="schtasks", entry_ids=["codex-cloud-sync"], json_output=True) == 3
    plan = json.loads(capsys.readouterr().out)
    assert plan["mutates_scheduler"] is False
    assert plan["tasks"][0]["schedule_flags"] == "/SC MINUTE /MO 2"
    runbook = json.loads((tmp_path / ".brigade/runbooks/codex-cloud-sync.json").read_text())
    assert runbook["steps"][0]["run"] == "brigade run cloud sync --codex-leases-only --target . --json"


@pytest.mark.parametrize("entry_ids", [None, ["codex-cloud-sync"]])
def test_codex_cloud_sync_systemd_install_plan_is_opt_in(tmp_path, capsys, entry_ids):
    rc = care_cmd.install(
        target=tmp_path, backend="systemd", dry_run=True, json_output=True, home=tmp_path / "home", entry_ids=entry_ids
    )
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    sync_units = [unit["name"] for unit in plan["units"] if "-codex-cloud-sync." in unit["name"]]
    assert sorted(name.rsplit(".", 1)[1] for name in sync_units) == (["service", "timer"] if entry_ids else [])
