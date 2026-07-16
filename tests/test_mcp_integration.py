"""MCP user-scope writes, operator sync-mcp, and station/CLI registration."""

from __future__ import annotations

import json

import pytest

from brigade import cli, doctor, mcp_cmd, operator_cmd, registry


def _seed(target):
    mcp_cmd.init(target=target, json_output=True)
    mcp_cmd.add(
        target=target,
        name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env=["GITHUB_TOKEN=ref:GITHUB_TOKEN"],
        timeout=60,
        json_output=True,
    )


# --- user-scope (antigravity writes under $HOME, gated by --user-scope) --- #


def test_user_scope_antigravity_writes_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo)
    # Default sync never touches the user-global antigravity config.
    mcp_cmd.sync(target=repo, write=True, json_output=True)
    assert not (home / ".gemini/config/mcp_config.json").exists()
    # With --user-scope it writes under $HOME using serverUrl-style mcpServers.
    mcp_cmd.sync(target=repo, harness="antigravity", user_scope=True, write=True, json_output=True)
    cfg = home / ".gemini/config/mcp_config.json"
    assert cfg.is_file()
    assert "github" in json.loads(cfg.read_text())["mcpServers"]


def test_cursor_user_scope_uses_home_config_and_reports_paths(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo)

    capsys.readouterr()
    assert mcp_cmd.plan(target=repo, harness="cursor", user_scope=True, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    destination = home / ".cursor" / "mcp.json"
    assert plan["source_catalog"] == str(repo / ".brigade" / "mcp.json")
    assert plan["destination_files"] == [str(destination)]
    assert {item["file"] for item in plan["items"]} == {"~/.cursor/mcp.json"}

    assert (
        mcp_cmd.sync(
            target=repo,
            harness="cursor",
            user_scope=True,
            write=True,
            json_output=True,
        )
        == 0
    )
    assert "github" in json.loads(destination.read_text())["mcpServers"]
    assert not (repo / ".cursor" / "mcp.json").exists()


def test_broad_user_scope_does_not_select_cursor_global_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo)

    capsys.readouterr()
    assert mcp_cmd.plan(target=repo, user_scope=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "cursor-user" not in payload["harnesses"]
    assert str(home / ".cursor" / "mcp.json") not in payload["destination_files"]


def test_cursor_user_scope_accepts_global_target_alias(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)
    mcp_cmd.add(
        target=repo,
        name="global-only",
        command="global-mcp",
        targets=["cursor-user"],
        json_output=True,
    )

    capsys.readouterr()
    assert mcp_cmd.plan(target=repo, harness="cursor", user_scope=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(item["server"], item["action"]) for item in payload["items"]] == [("global-only", "create")]


def test_cursor_user_scope_normalizes_repo_graphtrail_pin_and_preserves_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    cursor_config = home / ".cursor" / "mcp.json"
    cursor_config.parent.mkdir(parents=True)
    cursor_config.write_text(
        json.dumps(
            {
                "theme": "keep",
                "mcpServers": {
                    "private": {
                        "command": "private-mcp",
                        "env": {"API_TOKEN": "keep-this-secret"},
                    }
                },
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)
    repo_db = repo / ".graphtrail" / "graphtrail.db"
    mcp_cmd.add(
        target=repo,
        name="graphtrail",
        command="graphtrail",
        args=["mcp", "--db", str(repo_db), "--verbose"],
        timeout=60,
        targets=["cursor"],
        json_output=True,
    )

    before = cursor_config.read_text()
    capsys.readouterr()
    assert mcp_cmd.sync(target=repo, harness="cursor", user_scope=True, json_output=True) == 0
    first_dry_run = json.loads(capsys.readouterr().out)
    assert first_dry_run["counts"]["create"] == 1
    assert cursor_config.read_text() == before

    assert (
        mcp_cmd.sync(
            target=repo,
            harness="cursor",
            user_scope=True,
            write=True,
            json_output=True,
        )
        == 0
    )
    written = json.loads(cursor_config.read_text())
    assert written["theme"] == "keep"
    assert written["mcpServers"]["private"]["env"]["API_TOKEN"] == "keep-this-secret"
    assert written["mcpServers"]["graphtrail"]["args"] == ["mcp", "--verbose"]

    capsys.readouterr()
    assert mcp_cmd.sync(target=repo, harness="cursor", user_scope=True, json_output=True) == 0
    second_dry_run = json.loads(capsys.readouterr().out)
    assert second_dry_run["counts"] == {"create": 0, "update": 0, "skip": 1, "conflict": 0, "remove": 0}
    assert second_dry_run["items"][0]["status"] == "current"


def test_cursor_user_scope_preserves_relative_graphtrail_pin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)
    mcp_cmd.add(
        target=repo,
        name="graphtrail",
        command="graphtrail",
        args=["mcp", "--db", ".graphtrail/custom.db"],
        targets=["cursor"],
        json_output=True,
    )

    assert mcp_cmd.sync(target=repo, harness="cursor", user_scope=True, write=True, json_output=True) == 0
    written = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert written["mcpServers"]["graphtrail"]["args"] == ["mcp", "--db", ".graphtrail/custom.db"]


def test_cursor_user_scope_preserves_remote_graphtrail_shape(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)
    mcp_cmd.add(
        target=repo,
        name="graphtrail",
        transport="http",
        url="https://example.test/mcp",
        args=["--db", str(repo / ".graphtrail/graphtrail.db"), "--verbose"],
        targets=["cursor"],
        json_output=True,
    )

    assert mcp_cmd.sync(target=repo, harness="cursor", user_scope=True, write=True, json_output=True) == 0
    projected = json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["graphtrail"]
    assert projected == {"url": "https://example.test/mcp", "type": "http"}


@pytest.mark.parametrize(
    ("invalid", "error"),
    [
        (
            '{"mcpServers": {"private": {"command": "private-mcp"}},}',
            "existing JSON configuration is invalid; refusing to overwrite",
        ),
        (
            '[{"mcpServers": {}}]',
            "existing JSON configuration must be an object; refusing to overwrite",
        ),
        (
            '{"mcpServers": [{"command": "private-mcp"}]}',
            "existing mcpServers section must be an object; refusing to overwrite",
        ),
    ],
)
def test_cursor_user_scope_rejects_invalid_existing_config(tmp_path, monkeypatch, capsys, invalid, error):
    home = tmp_path / "home"
    cursor_config = home / ".cursor" / "mcp.json"
    cursor_config.parent.mkdir(parents=True)
    cursor_config.write_text(invalid)
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo)

    capsys.readouterr()
    assert mcp_cmd.sync(target=repo, harness="cursor", user_scope=True, write=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == [f"{cursor_config}: {error}"]
    assert cursor_config.read_text() == invalid


def test_cursor_user_scope_import_reads_home_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    cursor_config = home / ".cursor" / "mcp.json"
    cursor_config.parent.mkdir(parents=True)
    cursor_config.write_text(json.dumps({"mcpServers": {"global": {"command": "global-mcp"}}}))
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)

    capsys.readouterr()
    assert (
        mcp_cmd.import_servers(
            target=repo,
            harness="cursor",
            user_scope=True,
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["harness"] == "cursor-user"
    assert payload["discovered"] == ["global"]


def test_user_scope_grok_writes_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo)
    # Default sync never touches the user-global grok config.
    mcp_cmd.sync(target=repo, write=True, json_output=True)
    assert not (home / ".grok/config.toml").exists()
    # Plan with --user-scope includes grok-user.
    capsys.readouterr()
    assert mcp_cmd.plan(target=repo, harness="grok-user", user_scope=True, json_output=True) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert any(item.get("harness") == "grok-user" for item in plan_payload.get("items", []))
    # With --user-scope it writes under $HOME using codex-shaped TOML mcp_servers.
    assert mcp_cmd.sync(target=repo, harness="grok-user", user_scope=True, write=True, json_output=True) == 0
    cfg = home / ".grok/config.toml"
    assert cfg.is_file()
    text = cfg.read_text()
    assert "[mcp_servers.github]" in text
    assert "command" in text
    # Import without --user-scope is refused; with the flag it discovers the synced server.
    assert mcp_cmd.import_servers(target=repo, harness="grok-user", json_output=True) == 2
    capsys.readouterr()
    assert mcp_cmd.import_servers(target=repo, harness="grok-user", user_scope=True, json_output=True) == 0
    import_payload = json.loads(capsys.readouterr().out)
    assert "github" in import_payload.get("discovered", [])


def test_user_scope_required_for_antigravity_import(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)
    assert mcp_cmd.import_servers(target=repo, harness="antigravity", json_output=True) == 2


def test_codex_user_stdio_no_args_never_stays_conflicted(tmp_path, monkeypatch, capsys):
    """Issue #181: a no-args stdio server must not be "conflicted" forever.

    ``_codex_render_table`` omits an empty ``args = []`` from the rendered TOML, so
    reading the file back never carries an "args" key. If the fingerprint recorded at
    sync time was computed against a provider dict that DID include ``"args": []``,
    every later plan sees a live/projected fingerprint mismatch and reports the server
    "conflicted" forever, even though nothing was ever edited outside Brigade.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)
    mcp_cmd.add(target=repo, name="noargs", command="some-mcp-server", json_output=True)

    capsys.readouterr()
    rc = mcp_cmd.sync(target=repo, harness="codex-user", user_scope=True, write=True, force=True, json_output=True)
    assert rc == 0
    sync_payload = json.loads(capsys.readouterr().out)
    assert sync_payload["counts"]["conflict"] == 0

    # Plan again, twice, exactly as `brigade mcp plan --harness codex-user --user-scope`
    # would after a real sync --write --force. A forever-conflicted server never clears.
    for _ in range(2):
        capsys.readouterr()
        rc = mcp_cmd.plan(target=repo, harness="codex-user", user_scope=True, json_output=True)
        payload = json.loads(capsys.readouterr().out)
        statuses = {item["server"]: item["status"] for item in payload["items"]}
        assert statuses.get("noargs") in ("current", "skip"), payload["items"]
        assert payload["counts"]["conflict"] == 0
        assert rc == 0


# --- operator sync-mcp (three-phase, dry-run default) --- #


def test_operator_sync_mcp_dry_run_default(tmp_path, capsys):
    _seed(tmp_path)
    capsys.readouterr()
    assert operator_cmd.sync_mcp(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["write"] is False
    assert payload["status"] == "ok"
    assert not (tmp_path / ".mcp.json").exists()


def test_operator_sync_mcp_write(tmp_path, capsys):
    _seed(tmp_path)
    capsys.readouterr()
    assert operator_cmd.sync_mcp(target=tmp_path, write=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["write"] is True
    assert payload["sync"]["counts"]["create"] > 0
    assert (tmp_path / ".mcp.json").is_file()


def test_operator_sync_mcp_warns_on_invalid_catalog(tmp_path, capsys):
    # no canonical file -> doctor fails -> sync-mcp returns 1 and never writes
    assert operator_cmd.sync_mcp(target=tmp_path, write=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "warn"
    assert payload["sync"] is None


# --- station + CLI registration --- #


def test_mcp_station_registered_by_name_and_alias():
    assert registry.resolve("mcp") is not None
    assert registry.resolve("brigadier") is not None
    assert registry.resolve("mcp") in registry.all_stations()


def test_mcp_in_command_groups():
    names = [name for _, names in cli.COMMAND_GROUPS for name in names]
    assert "mcp" in names


def test_mcp_cli_init_via_main(tmp_path):
    assert cli.main(["mcp", "init", "--target", str(tmp_path), "--json"]) == 0
    assert (tmp_path / ".brigade/mcp.json").is_file()


def test_mcp_cli_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.main(["mcp"])


def test_mcp_cli_sync_args_with_leading_dash(tmp_path):
    # --args is a single shlex string so leading-dash args (e.g. -y) parse cleanly.
    assert cli.main(["mcp", "init", "--target", str(tmp_path), "--json"]) == 0
    rc = cli.main(
        [
            "mcp",
            "add",
            "--target",
            str(tmp_path),
            "--name",
            "gh",
            "--command",
            "npx",
            "--args",
            "-y @scope/pkg",
            "--timeout",
            "30",
            "--json",
        ]
    )
    assert rc == 0
    servers, _, _ = mcp_cmd.load_canonical(tmp_path)
    assert servers["gh"].args == ("-y", "@scope/pkg")


def test_mcp_station_doctor_reports_cleanly(tmp_path):
    _seed(tmp_path)
    ctx = doctor.build_context(tmp_path)
    results = doctor.mcp_station_checks(ctx)
    assert results
    assert all(status != doctor.FAIL for status, _, _ in results)


def test_mcp_station_doctor_info_when_uninitialized(tmp_path):
    ctx = doctor.build_context(tmp_path)
    results = doctor.mcp_station_checks(ctx)
    assert any(status == doctor.INFO for status, _, _ in results)
