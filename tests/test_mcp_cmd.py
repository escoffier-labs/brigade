"""Engine behavior: init/add/list/plan/sync/doctor/import + merge/ownership semantics."""

from __future__ import annotations

import json

from brigade import mcp_cmd


def _init(target):
    assert mcp_cmd.init(target=target, json_output=True) == 0


def _add_github(target, **kw):
    return mcp_cmd.add(
        target=target,
        name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env=["GITHUB_TOKEN=ref:GITHUB_TOKEN"],
        timeout=60,
        json_output=True,
        **kw,
    )


def _payload(capsys):
    return json.loads(capsys.readouterr().out)


def test_init_creates_canonical_and_gitignore(tmp_path, capsys):
    assert mcp_cmd.init(target=tmp_path, json_output=True) == 0
    payload = _payload(capsys)
    assert (tmp_path / ".brigade/mcp.json").is_file()
    assert payload["gitignore_updated"] is True
    gi = (tmp_path / ".gitignore").read_text()
    assert "!.brigade/mcp.json" in gi and ".brigade/mcp/" in gi


def test_init_refuses_overwrite_without_force(tmp_path):
    _init(tmp_path)
    assert mcp_cmd.init(target=tmp_path, json_output=True) == 3


def test_add_then_list(tmp_path, capsys):
    _init(tmp_path)
    capsys.readouterr()
    assert _add_github(tmp_path) == 0
    capsys.readouterr()
    assert mcp_cmd.list_servers(target=tmp_path, json_output=True) == 0
    payload = _payload(capsys)
    assert payload["count"] == 1
    assert payload["servers"][0]["env_refs"] == ["GITHUB_TOKEN"]


def test_add_rejects_high_risk_command(tmp_path):
    _init(tmp_path)
    rc = mcp_cmd.add(target=tmp_path, name="x", command="bash -c evil", timeout=5, json_output=True)
    assert rc == 2


def test_sync_dry_run_writes_nothing(tmp_path):
    _init(tmp_path)
    _add_github(tmp_path)
    assert mcp_cmd.sync(target=tmp_path, json_output=True) == 0  # no --write
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".cursor/mcp.json").exists()


def test_sync_write_creates_all_repo_scoped_targets(tmp_path):
    _init(tmp_path)
    _add_github(tmp_path)
    assert mcp_cmd.sync(target=tmp_path, write=True, json_output=True) == 0
    claude = json.loads((tmp_path / ".mcp.json").read_text())
    assert "github" in claude["mcpServers"]
    assert claude["mcpServers"]["github"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
    assert (tmp_path / ".cursor/mcp.json").is_file()
    assert (tmp_path / ".codex/config.toml").is_file()
    assert (tmp_path / ".vscode/mcp.json").is_file()
    assert (tmp_path / "opencode.json").is_file()
    # user-scoped antigravity is NOT written without --user-scope
    assert "antigravity" not in json.loads((tmp_path / ".brigade/mcp/state.json").read_text())["ownership"]


def test_sync_is_idempotent(tmp_path, capsys):
    _init(tmp_path)
    _add_github(tmp_path)
    mcp_cmd.sync(target=tmp_path, write=True, json_output=True)
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, write=True, json_output=True) == 0
    payload = _payload(capsys)
    assert payload["counts"]["create"] == 0
    assert all(i["status"] == "current" for i in payload["items"])


def test_merge_preserves_foreign_server(tmp_path):
    _init(tmp_path)
    _add_github(tmp_path)
    target_file = tmp_path / ".mcp.json"
    target_file.write_text(json.dumps({"mcpServers": {"local": {"command": "mylocal"}}}))
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    doc = json.loads(target_file.read_text())
    assert doc["mcpServers"]["local"] == {"command": "mylocal"}  # untouched
    assert "github" in doc["mcpServers"]


def test_foreign_same_name_conflicts_then_adopts(tmp_path, capsys):
    _init(tmp_path)
    _add_github(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "hand-rolled"}}}))
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 1  # conflict rc
    payload = _payload(capsys)
    statuses = {i["status"] for i in payload["items"]}
    assert "foreign" in statuses
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["github"]["command"] == "hand-rolled"
    # --adopt takes ownership and overwrites with the canonical value
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, adopt=True, json_output=True) == 0
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["github"]["command"] == "npx"


def test_user_edit_conflicts_then_force(tmp_path, capsys):
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
    assert any(i["status"] == "conflicted" for i in payload["items"])
    # untouched without --force
    assert json.loads(target_file.read_text())["mcpServers"]["github"]["command"] == "edited-by-user"
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, force=True, json_output=True) == 0
    assert json.loads(target_file.read_text())["mcpServers"]["github"]["command"] == "npx"


def test_prune_removes_only_pristine_orphan(tmp_path):
    _init(tmp_path)
    _add_github(tmp_path)
    mcp_cmd.add(target=tmp_path, name="docs", transport="http", url="https://x/v1", timeout=10, json_output=True)
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    # drop docs from canonical
    servers, _, _ = mcp_cmd.load_canonical(tmp_path)
    del servers["docs"]
    mcp_cmd._write_canonical(tmp_path, servers)
    # without --prune, docs stays
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    assert "docs" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    # with --prune, pristine docs is removed
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, prune=True, json_output=True)
    assert "docs" not in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    assert "github" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]


def test_prune_skips_edited_orphan(tmp_path):
    _init(tmp_path)
    mcp_cmd.add(target=tmp_path, name="docs", transport="http", url="https://x/v1", timeout=10, json_output=True)
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    f = tmp_path / ".mcp.json"
    doc = json.loads(f.read_text())
    doc["mcpServers"]["docs"]["url"] = "https://edited/v1"  # user edits the orphan-to-be
    f.write_text(json.dumps(doc))
    servers, _, _ = mcp_cmd.load_canonical(tmp_path)
    del servers["docs"]
    mcp_cmd._write_canonical(tmp_path, servers)
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, prune=True, json_output=True)
    assert json.loads(f.read_text())["mcpServers"]["docs"]["url"] == "https://edited/v1"  # left alone


def test_state_loss_reconciles_ownership(tmp_path, capsys):
    _init(tmp_path)
    _add_github(tmp_path)
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    (tmp_path / ".brigade/mcp/state.json").unlink()  # simulate fresh clone
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0
    payload = _payload(capsys)
    gh = [i for i in payload["items"] if i["server"] == "github"][0]
    assert gh["status"] == "current"  # reconciled, not conflict
    state = json.loads((tmp_path / ".brigade/mcp/state.json").read_text())
    assert "github" in state["ownership"]["claude"][".mcp.json"]


def test_targets_scopes_to_subset(tmp_path):
    _init(tmp_path)
    mcp_cmd.add(target=tmp_path, name="only", command="npx", timeout=5, targets=["claude"], json_output=True)
    mcp_cmd.sync(target=tmp_path, write=True, json_output=True)
    assert "only" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    assert not (tmp_path / ".cursor/mcp.json").exists()  # not targeted


def test_name_filter_does_not_prune_others(tmp_path):
    _init(tmp_path)
    _add_github(tmp_path)
    mcp_cmd.add(target=tmp_path, name="docs", transport="http", url="https://x/v1", timeout=10, json_output=True)
    mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True)
    # sync only github with --prune: docs must survive
    mcp_cmd.sync(target=tmp_path, harness="claude", name="github", write=True, prune=True, json_output=True)
    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert "github" in doc["mcpServers"] and "docs" in doc["mcpServers"]


def test_doctor_clean_and_dirty(tmp_path, capsys):
    _init(tmp_path)
    _add_github(tmp_path)
    capsys.readouterr()
    assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
    # add a server with an inlined secret -> warn, still valid (rc 0)
    mcp_cmd.add(target=tmp_path, name="bad", command="npx", env=["API_KEY=literal:sk-123"], timeout=5, json_output=True)
    capsys.readouterr()
    assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
    payload = _payload(capsys)
    assert any("inlined secret" in i["message"] for i in payload["issues"])


def test_doctor_errors_when_no_canonical(tmp_path, capsys):
    assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 1


def test_import_preview_then_merge(tmp_path, capsys):
    _init(tmp_path)
    # seed an existing cursor config with a literal secret
    cursor = tmp_path / ".cursor/mcp.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text(json.dumps({"mcpServers": {"gh": {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_secret"}}}}))
    capsys.readouterr()
    assert mcp_cmd.import_servers(target=tmp_path, harness="cursor", json_output=True) == 0
    payload = _payload(capsys)
    assert payload["discovered"] == ["gh"]
    assert payload["merged"] is False
    assert not mcp_cmd.load_canonical(tmp_path)[0]  # preview only, nothing written
    # merge demotes the secret to a ref
    assert mcp_cmd.import_servers(target=tmp_path, harness="cursor", merge=True, json_output=True) == 0
    servers, _, _ = mcp_cmd.load_canonical(tmp_path)
    assert servers["gh"].env["GITHUB_TOKEN"] == {"ref": "GITHUB_TOKEN"}


def test_unsupported_harness_reported_by_doctor(tmp_path, capsys):
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    _init(tmp_path)
    write_config(tmp_path, Config(version=1, selection=Selection(depth="repo", harnesses=["claude", "aider"])))
    capsys.readouterr()
    mcp_cmd.doctor(target=tmp_path, json_output=True)
    payload = _payload(capsys)
    assert "aider" in payload["unsupported_harnesses"]
