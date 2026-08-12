"""Tests for `brigade memory topology --json` (#875 Task 1)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
