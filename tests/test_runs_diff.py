"""Durable child-vs-parent (or sibling) run diff (#1071)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from brigade import cli
from brigade import runs_diff


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_run(
    run_dir: Path,
    *,
    status: str = "ok",
    task: str = "inspect work",
    error: str | None = None,
    failure: dict[str, object] | None = None,
    lineage: dict[str, object] | None = None,
    suspected_noop: bool | None = None,
    worker_ok: bool = True,
    worker_status: str = "ok",
    verify_status: str = "ok",
    verify_exit: int = 0,
    verify_command: str = "pytest -q",
    verify_run_id: str | None = None,
    synthesis_ok: bool = True,
    synthesis_detail: str = "merged",
    final_text: str | None = "done\n",
    extra: dict[str, object] | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "task": task,
        "status": status,
        "started_at": "2026-08-21T10:00:00Z",
        "finished_at": "2026-08-21T10:00:04Z",
        "duration_seconds": 4.0,
        "orchestrator": "chef",
    }
    if error is not None:
        payload["error"] = error
    if failure is not None:
        payload["failure"] = failure
    if lineage is not None:
        payload["lineage"] = lineage
    if suspected_noop is True:
        payload["suspected_noop"] = True
    if extra:
        payload.update(extra)
    _write_json(run_dir / "run.json", payload)
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "coder",
                    "ok": worker_ok,
                    "status": worker_status,
                    "exit_code": 0 if worker_ok else 1,
                }
            ],
            "ground_truth": {
                "available": True,
                "verify_receipts": [
                    {
                        "run_id": verify_run_id or f"verify-{run_dir.name}",
                        "status": verify_status,
                        "commands": [{"command": verify_command, "exit_code": verify_exit}],
                    }
                ],
            },
        },
    )
    _write_json(
        run_dir / "synthesis.json",
        {"orchestrator": "chef", "mode": "merge", "result": {"ok": synthesis_ok, "detail": synthesis_detail}},
    )
    if final_text is not None:
        (run_dir / "final.txt").write_text(final_text)


def _child_lineage(parent_id: str, branch: str = "20260821-120000-parent-000003-bbbbbbbbbbbb") -> dict[str, object]:
    return {
        "kind": "child",
        "parent_run_id": parent_id,
        "branch_point_event_id": branch,
    }


def _compatible_graphtrail(
    *, after_sha: str, symbols: int = 0, extra: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "status": "ok",
        "changed_symbol_count": symbols,
        "edge_churn": 0,
        "before_snapshot_path": "/home/someuser/project/graphtrail-before.db",
        "db_path": "/home/someuser/project/.graphtrail/graphtrail.db",
        "attestations": {
            "before_snapshot_sha256": "a" * 64,
            "after_snapshot_sha256": after_sha,
        },
    }
    if extra:
        payload.update(extra)
    return payload


def _tree_fingerprint(root: Path) -> dict[str, str]:
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def test_runs_diff_cli_dispatches_child_and_optional_other(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_diff(run, other, **kwargs):
        seen.update(run=run, other=other, **kwargs)
        return 0

    monkeypatch.setattr(runs_diff, "diff", fake_diff, raising=False)
    rc = cli.main(["runs", "diff", "child-id", "sibling-id", "--cwd", str(tmp_path), "--json"])
    assert rc == 0
    assert seen == {
        "run": "child-id",
        "other": "sibling-id",
        "cwd": tmp_path,
        "runs_dir": None,
        "json_output": True,
    }


def test_diff_compares_child_against_recorded_parent(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent, status="ok", task="parent task", worker_ok=True, verify_status="ok")
    _write_run(
        child,
        status="failed",
        task="child task",
        error="child failed",
        failure={"phase": "worker", "kind": "exit-error", "detail": "coder died"},
        lineage=_child_lineage(parent.name),
        worker_ok=False,
        worker_status="failed",
        verify_status="failed",
        verify_exit=1,
        synthesis_ok=False,
        synthesis_detail="could not merge",
        final_text="child-final\n",
        suspected_noop=True,
    )

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == runs_diff.RUN_DIFF_SCHEMA
    assert set(payload) == runs_diff.RUN_DIFF_KEYS
    assert payload["relation"] == "child-parent"
    assert payload["left"]["run_id"] == child.name
    assert payload["left"]["role"] == "child"
    assert payload["right"]["run_id"] == parent.name
    assert payload["right"]["role"] == "parent"
    assert payload["parent_run_id"] == parent.name
    assert payload["lifecycle"]["changed"] is True
    assert any(change["field"] == "status" for change in payload["lifecycle"]["changes"])
    assert payload["workers"]["changed"] is True
    assert payload["verification"]["changed"] is True
    assert payload["outcome"]["changed"] is True
    assert payload["graphtrail"]["status"] == "skipped"
    assert payload["graphtrail"]["reason"] == "absent snapshots"
    assert payload["verification"]["left"]["run_id"] != payload["verification"]["right"]["run_id"]
    assert any(change["field"] == "status" for change in payload["verification"]["changes"])
    assert any(change["field"] == "exit_code" for change in payload["verification"]["changes"])


def test_verification_matching_content_different_run_ids_is_unchanged(tmp_path, capsys):
    """Equal verify results stay unchanged even when production-unique run_ids differ."""
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(
        parent,
        verify_run_id="20260821-120000-work-verify-aaaaaa",
        verify_status="ok",
        verify_exit=0,
        verify_command="pytest -q",
        final_text="same-final\n",
    )
    _write_run(
        child,
        lineage=_child_lineage(parent.name),
        verify_run_id="20260821-130000-work-verify-bbbbbb",
        verify_status="ok",
        verify_exit=0,
        verify_command="pytest -q",
        final_text="same-final\n",
    )

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"]["left"]["run_id"] == "20260821-130000-work-verify-bbbbbb"
    assert payload["verification"]["right"]["run_id"] == "20260821-120000-work-verify-aaaaaa"
    assert payload["verification"]["left"]["run_id"] != payload["verification"]["right"]["run_id"]
    assert payload["verification"]["changed"] is False
    assert payload["verification"].get("changes") is None
    assert payload["verification"]["left"]["status"] == payload["verification"]["right"]["status"] == "ok"
    assert payload["verification"]["left"]["exit_code"] == payload["verification"]["right"]["exit_code"] == 0
    assert payload["verification"]["left"]["command"] == payload["verification"]["right"]["command"]
    assert payload["verification"]["left"]["digest"] == payload["verification"]["right"]["digest"]
    assert payload["verification"]["left"]["digest"]


def test_verification_content_differences_are_changed(tmp_path, capsys):
    """A status, exit_code, command, or result-digest mismatch must mark verification changed."""
    cases = (
        ({"verify_status": "failed"}, "status"),
        ({"verify_exit": 1}, "exit_code"),
        ({"verify_command": "ruff check ."}, "command"),
        ({"final_text": "child-verify-final\n"}, "digest"),
    )
    for child_kwargs, field in cases:
        runs_root = tmp_path / ".brigade" / "runs"
        parent = runs_root / f"20260821-120000-parent-{field}"
        child = runs_root / f"20260821-130000-child-{field}"
        shared = {
            "verify_status": "ok",
            "verify_exit": 0,
            "verify_command": "pytest -q",
            "final_text": "same-final\n",
        }
        _write_run(parent, verify_run_id=f"verify-{parent.name}", **shared)
        _write_run(
            child,
            lineage=_child_lineage(parent.name),
            verify_run_id=f"verify-{child.name}",
            **{**shared, **child_kwargs},
        )
        assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verification"]["left"]["run_id"] != payload["verification"]["right"]["run_id"]
        assert payload["verification"]["changed"] is True, field
        assert any(change["field"] == field for change in payload["verification"]["changes"]), field
        assert payload["verification"]["left"][field] != payload["verification"]["right"][field], field


def test_diff_two_run_form_compares_siblings(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child_a = runs_root / "20260821-130000-child-a"
    child_b = runs_root / "20260821-140000-child-b"
    _write_run(parent)
    _write_run(child_a, lineage=_child_lineage(parent.name), worker_ok=True)
    _write_run(child_b, lineage=_child_lineage(parent.name), worker_ok=False, worker_status="failed")

    assert cli.main(["runs", "diff", child_a.name, child_b.name, "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["relation"] == "siblings"
    assert payload["left"]["run_id"] == child_a.name
    assert payload["right"]["run_id"] == child_b.name
    assert payload["left"]["role"] == "sibling"
    assert payload["parent_run_id"] == parent.name
    assert payload["workers"]["changed"] is True


def test_diff_two_run_form_normalizes_parent_then_child_order(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent)
    _write_run(child, lineage=_child_lineage(parent.name), status="failed")

    assert cli.main(["runs", "diff", parent.name, child.name, "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["relation"] == "child-parent"
    assert payload["left"]["run_id"] == child.name
    assert payload["right"]["run_id"] == parent.name


def test_graphtrail_compared_when_both_sides_have_compatible_snapshots(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent)
    _write_run(child, lineage=_child_lineage(parent.name))
    _write_json(parent / "graph-delta.json", _compatible_graphtrail(after_sha="b" * 64, symbols=0))
    _write_json(child / "graph-delta.json", _compatible_graphtrail(after_sha="c" * 64, symbols=2))

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["graphtrail"]["status"] == "compared"
    assert payload["graphtrail"]["changed"] is True
    assert "reason" not in payload["graphtrail"]
    assert payload["graphtrail"]["left"]["after_snapshot_sha256"] == "c" * 64
    assert payload["graphtrail"]["right"]["after_snapshot_sha256"] == "b" * 64
    dumped = json.dumps(payload)
    assert "/home/someuser" not in dumped
    assert "graphtrail-before.db" not in dumped


def test_graphtrail_skips_incompatible_snapshots_without_error(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent, extra={"code_graph_delta": _compatible_graphtrail(after_sha="b" * 64)})
    _write_run(
        child,
        lineage=_child_lineage(parent.name),
        extra={"code_graph_delta": {"ok": False, "status": "unavailable", "summary": "no graph"}},
    )

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["graphtrail"]["status"] == "skipped"
    assert payload["graphtrail"]["reason"] == "incompatible snapshots"
    assert payload["graphtrail"].get("changed") is None


def test_graphtrail_skips_absent_snapshots(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent)
    _write_run(child, lineage=_child_lineage(parent.name))

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["graphtrail"]["status"] == "skipped"
    assert payload["graphtrail"]["reason"] == "absent snapshots"
    assert payload["graphtrail"].get("changed") is None


def test_diff_never_mutates_parent_or_child(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent)
    _write_run(child, lineage=_child_lineage(parent.name), status="failed")
    before = {parent.name: _tree_fingerprint(parent), child.name: _tree_fingerprint(child)}

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    after = {parent.name: _tree_fingerprint(parent), child.name: _tree_fingerprint(child)}
    assert after == before


def test_unknown_run_fails_closed_with_no_stdout(tmp_path, capsys):
    (tmp_path / ".brigade" / "runs").mkdir(parents=True)
    rc = cli.main(["runs", "diff", "missing-run", "--cwd", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "not found" in captured.err


def test_corrupt_run_fails_closed_with_no_stdout(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent)
    child.mkdir(parents=True)
    (child / "run.json").write_text("not json")
    rc = cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "not valid JSON" in captured.err


def test_parentless_run_fails_closed_with_no_stdout(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    legacy = runs_root / "20260821-120000-legacy"
    _write_run(legacy)
    rc = cli.main(["runs", "diff", legacy.name, "--cwd", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "no recorded parent lineage" in captured.err


def test_missing_parent_fails_closed_with_no_stdout(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    child = runs_root / "20260821-130000-child"
    _write_run(child, lineage=_child_lineage("20260821-120000-missing"))
    rc = cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "recorded parent run not found" in captured.err


def test_unrelated_runs_fail_closed_with_no_stdout(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    left = runs_root / "20260821-130000-left"
    right = runs_root / "20260821-140000-right"
    _write_run(left, lineage=_child_lineage("parent-a"))
    _write_run(right, lineage=_child_lineage("parent-b"))
    rc = cli.main(["runs", "diff", left.name, right.name, "--cwd", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "do not share parent lineage" in captured.err


def test_human_diff_is_text_and_names_skip(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent)
    _write_run(child, lineage=_child_lineage(parent.name), status="failed")

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path)]) == 0
    human = capsys.readouterr().out
    assert human.startswith(f"diff: {child.name} vs {parent.name} (child-parent)")
    assert "brigade.run-diff.v1" not in human
    assert "lifecycle: changed" in human
    assert "graphtrail: skipped (absent snapshots)" in human


def test_diff_drops_unexpected_contract_keys(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent, extra={"unexpected_extra": "should-be-dropped"})
    _write_run(
        child,
        lineage={**_child_lineage(parent.name), "unexpected_extra": "should-be-dropped"},
        extra={"unexpected_extra": "should-be-dropped"},
    )
    _write_json(
        child / "graph-delta.json",
        _compatible_graphtrail(after_sha="c" * 64, extra={"unexpected_extra": "should-be-dropped"}),
    )
    _write_json(
        parent / "graph-delta.json",
        _compatible_graphtrail(after_sha="c" * 64, extra={"unexpected_extra": "should-be-dropped"}),
    )

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert "unexpected_extra" not in raw
    assert set(payload) == runs_diff.RUN_DIFF_KEYS
    assert payload["graphtrail"]["status"] == "compared"
    assert payload["graphtrail"]["changed"] is False


def test_diff_bounds_oversized_branch_point_and_graphtrail_status(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_run(parent)
    _write_run(child, lineage=_child_lineage(parent.name, branch="y" * 50_000))
    _write_json(
        child / "graph-delta.json",
        _compatible_graphtrail(after_sha="c" * 64, extra={"status": "ok" + ("z" * 50_000)}),
    )
    _write_json(parent / "graph-delta.json", _compatible_graphtrail(after_sha="c" * 64))

    assert cli.main(["runs", "diff", child.name, "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["branch_point_event_id"]) <= 400
    assert payload["branch_point_event_id"].endswith("...")
    assert payload["graphtrail"]["status"] == "compared"
    left_status = payload["graphtrail"]["left"]["status"]
    assert len(left_status) <= 400
    assert left_status.endswith("...")
    assert "z" * 50_000 not in json.dumps(payload)
