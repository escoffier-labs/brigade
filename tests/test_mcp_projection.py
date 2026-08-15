"""MCP sync transactional commit against the projection kernel (#911)."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from brigade import mcp_cmd, projection


def _init(target: Path) -> None:
    assert mcp_cmd.init(target=target, json_output=True) == 0


def _add_github(target: Path) -> None:
    assert (
        mcp_cmd.add(
            target=target,
            name="github",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env=["GITHUB_TOKEN=ref:GITHUB_TOKEN"],
            timeout=60,
            json_output=True,
        )
        == 0
    )


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _snapshot(paths: list[Path]) -> dict[str, bytes | None]:
    return {str(path): path.read_bytes() if path.is_file() else None for path in paths}


def test_sync_plan_includes_every_selected_destination_and_ownership(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    (tmp_path / ".vscode").mkdir()
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, json_output=True) == 0
    payload = _payload(capsys)
    labels = {item["display_path"] for item in payload["projection"]["destinations"]}
    assert ".mcp.json" in labels
    assert ".cursor/mcp.json" in labels
    assert ".codex/config.toml" in labels
    assert ".grok/config.toml" in labels
    assert ".vscode/mcp.json" in labels
    assert "opencode.json" in labels
    assert ".brigade/mcp/state.json" in labels
    assert payload["terminal_state"] == "planned"
    assert payload["operation_id"]
    assert payload["wrote"] is False
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".brigade/mcp/state.json").exists()


def test_idempotent_sync_writes_nothing_and_creates_no_recovery(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    assert mcp_cmd.sync(target=tmp_path, write=True, json_output=True) == 0
    capsys.readouterr()
    before_files = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert mcp_cmd.sync(target=tmp_path, write=True, json_output=True) == 0
    payload = _payload(capsys)
    assert payload["terminal_state"] == "unchanged"
    assert payload["files_written"] == []
    assert payload["operation_id"] is None
    assert projection.unfinished_operations(tmp_path) == []
    after_files = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after_files == before_files


def test_foreign_servers_remain_preserved(tmp_path: Path) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    target_file = tmp_path / ".mcp.json"
    target_file.write_text(json.dumps({"mcpServers": {"local": {"command": "mylocal"}}}))
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0
    doc = json.loads(target_file.read_text())
    assert doc["mcpServers"]["local"] == {"command": "mylocal"}
    assert "github" in doc["mcpServers"]


def test_user_edit_still_conflicts_unless_force(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    target_file = tmp_path / ".mcp.json"
    doc = json.loads(target_file.read_text())
    doc["mcpServers"]["github"]["command"] = "edited-by-user"
    target_file.write_text(json.dumps(doc))
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 1
    payload = _payload(capsys)
    assert any(item["status"] == "conflicted" for item in payload["items"])
    assert json.loads(target_file.read_text())["mcpServers"]["github"]["command"] == "edited-by-user"
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, force=True, json_output=True) == 0
    assert json.loads(target_file.read_text())["mcpServers"]["github"]["command"] == "npx"


def test_foreign_conflict_does_not_create_ownership_state(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    target_file = tmp_path / ".mcp.json"
    target_file.write_text(json.dumps({"mcpServers": {"github": {"command": "foreign"}}}))
    before = target_file.read_bytes()
    assert not mcp_cmd.state_path(tmp_path).exists()
    capsys.readouterr()

    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 1
    payload = _payload(capsys)

    assert payload["wrote"] is False
    assert payload["terminal_state"] == "unchanged"
    assert target_file.read_bytes() == before
    assert not mcp_cmd.state_path(tmp_path).exists()


def _claude_and_ownership(tmp_path: Path) -> list[Path]:
    return [tmp_path / ".mcp.json", tmp_path / ".brigade" / "mcp" / "state.json"]


def test_failure_before_and_after_each_destination_restores(tmp_path: Path) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"keep": {"command": "foreign"}}}))
    before = _snapshot(_claude_and_ownership(tmp_path))
    plan_payload = mcp_cmd.build_sync_plan(target=tmp_path, harness="claude")
    assert plan_payload.projection is not None
    points = [
        f"commit:{index}:{boundary}"
        for index, _mutation in enumerate(
            sorted(plan_payload.projection.mutations, key=lambda item: item.destination.as_posix())
        )
        for boundary in ("before", "after")
    ]
    assert points
    for point in points:
        for path, data in before.items():
            dest = Path(path)
            if data is None:
                dest.unlink(missing_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        rc = mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True, inject_failure=point)
        assert rc == 2
        assert _snapshot(_claude_and_ownership(tmp_path)) == before


def test_prune_rollback_restores_pruned_entry(tmp_path: Path) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    mcp_cmd.add(
        target=tmp_path, name="docs", transport="http", url="https://example.com/v1", timeout=10, json_output=True
    )
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    servers, _, _ = mcp_cmd.load_canonical(tmp_path)
    del servers["docs"]
    mcp_cmd._write_canonical(tmp_path, servers)
    live = json.loads((tmp_path / ".mcp.json").read_text())
    assert "docs" in live["mcpServers"]
    before = (tmp_path / ".mcp.json").read_bytes()
    state_before = (tmp_path / ".brigade/mcp/state.json").read_bytes()
    plan_payload = mcp_cmd.build_sync_plan(target=tmp_path, harness="claude", prune=True)
    assert plan_payload.projection is not None
    point = next(
        f"commit:{index}:after"
        for index, mutation in enumerate(
            sorted(plan_payload.projection.mutations, key=lambda item: item.destination.as_posix())
        )
        if mutation.display_path == ".mcp.json"
    )
    rc = mcp_cmd.sync(target=tmp_path, harness="claude", write=True, prune=True, json_output=True, inject_failure=point)
    assert rc == 2
    assert (tmp_path / ".mcp.json").read_bytes() == before
    assert (tmp_path / ".brigade/mcp/state.json").read_bytes() == state_before
    assert "docs" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]


def test_destination_drift_after_planning_fails_closed(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    capsys.readouterr()
    mcp_cmd.add(
        target=tmp_path,
        name="docs",
        transport="http",
        url="https://example.com/v1",
        timeout=10,
        json_output=True,
    )
    capsys.readouterr()
    planned = mcp_cmd.build_sync_plan(target=tmp_path, harness="claude")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"drifted": {"command": "nope"}}}))
    assert planned.projection is not None
    with pytest.raises(projection.DriftError):
        projection.execute(planned.projection, target=tmp_path)
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"] == {"drifted": {"command": "nope"}}


def test_recovery_output_uses_safe_labels_and_omits_secrets(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    mcp_cmd.add(
        target=tmp_path,
        name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env=["GITHUB_TOKEN=literal:super-secret-token-value"],
        timeout=60,
        json_output=True,
    )
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0
    payload = _payload(capsys)
    rendered = json.dumps(payload)
    assert "super-secret-token-value" not in rendered
    assert str(tmp_path) not in json.dumps(payload.get("projection", {}))
    for dest in payload["projection"]["destinations"]:
        assert not dest["display_path"].startswith("/")
        assert "/home/" not in dest["display_path"]


def test_unfinished_recovery_surfaces_on_status_doctor_and_blocks_sync(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    _add_github(tmp_path)
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    capsys.readouterr()
    planned = mcp_cmd.build_sync_plan(target=tmp_path, harness="cursor")
    assert planned.projection is not None
    projection.kernel.write_crash_fixture(planned.projection, target=tmp_path, committed_count=1)
    capsys.readouterr()
    assert mcp_cmd.status(target=tmp_path, json_output=True) == 0
    status_payload = _payload(capsys)
    assert status_payload["recovery"]
    assert status_payload["recovery"][0]["terminal_state"] == "recovery-required"
    assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor_payload = _payload(capsys)
    assert any("recovery" in issue["message"] for issue in doctor_payload["issues"])
    from brigade import doctor as doctor_mod

    checks = doctor_mod.mcp_station_checks(doctor_mod.build_context(tmp_path))
    assert any(name.startswith("mcp: recovery") for _, name, _ in checks)
    capsys.readouterr()
    rc = mcp_cmd.sync(target=tmp_path, harness="cursor", write=True, json_output=True)
    assert rc == 2
    blocked = _payload(capsys)
    assert blocked["terminal_state"] == "recovery-required"
    assert "errors" in blocked


def test_mcp_recovery_ignores_other_projectors(tmp_path: Path, capsys) -> None:
    destination = tmp_path / "generic.txt"
    data = b"generic\n"
    plan = projection.build_plan(
        operation_id="generic-test",
        projector="generic",
        source_fingerprint="fixture",
        target=tmp_path,
        mutations=[
            projection.mutation(
                destination=destination,
                display_path="generic.txt",
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=hashlib.sha256(data).hexdigest(),
                staged_bytes=data,
            )
        ],
    )
    assert (
        projection.execute(plan, target=tmp_path, inject=projection.FailureInjector(crash_after=0)).terminal_state
        == "recovery-required"
    )

    assert mcp_cmd.status(target=tmp_path, json_output=True) == 0
    assert _payload(capsys)["recovery"] == []
    assert mcp_cmd.recover_operation(target=tmp_path, operation_id="generic-test", json_output=True) == 2
