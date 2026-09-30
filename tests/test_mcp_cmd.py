"""Engine behavior: init/add/list/plan/sync/doctor/import + merge/ownership semantics."""

from __future__ import annotations

import json
import subprocess

import pytest

from brigade import mcp_adapters, mcp_cmd


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


def _git(repo, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_init_creates_canonical_and_gitignore(tmp_path, capsys):
    assert mcp_cmd.init(target=tmp_path, json_output=True) == 0
    payload = _payload(capsys)
    assert (tmp_path / ".brigade/mcp.json").is_file()
    assert payload["gitignore_updated"] is True
    gi = (tmp_path / ".gitignore").read_text()
    assert "!.brigade/mcp.json" in gi and ".brigade/mcp/" in gi


def test_init_repairs_parent_brigade_ignore_so_catalog_is_trackable(tmp_path, capsys):
    assert _git(tmp_path, "init").returncode == 0
    (tmp_path / ".gitignore").write_text("# user rules\n*.log\n.brigade/\n")

    assert mcp_cmd.init(target=tmp_path, json_output=True) == 0

    payload = _payload(capsys)
    assert payload["gitignore_updated"] is True
    gi = (tmp_path / ".gitignore").read_text()
    assert "# user rules" in gi
    assert "*.log" in gi
    assert _git(tmp_path, "check-ignore", ".brigade/mcp.json").returncode == 1
    assert _git(tmp_path, "check-ignore", ".brigade/mcp/state.json").returncode == 0


def test_init_repairs_later_gitignore_rule_shadowing_valid_mcp_snippet(tmp_path, capsys):
    assert _git(tmp_path, "init").returncode == 0
    (tmp_path / ".gitignore").write_text(
        "\n".join(
            [
                "# user rules",
                "!.brigade/",
                ".brigade/*",
                "!.brigade/mcp.json",
                ".brigade/mcp/",
                "*.json",
            ]
        )
        + "\n"
    )

    assert _git(tmp_path, "check-ignore", ".brigade/mcp.json").returncode == 0

    assert mcp_cmd.init(target=tmp_path, json_output=True) == 0

    payload = _payload(capsys)
    assert payload["gitignore_updated"] is True
    gi = (tmp_path / ".gitignore").read_text()
    assert gi.startswith("# user rules\n")
    assert "*.json" in gi
    assert _git(tmp_path, "check-ignore", ".brigade/mcp.json").returncode == 1
    assert _git(tmp_path, "check-ignore", ".brigade/mcp/state.json").returncode == 0


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
    (tmp_path / ".vscode").mkdir()
    assert mcp_cmd.sync(target=tmp_path, write=True, json_output=True) == 0
    claude = json.loads((tmp_path / ".mcp.json").read_text())
    assert "github" in claude["mcpServers"]
    assert claude["mcpServers"]["github"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
    assert (tmp_path / ".cursor/mcp.json").is_file()
    assert (tmp_path / ".codex/config.toml").is_file()
    assert (tmp_path / ".grok/config.toml").is_file()
    assert (tmp_path / ".vscode/mcp.json").is_file()
    assert (tmp_path / "opencode.json").is_file()
    # user-scoped antigravity is NOT written without --user-scope
    assert "antigravity" not in json.loads((tmp_path / ".brigade/mcp/state.json").read_text())["ownership"]


def test_sync_skips_vscode_when_repo_has_no_vscode_dir(tmp_path, capsys):
    # A repo that never used VS Code must not grow a .vscode/ directory
    # from a plain sync (audit 2026-07-02, backlog item 2).
    _init(tmp_path)
    _add_github(tmp_path)
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, write=True, json_output=True) == 0
    payload = _payload(capsys)
    assert not (tmp_path / ".vscode").exists()
    assert any("vscode" in note for note in payload["notes"])


def test_sync_explicit_vscode_harness_still_writes(tmp_path):
    _init(tmp_path)
    _add_github(tmp_path)
    assert mcp_cmd.sync(target=tmp_path, harness="vscode", write=True, json_output=True) == 0
    assert (tmp_path / ".vscode/mcp.json").is_file()


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


def test_codex_import_sync_preserves_native_auth_and_unmanaged_config(tmp_path, capsys):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    existing = """model = "fake-model"
[mcp_servers.docs]
url = "https://mcp.example.com/v1"
bearer_token_env_var = "FAKE_TOKEN"
http_headers = { Authorization = "Bearer fake-static" }
env_http_headers = { Authorization = "FAKE_HEADER" }
enabled_tools = ["fake-tool"]
startup_timeout_sec = 25
[mcp_servers.docs.oauth]
client_id = "fake-client"
[mcp_servers.docs.tools.fake-tool]
approval_mode = "prompt"
[mcp_servers.foreign]
url = "https://foreign.example.com/mcp"
bearer_token_env_var = "FOREIGN_TOKEN"
"""
    path.write_text(existing)
    capsys.readouterr()
    assert (
        mcp_cmd.import_servers(target=tmp_path, harness="codex", merge=True, keep_secrets=True, json_output=True) == 0
    )
    assert "fake-static" not in capsys.readouterr().out
    servers, errors, _ = mcp_cmd.load_canonical(tmp_path)
    assert errors == []
    # Leave the second native server unmanaged.
    del servers["foreign"]
    mcp_cmd._write_canonical(tmp_path, servers)
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    first = path.read_text()
    parsed = mcp_adapters.tomllib.loads(first)
    original = mcp_adapters.tomllib.loads(existing)
    assert parsed["model"] == original["model"]
    assert parsed["mcp_servers"]["foreign"] == original["mcp_servers"]["foreign"]
    assert parsed["mcp_servers"]["docs"] == {**original["mcp_servers"]["docs"], "type": "http"}
    for _ in range(2):
        capsys.readouterr()
        assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
        payload = _payload(capsys)
        assert payload["terminal_state"] == "unchanged"
        assert payload["files_written"] == []
        assert path.read_text() == first


def test_codex_sync_preserves_explicit_native_auth_over_generic_headers(tmp_path, capsys):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    path.write_text("""[mcp_servers.docs]
url = "https://mcp.example.com/v1"
http_headers = { Authorization = "Bearer native-fake" }
env_http_headers = { "X-Native" = "NATIVE_HEADER" }
bearer_token_env_var = "NATIVE_TOKEN"
required = true
""")
    raw = {
        "transport": "http",
        "url": "https://mcp.example.com/v1",
        "headers": {"Authorization": {"literal": "generic-fake"}, "X-Managed": {"ref": "MANAGED_HEADER"}},
    }
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    first = adapter.read_file(path.read_text())["docs"]
    assert first["http_headers"] == {"Authorization": "Bearer native-fake"}
    assert first["env_http_headers"] == {"X-Native": "NATIVE_HEADER", "X-Managed": "MANAGED_HEADER"}
    assert first["bearer_token_env_var"] == "NATIVE_TOKEN"
    assert first["required"] is True
    ownership = mcp_cmd.state_path(tmp_path).read_text()
    assert "native-fake" not in ownership
    assert "NATIVE_TOKEN" not in ownership
    raw["headers"]["X-Managed"] = {"ref": "CHANGED_HEADER"}
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    updated = adapter.read_file(path.read_text())["docs"]
    assert updated["http_headers"] == first["http_headers"]
    assert updated["env_http_headers"] == {"X-Native": "NATIVE_HEADER", "X-Managed": "CHANGED_HEADER"}
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert _payload(capsys)["terminal_state"] == "unchanged"


@pytest.mark.parametrize(
    "native",
    [
        {"http_headers": {"aUtHoRiZaTiOn": "Bearer fake-static"}},
        {"env_http_headers": {"aUtHoRiZaTiOn": "OLD_HEADER"}},
        {"bearer_token_env_var": "OLD_TOKEN"},
    ],
)
@pytest.mark.parametrize("initial_header", [False, True])
def test_review_changed_generic_auth_conflicts_with_adopted_native(tmp_path, capsys, native, initial_header):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    live = {"url": "https://mcp.example.com/v1", **native, "required": True}
    path.write_text(adapter.write_file(None, {"docs": live}, set()))
    raw = {"transport": "http", "url": live["url"]}
    if initial_header:
        raw["headers"] = {"Authorization": {"ref": "ORIGINAL_HEADER"}}
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    before, state_before = path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes()
    raw["headers"] = {"AUTHORIZATION": {"ref": "NEW_HEADER"}}
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, force=True, json_output=True) == 1
    payload = _payload(capsys)
    assert payload["items"][0]["reason"] == "native_auth_collision"
    assert payload["items"][0]["headers"] == ["authorization"]
    assert "explicit native canonical auth" in payload["items"][0]["detail"]
    assert path.read_bytes() == before
    assert mcp_cmd.state_path(tmp_path).read_bytes() == state_before
    for secret in ("fake-static", "OLD_HEADER", "OLD_TOKEN", "ORIGINAL_HEADER", "NEW_HEADER"):
        assert secret not in json.dumps(payload)
        assert secret not in state_before.decode()


@pytest.mark.parametrize("legacy_unchanged", [False, True])
def test_review_legacy_auth_baseline_requires_unchanged_canonical(tmp_path, capsys, legacy_unchanged):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    path.write_text('[mcp_servers.docs]\nurl = "https://mcp.example.com"\nbearer_token_env_var = "OLD_TOKEN"\n')
    raw = {"transport": "http", "url": "https://mcp.example.com", "headers": {"Authorization": {"ref": "HEADER"}}}
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    state = mcp_cmd._load_state(tmp_path)
    record = state["ownership"]["codex"][adapter.path]["docs"]
    record.pop("generic_header_fingerprints", None)
    mcp_cmd.state_path(tmp_path).write_text(json.dumps(state))
    before = path.read_bytes()
    if not legacy_unchanged:
        raw["headers"]["X-Other"] = {"literal": "unrelated"}
        server, _ = mcp_adapters.server_from_dict("docs", raw)
        mcp_cmd._write_canonical(tmp_path, {"docs": server})
    capsys.readouterr()
    rc = mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True)
    payload = _payload(capsys)
    assert rc == (0 if legacy_unchanged else 1)
    assert path.read_bytes() == before
    if legacy_unchanged:
        record = mcp_cmd._load_state(tmp_path)["ownership"]["codex"][adapter.path]["docs"]
        assert set(record["generic_header_fingerprints"]) == {"authorization"}
    else:
        assert payload["items"][0]["reason"] == "native_auth_collision"


@pytest.mark.parametrize("harness", ["codex", "codex-user"])
def test_review_default_import_demotion_removes_adopted_static_auth(tmp_path, capsys, monkeypatch, harness):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS[harness]
    path = mcp_adapters.resolve_path(adapter, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        '[mcp_servers.docs]\nurl = "https://mcp.example.com"\nhttp_headers = { aUtHoRiZaTiOn = "Bearer fake-secret", "X-Public" = "keep" }\n'
    )
    assert (
        mcp_cmd.import_servers(
            target=tmp_path, harness=harness, merge=True, user_scope=adapter.user_scope, json_output=True
        )
        == 0
    )
    for _ in range(2):
        assert (
            mcp_cmd.sync(
                target=tmp_path,
                harness=harness,
                adopt=True,
                write=True,
                user_scope=adapter.user_scope,
                json_output=True,
            )
            == 0
        )
        live = adapter.read_file(path.read_text())["docs"]
        assert live["http_headers"] == {"X-Public": "keep"}
        assert live["env_http_headers"] == {"aUtHoRiZaTiOn": "AUTHORIZATION"}
        assert "fake-secret" not in path.read_text()


def test_review_auth_collision_does_not_block_stdio_transition(tmp_path):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    path.write_text('[mcp_servers.docs]\nurl = "https://mcp.example.com"\nbearer_token_env_var = "OLD_TOKEN"\n')
    server = mcp_adapters.CanonicalServer(name="docs", transport="http", url="https://mcp.example.com")
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    stdio = mcp_adapters.CanonicalServer(
        name="docs", command="fake-command", headers={"Authorization": {"ref": "UNUSED_HEADER"}}
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": stdio})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert adapter.read_file(path.read_text())["docs"] == {"command": "fake-command"}


def test_codex_unsupported_interpolation_is_a_safe_command_error(tmp_path, capsys):
    _init(tmp_path)
    server, _ = mcp_adapters.server_from_dict(
        "docs",
        {
            "transport": "http",
            "url": "https://mcp.example.com/v1",
            "headers": {"Authorization": {"literal": "private-prefix-${FAKE_TOKEN}"}},
        },
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 2
    output = capsys.readouterr().out
    assert "unsupported" in output and "interpolation" in output
    assert "private-prefix" not in output and "FAKE_TOKEN" not in output
    assert not (tmp_path / ".codex/config.toml").exists()
    assert not mcp_cmd.state_path(tmp_path).exists()


def test_codex_remote_to_stdio_does_not_restore_remote_modeled_fields(tmp_path, capsys):
    _init(tmp_path)
    remote, _ = mcp_adapters.server_from_dict(
        "docs",
        {
            "transport": "http",
            "url": "https://mcp.example.com/v1",
            "headers": {"Authorization": {"ref": "FAKE_HEADER"}},
        },
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": remote})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    stdio = mcp_adapters.CanonicalServer(name="docs", command="fake-command")
    mcp_cmd._write_canonical(tmp_path, {"docs": stdio})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    assert adapter.read_file(path.read_text())["docs"] == {"command": "fake-command"}
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert _payload(capsys)["terminal_state"] == "unchanged"


def test_codex_stdio_to_remote_sync_does_not_restore_stdio_fields(tmp_path, capsys):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    path.write_text(
        '[mcp_servers.docs]\ncommand = "fake-command"\ncwd = "/fake"\nenv_vars = ["FAKE_ENV"]\nenabled_tools = ["keep"]\n'
    )
    stdio = mcp_adapters.CanonicalServer(name="docs", command="fake-command")
    mcp_cmd._write_canonical(tmp_path, {"docs": stdio})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    assert adapter.read_file(path.read_text())["docs"]["env_vars"] == ["FAKE_ENV"]
    remote = mcp_adapters.CanonicalServer(name="docs", transport="http", url="https://mcp.example.com/v1")
    mcp_cmd._write_canonical(tmp_path, {"docs": remote})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    table = adapter.read_file(path.read_text())["docs"]
    assert table == {"url": remote.url, "type": "http", "enabled_tools": ["keep"]}
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert _payload(capsys)["terminal_state"] == "unchanged"


def test_codex_remote_to_stdio_sync_does_not_restore_oauth_resource(tmp_path, capsys):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    path.write_text(
        '[mcp_servers.docs]\nurl = "https://mcp.example.com/v1"\ntype = "http"\n'
        'oauth_resource = "https://fake.example"\nscopes = ["keep"]\n'
    )
    remote = mcp_adapters.CanonicalServer(name="docs", transport="http", url="https://mcp.example.com/v1")
    mcp_cmd._write_canonical(tmp_path, {"docs": remote})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    assert adapter.read_file(path.read_text())["docs"]["oauth_resource"] == "https://fake.example"
    stdio = mcp_adapters.CanonicalServer(name="docs", command="fake-command")
    mcp_cmd._write_canonical(tmp_path, {"docs": stdio})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert adapter.read_file(path.read_text())["docs"] == {"command": "fake-command", "scopes": ["keep"]}
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert _payload(capsys)["terminal_state"] == "unchanged"


def test_codex_generated_auth_can_be_removed_without_restoring_live_fields(tmp_path, capsys):
    _init(tmp_path)
    raw = {
        "transport": "http",
        "url": "https://mcp.example.com/v1",
        "http_headers": {"X-Static": "fake"},
        "env_http_headers": {"X-Env": "FAKE_HEADER"},
        "bearer_token_env_var": "FAKE_TOKEN",
    }
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    server = mcp_adapters.CanonicalServer(name="docs", transport="http", url=raw["url"])
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    adapter = mcp_adapters.ADAPTERS["codex"]
    assert adapter.read_file((tmp_path / adapter.path).read_text())["docs"] == {"url": raw["url"], "type": "http"}
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert _payload(capsys)["terminal_state"] == "unchanged"


def test_unsupported_harness_reported_by_doctor(tmp_path, capsys):
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    _init(tmp_path)
    write_config(tmp_path, Config(version=1, selection=Selection(depth="repo", harnesses=["claude", "aider"])))
    capsys.readouterr()
    mcp_cmd.doctor(target=tmp_path, json_output=True)
    payload = _payload(capsys)
    assert "aider" in payload["unsupported_harnesses"]


def test_grok_harness_is_supported_by_doctor(tmp_path, capsys):
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    _init(tmp_path)
    write_config(tmp_path, Config(version=1, selection=Selection(depth="repo", harnesses=["grok"])))
    capsys.readouterr()
    assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
    payload = _payload(capsys)
    assert "grok" not in payload["unsupported_harnesses"]


def test_review_imported_auth_can_be_removed_after_adoption(tmp_path, capsys):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    path.write_text("""[mcp_servers.docs]
url = "https://mcp.example.com"
http_headers = { "X-Static" = "fake-public" }
env_http_headers = { "X-Env" = "FAKE_HEADER" }
bearer_token_env_var = "FAKE_TOKEN"
""")
    assert (
        mcp_cmd.import_servers(target=tmp_path, harness="codex", merge=True, keep_secrets=True, json_output=True) == 0
    )
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    server, _ = mcp_adapters.server_from_dict("docs", {"transport": "http", "url": "https://mcp.example.com"})
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert adapter.read_file(path.read_text())["docs"] == {"url": server.url, "type": "http"}
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert _payload(capsys)["terminal_state"] == "unchanged"


def test_review_adoption_tracks_only_unowned_native_map_entries(tmp_path, capsys):
    _init(tmp_path)
    adapter = mcp_adapters.ADAPTERS["codex"]
    path = tmp_path / adapter.path
    path.parent.mkdir()
    path.write_text("""[mcp_servers.docs]
url = "https://mcp.example.com"
http_headers = { "X-Owned" = "old-public", "X-Local" = "local-public" }
env_http_headers = { "X-Owned" = "OLD_HEADER", "X-Local" = "LOCAL_HEADER" }
bearer_token_env_var = "OLD_TOKEN"
""")
    raw = {
        "transport": "http",
        "url": "https://mcp.example.com",
        "http_headers": {"X-Owned": "new-public"},
        "env_http_headers": {"X-Owned": "NEW_HEADER"},
        "bearer_token_env_var": "NEW_TOKEN",
    }
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", adopt=True, write=True, json_output=True) == 0
    for field in ("http_headers", "env_http_headers", "bearer_token_env_var"):
        raw.pop(field)
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert adapter.read_file(path.read_text())["docs"] == {
        "url": server.url,
        "type": "http",
        "http_headers": {"X-Local": "local-public"},
        "env_http_headers": {"X-Local": "LOCAL_HEADER"},
    }


def test_review_import_error_names_server_without_auth_value(tmp_path, capsys):
    _init(tmp_path)
    path = tmp_path / mcp_adapters.ADAPTERS["codex"].path
    path.parent.mkdir()
    path.write_text("""[mcp_servers.broken]
url = "https://mcp.example.com"
bearer_token_env_var = "fake-invalid-value!"
""")
    capsys.readouterr()
    assert mcp_cmd.import_servers(target=tmp_path, harness="codex", json_output=True) == 2
    errors = _payload(capsys)["errors"]
    assert "broken:" in errors[0]
    assert "fake-invalid-value" not in errors[0]
