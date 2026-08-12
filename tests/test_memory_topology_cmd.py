"""Tests for `brigade memory topology --json` (#875 Task 1)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import cli


NODE_FIELDS = {
    "id",
    "kind",
    "label",
    "owner",
    "authority",
    "state",
    "path",
    "schedule",
    "counts",
    "latest_run",
    "next_action",
}
EDGE_FIELDS = {
    "id",
    "from",
    "to",
    "flow",
    "authority",
    "writer_component",
    "latest_receipt",
    "enabled",
}
HEALTH_BLOCKS = {
    "care_scan",
    "refresh_queue",
    "handoff_backlog",
    "quarantine",
    "closeout",
    "evidence_projection",
    "config",
}
CANONICAL_KINDS = {"cards", "rules", "tools", "user", "learnings"}
FORBIDDEN_CONTENT_KEYS = {"body", "prompt", "content", "summary", "cron", "environment", "hostname"}


def _write_config(target: Path, *, harnesses: list[str], owner: str | None = None) -> None:
    root = target / ".brigade"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": harnesses,
                "owner": owner or harnesses[0],
                "includes": [],
            }
        )
        + "\n"
    )


def _write_handoff(inbox: Path, name: str = "2026-08-11-example.md") -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(
        "\n".join(
            [
                "# Handoff",
                "",
                "## Summary",
                "Fixture handoff for topology tests.",
                "",
                "## Durable Knowledge",
                "- example",
                "",
            ]
        )
    )
    return path


def _stub_evidence(monkeypatch) -> None:
    """Avoid engine probes; tolerate RED when the module is not implemented yet."""
    try:
        from brigade import memory_operations
    except ImportError:
        return
    monkeypatch.setattr(
        memory_operations.evidence_cmd,
        "status_payload",
        lambda target, **kwargs: {
            "health": "missing",
            "memory_projection": {"healthy": False, "status": "absent", "capability": None},
        },
    )


def _stub_care_disabled(monkeypatch) -> None:
    from brigade import care_cmd, memory_operations

    def _disabled_status(*, target, home=None):
        return {
            "enabled": False,
            "target_match": False,
            "backends": {
                "crontab": {"status": "missing", "target_match": False},
                "systemd": {"status": "missing", "target_match": False},
            },
            "entries": [
                {
                    "id": entry.entry_id,
                    "schedule": entry.schedule,
                    "on_calendar": entry.on_calendar,
                    "kind": entry.kind,
                    "description": entry.description,
                    "runbook": entry.runbook_rel,
                    "runbook_id": entry.runbook_id,
                    "runbook_present": False,
                    "last_receipt": None,
                }
                for entry in care_cmd.CARE_ENTRIES
            ],
        }

    monkeypatch.setattr(memory_operations.care_cmd, "status_payload", _disabled_status)


@pytest.fixture(autouse=True)
def _topology_host_independent_care(monkeypatch, request):
    if request.node.get_closest_marker("care_host_integration"):
        return
    _stub_care_disabled(monkeypatch)


def _run_topology(target: Path, capsys) -> dict:
    rc = cli.main(["memory", "topology", "--target", str(target), "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def _assert_safe_paths(payload: dict) -> None:
    dumped = json.dumps(payload)
    assert "/home/" not in dumped
    for node in payload["nodes"]:
        path = node.get("path")
        if isinstance(path, str) and path:
            assert not path.startswith("/"), path
            assert "\\" not in path
    for key in FORBIDDEN_CONTENT_KEYS:
        assert key not in payload


def test_memory_topology_schema_and_required_fields(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    payload = _run_topology(tmp_path, capsys)

    assert payload["schema_version"] == 1
    assert payload["schema"] == {"name": "memory-topology", "version": 1}
    assert payload["target"] == "."
    assert isinstance(payload["generated_at"], str) and payload["generated_at"]
    assert isinstance(payload["nodes"], list) and payload["nodes"]
    assert isinstance(payload["edges"], list) and payload["edges"]
    assert isinstance(payload["paths"], list)
    assert isinstance(payload["flags"], list)
    assert set(payload["health"]) == HEALTH_BLOCKS
    for node in payload["nodes"]:
        assert NODE_FIELDS <= set(node)
    for edge in payload["edges"]:
        assert EDGE_FIELDS <= set(edge)
    _assert_safe_paths(payload)


def test_memory_topology_includes_configured_harness_to_canonical_path(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude", "codex"])
    _write_handoff(tmp_path / ".claude" / "memory-handoffs")

    payload = _run_topology(tmp_path, capsys)

    harness_ids = {p["harness"] for p in payload["paths"]}
    assert "claude" in harness_ids
    assert "codex" in harness_ids
    assert "cursor" not in harness_ids

    claude_path = next(p for p in payload["paths"] if p["harness"] == "claude")
    node_ids = claude_path["node_ids"]
    assert node_ids[0].startswith("harness:")
    assert any(n.startswith("inbox:") for n in node_ids)
    assert "stage:lint" in node_ids
    assert "stage:ingest" in node_ids
    for kind in CANONICAL_KINDS:
        assert f"canonical:{kind}" in node_ids

    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["canonical:cards"]["state"] == "canonical"
    assert by_id["canonical:cards"]["path"] == "memory/cards"
    assert by_id["canonical:rules"]["path"] == "rules"
    assert by_id["canonical:tools"]["path"] == "TOOLS.md"
    assert by_id["canonical:user"]["path"] == "USER.md"
    assert by_id["canonical:learnings"]["path"] == ".learnings"


def test_memory_topology_present_unconfigured_writer_gets_path(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    _write_handoff(tmp_path / ".cursor" / "memory-handoffs")

    payload = _run_topology(tmp_path, capsys)
    harness_ids = {p["harness"] for p in payload["paths"]}
    assert "claude" in harness_ids
    assert "cursor" in harness_ids


def test_memory_topology_explicit_missing_owner_and_missing_receipt(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    sources = tmp_path / ".brigade" / "handoff-sources.json"
    sources.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "root": ".",
                        "inboxes": [".claude/memory-handoffs", ".unknown-adapter/memory-handoffs"],
                    }
                ],
            }
        )
        + "\n"
    )
    (tmp_path / ".unknown-adapter" / "memory-handoffs").mkdir(parents=True)

    payload = _run_topology(tmp_path, capsys)

    unknown_nodes = [n for n in payload["nodes"] if n.get("owner") == "unknown"]
    assert unknown_nodes, "explicit unknown owners must remain visible"
    mutating = [
        e
        for e in payload["edges"]
        if e.get("authority") in {"canonical_writer", "derived_writer"} or e.get("flow") in {"write", "mutate"}
    ]
    assert mutating
    assert any(
        isinstance(e.get("latest_receipt"), dict) and e["latest_receipt"].get("evidence_state") == "missing"
        for e in mutating
    )
    assert "missing_owner" in {f.get("kind") for f in payload["flags"]}


def test_memory_topology_disabled_care_jobs_visible_without_duplicate_paths(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    payload = _run_topology(tmp_path, capsys)

    care_nodes = [n for n in payload["nodes"] if n.get("kind") == "care_job"]
    assert care_nodes
    assert all(n.get("state") in {"disabled", "enabled", "missing"} for n in care_nodes)
    assert any(n.get("state") == "disabled" for n in care_nodes)

    path_ids = [p["id"] for p in payload["paths"]]
    assert len(path_ids) == len(set(path_ids))
    assert all(not pid.startswith("care-path:") for pid in path_ids)


def test_memory_topology_flags_duplicate_enabled_writers():
    from brigade import memory_operations

    nodes = [
        {
            "id": "canonical:cards",
            "kind": "canonical",
            "label": "cards",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "canonical",
            "path": "memory/cards",
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "writer:a",
            "kind": "stage",
            "label": "a",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "writer:b",
            "kind": "stage",
            "label": "b",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
    ]
    edges = [
        {
            "id": "e1",
            "from": "writer:a",
            "to": "canonical:cards",
            "flow": "write",
            "authority": "canonical_writer",
            "writer_component": "ingest",
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": True,
        },
        {
            "id": "e2",
            "from": "writer:b",
            "to": "canonical:cards",
            "flow": "write",
            "authority": "canonical_writer",
            "writer_component": "care-scan",
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": True,
        },
        {
            "id": "e3",
            "from": "writer:a",
            "to": "canonical:cards",
            "flow": "write",
            "authority": "canonical_writer",
            "writer_component": "disabled-job",
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": False,
        },
    ]
    flags = memory_operations.detect_flags(nodes, edges, health={})
    kinds = {f["kind"] for f in flags}
    assert "duplicate_enabled_writer" in kinds
    dup = next(f for f in flags if f["kind"] == "duplicate_enabled_writer")
    assert dup["destination"] == "canonical:cards"
    assert set(dup["writers"]) == {"ingest", "care-scan"}


def test_memory_topology_flags_stale_receipt():
    from brigade import memory_operations

    stale_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    nodes = [
        {
            "id": "stage:ingest",
            "kind": "stage",
            "label": "ingest",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "canonical:cards",
            "kind": "canonical",
            "label": "cards",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "canonical",
            "path": "memory/cards",
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
    ]
    edges = [
        {
            "id": "e1",
            "from": "stage:ingest",
            "to": "canonical:cards",
            "flow": "write",
            "authority": "canonical_writer",
            "writer_component": "ingest",
            "latest_receipt": {"evidence_state": "present", "status": "ok", "completed_at": stale_at},
            "enabled": True,
        }
    ]
    flags = memory_operations.detect_flags(nodes, edges, health={})
    assert "stale_receipt" in {f["kind"] for f in flags}


def test_memory_topology_stale_receipt_dedup_across_fanout_edges():
    from brigade import memory_operations

    stale_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    shared_receipt = {
        "evidence_state": "present",
        "status": "ok",
        "completed_at": stale_at,
        "run_id": "ingest-run-shared",
    }
    other_receipt = {
        "evidence_state": "present",
        "status": "ok",
        "completed_at": stale_at,
        "run_id": "ingest-run-other",
    }
    nodes = [
        {
            "id": "stage:ingest",
            "kind": "stage",
            "label": "ingest",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "stage:care_scan",
            "kind": "stage",
            "label": "care scan",
            "owner": "brigade",
            "authority": "queue_producer",
            "state": "enabled",
            "path": ".brigade/memory-care",
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "care_job:daily-care",
            "kind": "care_job",
            "label": "daily care",
            "owner": "scheduler",
            "authority": "queue_producer",
            "state": "enabled",
            "path": ".brigade/memory-care/runbooks/daily-care-pass.json",
            "schedule": {"source": "brigade_care", "cadence": "daily"},
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "canonical:cards",
            "kind": "canonical",
            "label": "cards",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "canonical",
            "path": "memory/cards",
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "canonical:rules",
            "kind": "canonical",
            "label": "rules",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "canonical",
            "path": "rules",
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
    ]
    ingest_edges = [
        {
            "id": f"edge:ingest:write:{dest}",
            "from": "stage:ingest",
            "to": f"canonical:{dest}",
            "flow": "write",
            "authority": "canonical_writer",
            "writer_component": "ingest",
            "latest_receipt": shared_receipt,
            "enabled": True,
        }
        for dest in ("cards", "rules", "tools", "user", "learnings")
    ]
    scan_receipt = dict(shared_receipt)
    scan_receipt["run_id"] = "care-scan-shared"
    care_edges = [
        {
            "id": "edge:care_job:daily-care",
            "from": "care_job:daily-care",
            "to": "stage:care_scan",
            "flow": "schedule",
            "authority": "queue_producer",
            "writer_component": "daily-care",
            "latest_receipt": scan_receipt,
            "enabled": True,
        },
        {
            "id": "edge:care_job:daily-care:queue",
            "from": "care_job:daily-care",
            "to": "stage:refresh_queue",
            "flow": "schedule",
            "authority": "queue_producer",
            "writer_component": "daily-care",
            "latest_receipt": scan_receipt,
            "enabled": True,
        },
    ]
    distinct_run_edge = {
        "id": "edge:ingest:write:tools-distinct",
        "from": "stage:ingest",
        "to": "canonical:tools",
        "flow": "write",
        "authority": "canonical_writer",
        "writer_component": "ingest",
        "latest_receipt": other_receipt,
        "enabled": True,
    }
    edges = [*ingest_edges, *care_edges, distinct_run_edge]
    flags = memory_operations.detect_flags(nodes, edges, health={})
    stale_flags = [f for f in flags if f.get("kind") == "stale_receipt"]
    ingest_flags = [f for f in stale_flags if f.get("writer_component") == "ingest"]
    assert len(ingest_flags) == 2
    shared_flag = next(f for f in ingest_flags if f.get("edge_id") == "edge:ingest:write:cards")
    assert shared_flag["edge_ids"] == sorted(e["id"] for e in ingest_edges)
    other_flag = next(f for f in ingest_flags if f.get("edge_id") == "edge:ingest:write:tools-distinct")
    assert other_flag.get("edge_ids") == ["edge:ingest:write:tools-distinct"]
    scan_flags = [f for f in stale_flags if f.get("writer_component") == "daily-care"]
    assert len(scan_flags) == 1
    assert scan_flags[0]["edge_id"] == "edge:care_job:daily-care"
    assert scan_flags[0]["edge_ids"] == sorted(e["id"] for e in care_edges)


def test_memory_topology_on_demand_schedule_skips_stale_receipt():
    from brigade import memory_operations

    stale_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    receipt = {"evidence_state": "present", "status": "ok", "completed_at": stale_at, "run_id": "manual-scan"}
    nodes = [
        {
            "id": "stage:ingest",
            "kind": "stage",
            "label": "ingest",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "stage:care_scan",
            "kind": "stage",
            "label": "care scan",
            "owner": "brigade",
            "authority": "queue_producer",
            "state": "manual",
            "path": ".brigade/memory-care",
            "schedule": {"source": "manual", "cadence": "on_demand"},
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "stage:manual_refresh",
            "kind": "manual_writer",
            "label": "manual refresh",
            "owner": "manual process",
            "authority": "canonical_writer",
            "state": "manual",
            "path": ".brigade/memory-care",
            "schedule": {"source": "manual", "cadence": "on_demand"},
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "canonical:cards",
            "kind": "canonical",
            "label": "cards",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "canonical",
            "path": "memory/cards",
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
    ]
    edges = [
        {
            "id": "edge:ingest:write:cards",
            "from": "stage:ingest",
            "to": "canonical:cards",
            "flow": "write",
            "authority": "canonical_writer",
            "writer_component": "ingest",
            "latest_receipt": receipt,
            "enabled": True,
        },
        {
            "id": "edge:care_scan:queue",
            "from": "stage:care_scan",
            "to": "stage:refresh_queue",
            "flow": "enqueue",
            "authority": "queue_producer",
            "writer_component": "memory-care-scan",
            "latest_receipt": receipt,
            "enabled": True,
        },
        {
            "id": "edge:cards:care_scan",
            "from": "canonical:cards",
            "to": "stage:care_scan",
            "flow": "scan",
            "authority": "read_only",
            "writer_component": "memory-care-scan",
            "latest_receipt": receipt,
            "enabled": True,
        },
        {
            "id": "edge:manual_refresh:cards",
            "from": "stage:manual_refresh",
            "to": "canonical:cards",
            "flow": "mutate",
            "authority": "canonical_writer",
            "writer_component": "memory-care-backfill",
            "latest_receipt": receipt,
            "enabled": True,
        },
    ]
    flags = memory_operations.detect_flags(nodes, edges, health={})
    stale_flags = [f for f in flags if f.get("kind") == "stale_receipt"]
    assert len(stale_flags) == 1
    assert stale_flags[0]["edge_id"] == "edge:ingest:write:cards"


def test_memory_topology_flags_cycles_without_suppressing_graph():
    from brigade import memory_operations

    nodes = [
        {
            "id": "a",
            "kind": "stage",
            "label": "a",
            "owner": "brigade",
            "authority": "read_only",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "b",
            "kind": "stage",
            "label": "b",
            "owner": "brigade",
            "authority": "read_only",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
    ]
    edges = [
        {
            "id": "e1",
            "from": "a",
            "to": "b",
            "flow": "route",
            "authority": "read_only",
            "writer_component": None,
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": True,
        },
        {
            "id": "e2",
            "from": "b",
            "to": "a",
            "flow": "route",
            "authority": "read_only",
            "writer_component": None,
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": True,
        },
    ]
    flags = memory_operations.detect_flags(nodes, edges, health={})
    assert "cycle" in {f["kind"] for f in flags}
    assert len(nodes) == 2
    assert len(edges) == 2


def test_memory_topology_health_blocks(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    assert cli.main(["memory", "care", "init", "--target", str(tmp_path)]) == 0
    capsys.readouterr()

    payload = _run_topology(tmp_path, capsys)
    health = payload["health"]
    assert set(health) == HEALTH_BLOCKS
    for key in HEALTH_BLOCKS:
        assert isinstance(health[key], dict)
        assert "status" in health[key]
    assert health["quarantine"]["status"] == "unknown"
    assert health["quarantine"]["count"] is None
    assert health["evidence_projection"]["status"] in {"absent", "missing", "unknown", "ok", "warn", "fail"}


def test_memory_topology_human_output_concise(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    rc = cli.main(["memory", "topology", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory topology" in out.lower()
    assert "nodes=" in out
    assert "edges=" in out


@pytest.mark.care_host_integration
def test_memory_topology_care_enabled_via_crontab_status_helper(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    store = {"text": ""}

    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: (store["text"], None))
    monkeypatch.setattr(care_cmd, "_write_crontab", lambda text: store.__setitem__("text", text) or None)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_systemd_user_dir", lambda home=None: tmp_path / "no-systemd")

    assert care_cmd.install(target=tmp_path, backend="crontab", json_output=True) == 0
    capsys.readouterr()
    assert "# BEGIN BRIGADE CARE" in store["text"]
    assert "BrigadeCare" not in store["text"]

    status = care_cmd.status_payload(target=tmp_path)
    assert status["enabled"] is True
    assert status["backends"]["crontab"]["status"] == "current"

    monkeypatch.setattr(memory_operations.care_cmd, "status_payload", care_cmd.status_payload)
    payload = _run_topology(tmp_path, capsys)
    care_jobs = [n for n in payload["nodes"] if n.get("kind") == "care_job"]
    assert care_jobs
    # Scheduler installed => jobs are enabled (or missing runbook), never disabled.
    assert all(n.get("state") in {"enabled", "missing"} for n in care_jobs)
    assert not any(n.get("state") == "disabled" for n in care_jobs)
    assert "BrigadeCare" not in json.dumps(payload)


@pytest.mark.care_host_integration
def test_memory_topology_care_enabled_via_systemd_status_helper(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    home = tmp_path / "home"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))

    assert care_cmd.install(target=tmp_path, backend="systemd", home=home, json_output=True) == 0
    capsys.readouterr()
    status = care_cmd.status_payload(target=tmp_path, home=home)
    assert status["enabled"] is True
    assert status["backends"]["systemd"]["status"] == "current"

    # Topology must honor the structured helper (not crontab text scraping).
    from brigade import memory_operations

    monkeypatch.setattr(
        memory_operations.care_cmd,
        "status_payload",
        lambda target, home=None: status,
    )
    payload = _run_topology(tmp_path, capsys)
    care_jobs = [n for n in payload["nodes"] if n.get("kind") == "care_job"]
    assert care_jobs
    assert all(n.get("state") in {"enabled", "missing"} for n in care_jobs)
    assert not any(n.get("state") == "disabled" for n in care_jobs)


def test_memory_topology_malformed_config_degrades_without_traceback(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    root = tmp_path / ".brigade"
    root.mkdir(parents=True)
    (root / "config.json").write_text("{not-json\n")
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    rc = cli.main(["memory", "topology", "--target", str(tmp_path), "--json"])
    err = capsys.readouterr()
    assert rc == 0, err.err
    payload = json.loads(err.out)
    assert any(f.get("kind") == "malformed_config" for f in payload["flags"])
    malformed = next(f for f in payload["flags"] if f.get("kind") == "malformed_config")
    assert "malformed" in malformed.get("detail", "").lower()
    assert "unreadable" in malformed.get("detail", "").lower()
    assert "missing" not in malformed.get("detail", "").lower()
    assert payload["health"].get("config", {}).get("status") == "malformed"
    _assert_safe_paths(payload)


def test_memory_topology_redacts_unsafe_adapter_paths(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    sources = tmp_path / ".brigade" / "handoff-sources.json"
    sources.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "root": ".",
                        "inboxes": [
                            "/home/operator/.secret-adapter/memory-handoffs",
                            "C:\\Users\\operator\\.secret-adapter\\memory-handoffs",
                            "~/.secret-adapter/memory-handoffs",
                            ".safe-adapter/memory-handoffs",
                        ],
                    },
                    {
                        "root": "/var/lib/external-agent",
                        "inboxes": ["memory-handoffs"],
                    },
                ],
            }
        )
        + "\n"
    )
    (tmp_path / ".safe-adapter" / "memory-handoffs").mkdir(parents=True)

    payload = _run_topology(tmp_path, capsys)
    dumped = json.dumps(payload)
    assert "/home/" not in dumped
    assert "C:\\\\Users" not in dumped and "C:\\Users" not in dumped
    assert "~/.secret" not in dumped
    assert "/var/lib/" not in dumped
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["adapter:.safe-adapter/memory-handoffs"]["path"] == ".safe-adapter/memory-handoffs"
    unsafe = [
        n for n in payload["nodes"] if n.get("kind") == "adapter" and n["id"] != "adapter:.safe-adapter/memory-handoffs"
    ]
    assert unsafe
    for node in unsafe:
        path = node.get("path")
        assert isinstance(path, str)
        assert not path.startswith("/")
        assert "\\" not in path
        assert not path.startswith("~")
        assert path.startswith("redacted:")


def test_memory_topology_no_harness_still_emits_core_graph(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    root = tmp_path / ".brigade"
    root.mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": [],
                "owner": "this-repo",
                "includes": [],
            }
        )
        + "\n"
    )
    payload = _run_topology(tmp_path, capsys)
    assert payload["paths"] == []
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert "stage:lint" in by_id
    assert "stage:ingest" in by_id
    assert "canonical:cards" in by_id
    assert by_id["canonical:cards"]["owner"] == "canonical memory system"


def test_memory_topology_evidence_failure_does_not_abort(tmp_path, capsys, monkeypatch):
    from brigade import memory_operations

    def boom(target, **kwargs):
        raise RuntimeError("evidence engine unavailable")

    monkeypatch.setattr(memory_operations.evidence_cmd, "status_payload", boom)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    payload = _run_topology(tmp_path, capsys)
    assert payload["health"]["evidence_projection"]["status"] in {"unknown", "missing"}
    assert "stage:evidence_projection" in {n["id"] for n in payload["nodes"]}


def test_memory_topology_non_directory_target_exits_2(tmp_path, capsys):
    missing = tmp_path / "nope"
    rc = cli.main(["memory", "topology", "--target", str(missing), "--json"])
    err = capsys.readouterr()
    assert rc == 2
    assert "not a directory" in err.err


def test_memory_topology_present_receipts_on_care_edges(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    completed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entries = []
    for entry in care_cmd.CARE_ENTRIES:
        entries.append(
            {
                "id": entry.entry_id,
                "schedule": entry.schedule,
                "on_calendar": entry.on_calendar,
                "kind": entry.kind,
                "description": entry.description,
                "runbook": entry.runbook_rel,
                "runbook_id": entry.runbook_id,
                "runbook_present": True,
                "last_receipt": {
                    "run_id": f"run-{entry.entry_id}",
                    "status": "ok",
                    "started_at": completed,
                    "completed_at": completed,
                },
            }
        )
    monkeypatch.setattr(
        memory_operations.care_cmd,
        "status_payload",
        lambda target, home=None: {
            "enabled": True,
            "backends": {"crontab": {"status": "current"}, "systemd": {"status": "missing"}},
            "entries": entries,
        },
    )
    payload = _run_topology(tmp_path, capsys)
    care_edges = [e for e in payload["edges"] if str(e.get("id", "")).startswith("edge:care_job:")]
    assert care_edges
    assert all(e["latest_receipt"].get("evidence_state") == "present" for e in care_edges)


def test_memory_topology_shared_edges_are_unique_and_paths_include_edge_ids(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude", "codex"])
    _write_handoff(tmp_path / ".claude" / "memory-handoffs")
    _write_handoff(tmp_path / ".codex" / "memory-handoffs")

    payload = _run_topology(tmp_path, capsys)
    edge_ids = [e["id"] for e in payload["edges"]]
    assert len(edge_ids) == len(set(edge_ids))
    assert edge_ids.count("edge:lint:ingest") == 1
    for kind in CANONICAL_KINDS:
        assert edge_ids.count(f"edge:ingest:write:{kind}") == 1
    for path in payload["paths"]:
        assert "edge_ids" in path
        assert "edge:lint:ingest" in path["edge_ids"]
        assert "edge:ingest:write:cards" in path["edge_ids"]


def test_memory_topology_manual_refresh_graph_direction(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    monkeypatch.setattr(
        memory_operations.care_cmd,
        "status_payload",
        lambda target, home=None: {
            "enabled": True,
            "target_match": True,
            "backends": {
                "crontab": {"status": "current", "target_match": True},
                "systemd": {"status": "missing", "target_match": False},
            },
            "entries": [
                {
                    "id": entry.entry_id,
                    "schedule": entry.schedule,
                    "on_calendar": entry.on_calendar,
                    "kind": entry.kind,
                    "description": entry.description,
                    "runbook": entry.runbook_rel,
                    "runbook_id": entry.runbook_id,
                    "runbook_present": True,
                    "last_receipt": None,
                }
                for entry in care_cmd.CARE_ENTRIES
            ],
        },
    )
    payload = _run_topology(tmp_path, capsys)
    by_id = {e["id"]: e for e in payload["edges"]}
    assert "edge:refresh_queue:cards" not in by_id
    assert by_id["edge:queue:manual_refresh"]["from"] == "stage:refresh_queue"
    assert by_id["edge:queue:manual_refresh"]["to"] == "stage:manual_refresh"
    manual_cards = by_id["edge:manual_refresh:cards"]
    assert manual_cards["authority"] == "canonical_writer"
    assert manual_cards["writer_component"] == "memory-care-backfill"
    assert manual_cards["enabled"] is False
    assert manual_cards["latest_receipt"]["evidence_state"] == "missing"
    scan_edge = by_id["edge:cards:care_scan"]
    assert scan_edge["flow"] == "scan"
    assert scan_edge["authority"] == "read_only"
    assert scan_edge.get("cycle_exempt") is True
    assert "cycle" not in {f.get("kind") for f in payload["flags"]}
    manual = next(n for n in payload["nodes"] if n["id"] == "stage:manual_refresh")
    assert manual["owner"] == "manual process"
    care_job = next(n for n in payload["nodes"] if n["kind"] == "care_job")
    assert care_job["owner"] == "scheduler"


def test_memory_topology_manual_refresh_visible_but_not_duplicate_enabled_writer(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    monkeypatch.setattr(
        memory_operations.care_cmd,
        "status_payload",
        lambda target, home=None: {
            "enabled": True,
            "backends": {"crontab": {"status": "current"}, "systemd": {"status": "missing"}},
            "entries": [
                {
                    "id": entry.entry_id,
                    "schedule": entry.schedule,
                    "on_calendar": entry.on_calendar,
                    "kind": entry.kind,
                    "description": entry.description,
                    "runbook": entry.runbook_rel,
                    "runbook_id": entry.runbook_id,
                    "runbook_present": True,
                    "last_receipt": None,
                }
                for entry in care_cmd.CARE_ENTRIES
            ],
        },
    )
    payload = _run_topology(tmp_path, capsys)
    manual_cards = next(e for e in payload["edges"] if e.get("id") == "edge:manual_refresh:cards")
    assert manual_cards["enabled"] is False
    assert manual_cards["authority"] == "canonical_writer"
    assert manual_cards["writer_component"] == "memory-care-backfill"
    ingest_writes = [
        e
        for e in payload["edges"]
        if e.get("to") == "canonical:cards" and e.get("writer_component") == "ingest" and e.get("enabled")
    ]
    assert ingest_writes
    assert "duplicate_enabled_writer" not in {f.get("kind") for f in payload["flags"]}
    manual_nodes = [
        n
        for n in payload["nodes"]
        if n.get("id") in {"stage:manual_refresh", "writer:manual_refresh"} or n.get("kind") == "manual_writer"
    ]
    assert manual_nodes
    assert all(n.get("state") in {"manual", "disabled", "external"} for n in manual_nodes)


def test_memory_topology_manual_backfill_receipt_is_evidence_not_enablement(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    backfills = tmp_path / ".brigade" / "memory-care" / "backfills"
    backfills.mkdir(parents=True)
    # Real-shaped receipt from `brigade memory care backfill --apply` (no invented status field).
    (backfills / "20260811T120000.json").write_text(
        json.dumps(
            {
                "target": str(tmp_path),
                "generated_at": "2026-08-11T12:00:00Z",
                "written_count": 1,
                "skipped_no_frontmatter": 0,
                "stale_after_days": 30,
                "candidates": [{"path": "memory/cards/example.md"}],
            }
        )
    )
    payload = _run_topology(tmp_path, capsys)
    manual_cards = next(e for e in payload["edges"] if e["id"] == "edge:manual_refresh:cards")
    assert manual_cards["enabled"] is False
    assert manual_cards["authority"] == "canonical_writer"
    assert manual_cards["writer_component"] == "memory-care-backfill"
    assert manual_cards["latest_receipt"]["evidence_state"] == "present"
    assert manual_cards["latest_receipt"]["completed_at"] == "2026-08-11T12:00:00Z"
    assert "duplicate_enabled_writer" not in {f.get("kind") for f in payload["flags"]}
    enabled_card_writers = [
        e
        for e in payload["edges"]
        if e.get("to") == "canonical:cards"
        and e.get("enabled")
        and e.get("authority") == "canonical_writer"
        and e.get("writer_component") == "memory-care-backfill"
    ]
    assert enabled_card_writers == []


def test_memory_topology_flags_duplicate_queue_producers_without_fan_in_false_positive():
    from brigade import memory_operations

    nodes = []
    edges = [
        {
            "id": "edge:harness:a:lint",
            "from": "inbox:a",
            "to": "stage:lint",
            "flow": "validate",
            "authority": "queue_producer",
            "writer_component": "harness-a",
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": True,
        },
        {
            "id": "edge:harness:b:lint",
            "from": "inbox:b",
            "to": "stage:lint",
            "flow": "validate",
            "authority": "queue_producer",
            "writer_component": "harness-b",
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": True,
        },
        {
            "id": "edge:dup:one",
            "from": "care_job:daily-care",
            "to": "stage:care_scan",
            "flow": "schedule",
            "authority": "queue_producer",
            "writer_component": "daily-care",
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": True,
        },
        {
            "id": "edge:dup:two",
            "from": "care_job:daily-care-copy",
            "to": "stage:care_scan",
            "flow": "schedule",
            "authority": "queue_producer",
            "writer_component": "daily-care",
            "latest_receipt": {"evidence_state": "missing"},
            "enabled": True,
        },
    ]
    flags = memory_operations.detect_flags(nodes, edges, health={})
    kinds = {f["kind"] for f in flags}
    assert "duplicate_enabled_queue_producer" in kinds
    dup_flags = [f for f in flags if f.get("kind") == "duplicate_enabled_queue_producer"]
    assert any(f.get("destination") == "stage:care_scan" for f in dup_flags)
    assert not any(f.get("destination") == "stage:lint" for f in dup_flags)


def test_memory_topology_stale_receipt_skips_disabled_edges():
    from brigade import memory_operations

    stale_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    edges = [
        {
            "id": "edge:disabled-stale",
            "from": "stage:manual_refresh",
            "to": "canonical:cards",
            "flow": "mutate",
            "authority": "canonical_writer",
            "writer_component": "memory-care-backfill",
            "latest_receipt": {"evidence_state": "present", "status": "ok", "completed_at": stale_at},
            "enabled": False,
        }
    ]
    flags = memory_operations.detect_flags([], edges, health={})
    assert "stale_receipt" not in {f.get("kind") for f in flags}


def test_memory_topology_receipt_prefers_finished_at_and_duration():
    from brigade import memory_operations

    receipt = memory_operations._normalize_receipt(
        {
            "status": "ok",
            "started_at": "2026-08-01T00:00:00Z",
            "finished_at": "2026-08-01T00:00:05Z",
            "completed_at": "2026-08-01T00:00:01Z",
            "duration_seconds": 5.0,
            "run_id": "run-1",
        }
    )
    assert receipt["completed_at"] == "2026-08-01T00:00:05Z"
    assert receipt["duration_seconds"] == 5.0


def test_memory_topology_malformed_config_non_object_json(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    root = tmp_path / ".brigade"
    root.mkdir(parents=True)
    (root / "config.json").write_text("[]\n")
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    rc = cli.main(["memory", "topology", "--target", str(tmp_path), "--json"])
    err = capsys.readouterr()
    assert rc == 0, err.err
    payload = json.loads(err.out)
    assert any(f.get("kind") == "malformed_config" for f in payload["flags"])


def test_memory_topology_external_scheduler_from_redacted_list_payload(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    surfaces = tmp_path / ".brigade" / "operator" / "surfaces"
    surfaces.mkdir(parents=True)
    # Capture-shaped latest.json; topology must consume public surfaces_list_payload records.
    (surfaces / "latest.json").write_text(
        json.dumps(
            {
                "captured_at": "2026-08-11T12:00:00Z",
                "surface_count": 2,
                "record_count": 2,
                "surfaces": {
                    "shell_crontab": {"count": 1, "available": True},
                    "openclaw_cron": {"count": 1, "available": True},
                    "pm2": {"count": 0, "available": False},
                },
                "records": [
                    {
                        "surface": "shell_crontab",
                        "record_label": "shell-crontab-001",
                        "status": "present",
                        "source_fingerprint": "abcdef0123456789",
                        "raw_included": False,
                        "command_included": False,
                        "path_included": False,
                        "env_included": False,
                        "schedule_kind": "cron",
                    },
                    {
                        "surface": "openclaw_cron",
                        "record_label": "openclaw-cron-001",
                        "status": "ok",
                        "source_fingerprint": "fedcba9876543210",
                        "raw_included": False,
                        "command_included": False,
                        "path_included": False,
                        "env_included": False,
                    },
                ],
                "privacy": {"raw_crontab_lines_included": False, "job_names_included": False},
                "source_fingerprint": "capture-fingerprint",
            }
        )
    )
    payload = _run_topology(tmp_path, capsys)
    external = next(n for n in payload["nodes"] if n["id"] == "adapter:external_scheduler")
    assert external["owner"] == "external adapter"
    assert external["state"] == "external"
    assert external["schedule"] == {"source": "external", "cadence": "unknown"}
    assert isinstance(external["path"], str) and external["path"].startswith("redacted:")
    assert external["counts"].get("record_count") == 2
    dumped = json.dumps(payload)
    assert "shell-crontab-001" not in dumped
    assert "openclaw-cron-001" not in dumped
    assert "/example/" not in dumped
    assert not any(
        e.get("from") == "adapter:external_scheduler" or e.get("to") == "adapter:external_scheduler"
        for e in payload["edges"]
    )
    _assert_safe_paths(payload)


def test_memory_topology_pm2_only_surfaces_are_not_external_scheduler(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    surfaces = tmp_path / ".brigade" / "operator" / "surfaces"
    surfaces.mkdir(parents=True)
    (surfaces / "latest.json").write_text(
        json.dumps(
            {
                "captured_at": "2026-08-11T12:00:00Z",
                "surface_count": 1,
                "record_count": 1,
                "surfaces": {"pm2": {"count": 1, "available": True, "status_counts": {"online": 1}}},
                "records": [
                    {
                        "surface": "pm2",
                        "record_label": "pm2-001",
                        "status": "online",
                        "source_fingerprint": "pm2fingerprint0001",
                        "raw_included": False,
                        "command_included": False,
                        "path_included": False,
                        "env_included": False,
                    }
                ],
                "privacy": {"raw_pm2_processes_included": False, "process_names_included": False},
                "source_fingerprint": "pm2-only-fingerprint",
            }
        )
    )
    payload = _run_topology(tmp_path, capsys)
    assert "adapter:external_scheduler" not in {n["id"] for n in payload["nodes"]}


def test_memory_topology_cadence_aware_stale_receipts():
    from brigade import memory_operations

    four_days = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat().replace("+00:00", "Z")
    ten_days = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    nodes = [
        {
            "id": "care_job:weekly-outcome-ratchet",
            "kind": "care_job",
            "label": "weekly",
            "owner": "brigade",
            "authority": "queue_producer",
            "state": "enabled",
            "path": ".brigade/memory-care/runbooks/weekly-outcome-ratchet.json",
            "schedule": {"source": "brigade_care", "cadence": "weekly"},
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "care_job:daily-care",
            "kind": "care_job",
            "label": "daily",
            "owner": "brigade",
            "authority": "queue_producer",
            "state": "enabled",
            "path": ".brigade/memory-care/runbooks/daily-care-pass.json",
            "schedule": {"source": "brigade_care", "cadence": "daily"},
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "stage:refresh_queue",
            "kind": "queue",
            "label": "queue",
            "owner": "brigade",
            "authority": "queue_producer",
            "state": "enabled",
            "path": ".brigade/memory-care",
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
    ]
    edges = [
        {
            "id": "edge:care_job:weekly-outcome-ratchet",
            "from": "care_job:weekly-outcome-ratchet",
            "to": "stage:refresh_queue",
            "flow": "schedule",
            "authority": "queue_producer",
            "writer_component": "weekly-outcome-ratchet",
            "latest_receipt": {"evidence_state": "present", "status": "ok", "completed_at": four_days},
            "enabled": True,
        },
        {
            "id": "edge:care_job:daily-care",
            "from": "care_job:daily-care",
            "to": "stage:refresh_queue",
            "flow": "schedule",
            "authority": "queue_producer",
            "writer_component": "daily-care",
            "latest_receipt": {"evidence_state": "present", "status": "ok", "completed_at": four_days},
            "enabled": True,
        },
        {
            "id": "edge:care_job:weekly-old",
            "from": "care_job:weekly-outcome-ratchet",
            "to": "stage:refresh_queue",
            "flow": "schedule",
            "authority": "queue_producer",
            "writer_component": "weekly-outcome-ratchet",
            "latest_receipt": {"evidence_state": "present", "status": "ok", "completed_at": ten_days},
            "enabled": True,
        },
    ]
    flags = memory_operations.detect_flags(nodes, edges, health={})
    stale_ids = {f.get("edge_id") for f in flags if f.get("kind") == "stale_receipt"}
    assert "edge:care_job:weekly-outcome-ratchet" not in stale_ids
    assert "edge:care_job:daily-care" in stale_ids
    assert "edge:care_job:weekly-old" in stale_ids


def test_memory_topology_scan_and_queue_counts_use_structured_fields(tmp_path, capsys, monkeypatch):
    from brigade import memory_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    assert cli.main(["memory", "care", "init", "--target", str(tmp_path)]) == 0
    capsys.readouterr()

    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "stale.md").write_text(
        "---\ntopic: stale\ncategory: system\ntags: [a]\nreviewed: 2020-01-01\n---\n\nStale fact.\n"
    )
    (tmp_path / "MEMORY.md").write_text("- [stale](memory/cards/stale.md)\n")
    assert memory_cmd.scan(target=tmp_path) == 0
    capsys.readouterr()

    health = memory_cmd.health(tmp_path)
    assert "scan_issue_count" in health
    assert "queue_count" in health
    assert health["scan_issue_count"] >= 1
    assert health["queue_count"] >= 1
    # issue_count remains the failing/warn health-check count, not scan findings.
    assert isinstance(health["issue_count"], int)
    assert health["issue_count"] == len([c for c in health["checks"] if c.get("status") != "ok"])

    monkeypatch.setattr(memory_operations.memory_cmd, "health", lambda target: health)
    payload = _run_topology(tmp_path, capsys)
    assert payload["health"]["care_scan"]["issue_count"] == health["scan_issue_count"]
    assert payload["health"]["refresh_queue"]["count"] == health["queue_count"]
    care_scan = next(n for n in payload["nodes"] if n["id"] == "stage:care_scan")
    assert care_scan["counts"]["issue_count"] == health["scan_issue_count"]
    queue = next(n for n in payload["nodes"] if n["id"] == "stage:refresh_queue")
    assert queue["counts"]["queued"] == health["queue_count"]


def test_memory_topology_different_target_care_block_does_not_enable_jobs(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    monkeypatch.setattr(
        memory_operations.care_cmd,
        "status_payload",
        lambda target, home=None: {
            "enabled": False,
            "target_match": False,
            "backends": {
                "crontab": {"status": "stale", "target_match": False},
                "systemd": {"status": "missing", "target_match": False},
            },
            "entries": [
                {
                    "id": entry.entry_id,
                    "schedule": entry.schedule,
                    "on_calendar": entry.on_calendar,
                    "kind": entry.kind,
                    "description": entry.description,
                    "runbook": entry.runbook_rel,
                    "runbook_id": entry.runbook_id,
                    "runbook_present": True,
                    "last_receipt": None,
                }
                for entry in care_cmd.CARE_ENTRIES
            ],
        },
    )
    payload = _run_topology(tmp_path, capsys)
    care_jobs = [n for n in payload["nodes"] if n.get("kind") == "care_job"]
    assert care_jobs
    assert all(n.get("state") == "disabled" for n in care_jobs)
    scan = next(n for n in payload["nodes"] if n["id"] == "stage:care_scan")
    assert scan["owner"] == "brigade"
    assert scan["state"] == "manual"
    assert scan["state"] != "enabled"
    queue = next(n for n in payload["nodes"] if n["id"] == "stage:refresh_queue")
    assert queue["owner"] == "brigade"
    assert queue["state"] == "manual"
    by_edge = {e["id"]: e for e in payload["edges"]}
    assert by_edge["edge:care_scan:queue"]["enabled"] is True
    care_schedule_edges = [e for e in payload["edges"] if str(e.get("id", "")).startswith("edge:care_job:")]
    assert care_schedule_edges
    assert all(e["enabled"] is False for e in care_schedule_edges)


def test_memory_topology_care_stage_ownership_when_scheduler_disabled(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    monkeypatch.setattr(
        memory_operations.care_cmd,
        "status_payload",
        lambda target, home=None: {
            "enabled": False,
            "target_match": False,
            "backends": {
                "crontab": {"status": "missing", "target_match": False},
                "systemd": {"status": "missing", "target_match": False},
            },
            "entries": [
                {
                    "id": entry.entry_id,
                    "schedule": entry.schedule,
                    "on_calendar": entry.on_calendar,
                    "kind": entry.kind,
                    "description": entry.description,
                    "runbook": entry.runbook_rel,
                    "runbook_id": entry.runbook_id,
                    "runbook_present": True,
                    "last_receipt": None,
                }
                for entry in care_cmd.CARE_ENTRIES
            ],
        },
    )
    payload = _run_topology(tmp_path, capsys)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["stage:care_scan"]["owner"] == "brigade"
    assert by_id["stage:care_scan"]["state"] == "manual"
    assert by_id["stage:care_scan"]["schedule"] == {"source": "manual", "cadence": "on_demand"}
    assert by_id["stage:refresh_queue"]["owner"] == "brigade"
    assert by_id["stage:refresh_queue"]["state"] == "manual"
    care_jobs = [n for n in payload["nodes"] if n.get("kind") == "care_job"]
    assert care_jobs
    assert all(n["owner"] == "scheduler" for n in care_jobs)
    assert all(n["state"] == "disabled" for n in care_jobs)
    by_edge = {e["id"]: e for e in payload["edges"]}
    assert by_edge["edge:care_scan:queue"]["enabled"] is True
    assert all(by_edge[f"edge:care_job:{n['id'].split(':', 1)[1]}"]["enabled"] is False for n in care_jobs)


def test_memory_topology_care_stage_ownership_when_scheduler_enabled(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    monkeypatch.setattr(
        memory_operations.care_cmd,
        "status_payload",
        lambda target, home=None: {
            "enabled": True,
            "target_match": True,
            "backends": {
                "crontab": {"status": "current", "target_match": True},
                "systemd": {"status": "missing", "target_match": False},
            },
            "entries": [
                {
                    "id": entry.entry_id,
                    "schedule": entry.schedule,
                    "on_calendar": entry.on_calendar,
                    "kind": entry.kind,
                    "description": entry.description,
                    "runbook": entry.runbook_rel,
                    "runbook_id": entry.runbook_id,
                    "runbook_present": True,
                    "last_receipt": None,
                }
                for entry in care_cmd.CARE_ENTRIES
            ],
        },
    )
    payload = _run_topology(tmp_path, capsys)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["stage:care_scan"]["owner"] == "brigade"
    assert by_id["stage:care_scan"]["state"] == "enabled"
    assert by_id["stage:refresh_queue"]["owner"] == "brigade"
    assert by_id["stage:refresh_queue"]["state"] == "enabled"
    care_jobs = [n for n in payload["nodes"] if n.get("kind") == "care_job"]
    assert care_jobs
    assert all(n["owner"] == "scheduler" for n in care_jobs)
    assert all(n["state"] in {"enabled", "missing"} for n in care_jobs)
    by_edge = {e["id"]: e for e in payload["edges"]}
    assert by_edge["edge:care_scan:queue"]["enabled"] is True
    assert all(
        by_edge[f"edge:care_job:{n['id'].split(':', 1)[1]}"]["enabled"] is (n["state"] == "enabled") for n in care_jobs
    )


def test_memory_topology_nightly_ops_cadence_and_canonical_owner(tmp_path, capsys, monkeypatch):
    from brigade import care_cmd, memory_operations

    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    monkeypatch.setattr(
        memory_operations.care_cmd,
        "status_payload",
        lambda target, home=None: {
            "enabled": False,
            "backends": {"crontab": {"status": "missing"}, "systemd": {"status": "missing"}},
            "entries": [
                {
                    "id": entry.entry_id,
                    "schedule": entry.schedule,
                    "on_calendar": entry.on_calendar,
                    "kind": entry.kind,
                    "description": entry.description,
                    "runbook": entry.runbook_rel,
                    "runbook_id": entry.runbook_id,
                    "runbook_present": True,
                    "last_receipt": None,
                }
                for entry in care_cmd.CARE_ENTRIES
            ],
        },
    )
    payload = _run_topology(tmp_path, capsys)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert "care_job:nightly-ops" in by_id
    assert by_id["care_job:nightly-ops"]["schedule"]["cadence"] == "nightly"
    for kind in CANONICAL_KINDS:
        assert by_id[f"canonical:{kind}"]["owner"] == "canonical memory system"
    node_ids = [n["id"] for n in payload["nodes"]]
    assert node_ids == sorted(node_ids)
    edge_ids = [e["id"] for e in payload["edges"]]
    assert edge_ids == sorted(edge_ids)
    assert edge_ids == list(dict.fromkeys(edge_ids))


def test_memory_topology_dot_slash_inbox_does_not_duplicate_builtin_adapter(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    sources = tmp_path / ".brigade" / "handoff-sources.json"
    sources.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [{"root": ".", "inboxes": ["./.claude/memory-handoffs"]}],
            }
        )
        + "\n"
    )
    payload = _run_topology(tmp_path, capsys)
    adapter_ids = [n["id"] for n in payload["nodes"] if n.get("kind") == "adapter"]
    assert adapter_ids.count("adapter:.claude/memory-handoffs") == 0
    assert "adapter:.claude/memory-handoffs" not in adapter_ids


def _write_handoff_sources(target: Path, sources_payload: dict) -> None:
    path = target / ".brigade" / "handoff-sources.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sources_payload) + "\n")


def test_memory_topology_adapter_ids_stable_under_source_reorder(tmp_path, capsys, monkeypatch):
    _stub_evidence(monkeypatch)
    _write_config(tmp_path, harnesses=["claude"])
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    (tmp_path / ".safe-adapter" / "memory-handoffs").mkdir(parents=True)
    payload_a = {
        "version": 1,
        "sources": [
            {
                "root": ".",
                "inboxes": [
                    "/home/operator/.secret-a/memory-handoffs",
                    ".safe-adapter/memory-handoffs",
                ],
            },
            {"root": "/var/lib/external-agent", "inboxes": ["memory-handoffs"]},
        ],
    }
    payload_b = {
        "version": 1,
        "sources": [
            {"root": "/var/lib/external-agent", "inboxes": ["memory-handoffs"]},
            {
                "root": ".",
                "inboxes": [
                    ".safe-adapter/memory-handoffs",
                    "/home/operator/.secret-a/memory-handoffs",
                ],
            },
        ],
    }
    _write_handoff_sources(tmp_path, payload_a)
    first = _run_topology(tmp_path, capsys)
    _write_handoff_sources(tmp_path, payload_b)
    second = _run_topology(tmp_path, capsys)
    first_adapters = sorted(n["id"] for n in first["nodes"] if n.get("kind") == "adapter")
    second_adapters = sorted(n["id"] for n in second["nodes"] if n.get("kind") == "adapter")
    assert first_adapters == second_adapters
    for node_id in first_adapters:
        assert "/home/" not in node_id
        assert node_id.startswith("adapter:")
        assert "secret-a" not in node_id
        assert "external-agent" not in node_id
