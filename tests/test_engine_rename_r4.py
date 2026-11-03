"""Rename slice R4: Brigade-side compatibility for the retired engine names.

docs/phase-engine-rename.md, "R4". Legacy names keep working through v0.32 and
print nothing on stderr until R5; doctor reports them instead.
"""

from __future__ import annotations

import json
import os

import pytest

from brigade import (
    cli,
    code_graph_delta,
    config,
    cursor_user_cmd,
    doctor,
    engine_paths,
    install,
    mcp_adapters,
    mcp_cmd,
)
from brigade.operator_cmd import lifecycle
from brigade.selection import Selection
from tests._home import set_home

LEGACY_GRAPH, LEGACY_EVIDENCE = "graphtrail", "miseledger"


def _seed_index(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("db")
    os.utime(path, (mtime, mtime))


def _evidence_env(tmp_path):
    return {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }


# --------------------------------------------------------------------------- #
# doctor: both layouts, legacy variables, legacy config key
# --------------------------------------------------------------------------- #


def test_both_index_layouts_warn_with_paths_and_modification_times(tmp_path):
    env = _evidence_env(tmp_path)
    canonical, legacy = engine_paths.code_graph_db_paths(tmp_path)
    _seed_index(legacy, 1_700_000_000)
    assert engine_paths.legacy_findings(tmp_path, env=env) == []

    _seed_index(canonical, 1_800_000_000)
    [(status, name, detail, scope)] = engine_paths.legacy_findings(tmp_path, env=env)
    assert (status, name, scope) == ("WARN", "code graph index layout", "target")
    assert str(canonical) in detail and str(legacy) in detail
    assert "2027-01-15T08:00:00Z" in detail and "2023-11-14T22:13:20Z" in detail
    assert "nothing is merged or deleted" in detail
    assert engine_paths.code_graph_db(tmp_path, env=env).path == canonical


@pytest.mark.parametrize("legacy_part", ["data", "config"])
def test_both_evidence_layouts_warn_and_the_canonical_set_wins(tmp_path, legacy_part):
    env = _evidence_env(tmp_path)
    (tmp_path / "data" / "brigade" / "evidence").mkdir(parents=True)
    assert engine_paths.legacy_findings(tmp_path, env=env) == []

    legacy_dir = tmp_path / legacy_part / engine_paths.LEGACY_EVIDENCE_LEAF
    legacy_dir.mkdir(parents=True)
    [(status, name, detail, scope)] = engine_paths.legacy_findings(tmp_path, env=env)
    assert (status, name, scope) == ("WARN", "evidence directory layout", "operator")
    assert str(tmp_path / "data" / "brigade" / "evidence") in detail and str(legacy_dir) in detail
    assert engine_paths.evidence_dirs(env=env).source == "canonical"


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("GRAPHTRAIL_DB", "BRIGADE_CODE_GRAPH_DB"),
        ("GRAPHTRAIL_ALLOW_UNSAFE_ROOT", "BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT"),
        ("GRAPHTRAIL_BIN", "BRIGADE_CODE_GRAPH_BIN"),
        ("GRAPHTRAIL_MCP_BIN", "BRIGADE_CODE_GRAPH_MCP_BIN"),
        ("MISELEDGER_BIN", "BRIGADE_EVIDENCE_BIN"),
    ],
)
def test_legacy_variables_are_info_and_conflicts_warn_without_stderr(tmp_path, capsys, legacy, canonical):
    env = {**_evidence_env(tmp_path), legacy: "/opt/engine"}
    [(status, _name, detail, _scope)] = engine_paths.legacy_findings(tmp_path, env=env)
    assert status == "INFO" and legacy in detail and canonical in detail and "v0.33.0" in detail

    env[canonical] = "/opt/engine"
    assert engine_paths.legacy_findings(tmp_path, env=env)[0][0] == "INFO"

    env[canonical] = "/opt/other"
    [(status, _name, detail, _scope)] = engine_paths.legacy_findings(tmp_path, env=env)
    assert status == "WARN" and "refused" in detail
    assert capsys.readouterr().err == ""


def test_doctor_gathers_rename_findings(tmp_path, monkeypatch):
    canonical, legacy = engine_paths.code_graph_db_paths(tmp_path)
    _seed_index(canonical, 1_800_000_000)
    _seed_index(legacy, 1_700_000_000)
    path = config.config_path(tmp_path)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": ["claude"],
                "owner": "claude",
                "includes": [],
                config.LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY: 12,
            }
        )
    )
    monkeypatch.setattr("brigade.registry.all_stations", lambda: [])
    monkeypatch.setattr("brigade.component_report.doctor_checks", lambda: [])

    rows = {
        name: (status, detail) for status, name, detail, _scope in doctor._gather_checks(doctor.build_context(tmp_path))
    }

    assert rows["code graph index layout"][0] == doctor.WARN
    status, detail = rows["legacy config key"]
    assert status == doctor.INFO and config.CODE_GRAPH_DELTA_TIMEOUT_KEY in detail


# --------------------------------------------------------------------------- #
# installer gitignore and transient snapshot names
# --------------------------------------------------------------------------- #


def test_managed_gitignore_block_ignores_the_canonical_index():
    block = install.build_gitignore_block(Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[]))
    assert ".brigade/code-graph/" in block.splitlines()


def test_verify_snapshots_use_canonical_names():
    assert code_graph_delta.SNAPSHOT_NAME == "code-graph-before.db"
    assert code_graph_delta.SNAPSHOT_AFTER_NAME == "code-graph-after.db"


# --------------------------------------------------------------------------- #
# CLI aliases: accepted, hidden from --help, silent
# --------------------------------------------------------------------------- #


def _parse(argv):
    return cli._build_parser().parse_args(argv)


@pytest.mark.parametrize("flag", ["--code-graph-timeout", "--graphtrail-timeout"])
def test_code_graph_timeout_flag_and_legacy_alias(flag):
    args = _parse(["work", "verify", "run", "--target", ".", "--command", "true", flag, "7"])
    assert args.code_graph_timeout == 7.0


@pytest.mark.parametrize("flag", ["--from-evidence", "--from-miseledger"])
def test_from_evidence_flag_and_legacy_alias(flag):
    args = _parse(["work", "import", "context", flag, "query text"])
    assert args.from_evidence == "query text"


@pytest.mark.parametrize("name", ["evidence", LEGACY_EVIDENCE])
def test_receipts_export_evidence_and_legacy_alias_dispatch_alike(tmp_path, monkeypatch, capsys, name):
    seen = []
    monkeypatch.setattr("brigade.receipts_cmd.export_miseledger", lambda **kwargs: seen.append(kwargs) or 0)
    assert cli.main(["receipts", "export", name, "--target", str(tmp_path), "--new-only"]) == 0
    assert seen and seen[0]["new_only"] is True and seen[0]["target"] == tmp_path
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("argv", "canonical", "legacy"),
    [
        (["work", "verify", "run", "--help"], "--code-graph-timeout", "--graphtrail-timeout"),
        (["work", "import", "context", "--help"], "--from-evidence", "--from-miseledger"),
        (["receipts", "export", "--help"], "evidence", LEGACY_EVIDENCE),
    ],
)
def test_legacy_aliases_stay_out_of_help(capsys, argv, canonical, legacy):
    with pytest.raises(SystemExit):
        cli.main(argv)
    text = capsys.readouterr().out
    assert canonical in text
    assert legacy not in text


# --------------------------------------------------------------------------- #
# JSON outputs carry canonical and legacy keys with equal values
# --------------------------------------------------------------------------- #


def test_operator_checkup_ledger_dual_emits_evidence_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "brigade.evidence_cmd.status_payload",
        lambda target, include_doctor=False: {"installed": True, "health": "ok", "export_cursor_present": True},
    )
    monkeypatch.setattr("brigade.work_cmd.verification._verify_receipts", lambda target: [])
    lifecycle._checkup_ledger(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["evidence"] == payload[LEGACY_EVIDENCE]
    assert payload["evidence"]["health"] == "ok"


# --------------------------------------------------------------------------- #
# Cursor user installer: owned legacy keys kept, renamed only on request
# --------------------------------------------------------------------------- #


def _cursor_install(*extra):
    return cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json", *extra])


def _downgrade_to_legacy_keys(cursor):
    """Rewrite a fresh install the way an earlier Brigade wrote it: legacy keys, owned."""
    mcp_path = cursor / "mcp.json"
    state_path = cursor / "brigade" / "install-state.json"
    mcp = json.loads(mcp_path.read_text())
    state = json.loads(state_path.read_text())
    for canonical, legacy in (("brigade-code-graph", LEGACY_GRAPH), ("brigade-evidence", LEGACY_EVIDENCE)):
        mcp["mcpServers"][legacy] = mcp["mcpServers"].pop(canonical)
        state["mcp"][legacy] = state["mcp"].pop(canonical)
    mcp_path.write_text(json.dumps(mcp))
    state_path.write_text(json.dumps(state))
    return mcp_path


def test_cursor_keeps_owned_legacy_keys_and_prints_the_rename_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cursor_user_cmd, "_home_dir", lambda: tmp_path)
    assert _cursor_install() == 0
    capsys.readouterr()
    mcp_path = _downgrade_to_legacy_keys(tmp_path / ".cursor")

    assert _cursor_install() == 0
    payload = json.loads(capsys.readouterr().out)
    live = json.loads(mcp_path.read_text())["mcpServers"]
    assert {LEGACY_GRAPH, LEGACY_EVIDENCE} <= set(live)
    assert not {"brigade-code-graph", "brigade-evidence"} & set(live)
    assert any(f"mcp__{LEGACY_GRAPH}__* to mcp__brigade-code-graph__*" in note for note in payload["notices"])
    assert cli.main(["harness", "doctor", "cursor", "--scope", "user", "--json"]) == 0

    assert cli.main(["harness", "install", "cursor", "--scope", "user"]) == 0
    assert "note: kept Brigade-owned MCP server key" in capsys.readouterr().out


def test_cursor_rename_flag_renames_pristine_owned_keys_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cursor_user_cmd, "_home_dir", lambda: tmp_path)
    assert _cursor_install() == 0
    mcp_path = _downgrade_to_legacy_keys(tmp_path / ".cursor")
    mcp = json.loads(mcp_path.read_text())
    mcp["mcpServers"][LEGACY_EVIDENCE] = {"command": "user-edited"}
    mcp_path.write_text(json.dumps(mcp))
    capsys.readouterr()

    assert _cursor_install("--rename-legacy-servers") == 1
    payload = json.loads(capsys.readouterr().out)
    live = json.loads(mcp_path.read_text())["mcpServers"]
    assert "brigade-code-graph" in live and LEGACY_GRAPH not in live
    # The edited legacy entry stays where it is, and nothing shadows it: the
    # canonical key for that engine is not created alongside it.
    assert live[LEGACY_EVIDENCE] == {"command": "user-edited"}
    assert "brigade-evidence" not in live
    assert [item["name"] for item in payload["conflicts"]] == [LEGACY_EVIDENCE]
    assert payload["notices"] == []
    # The plugin's server list, the live config and ownership name the same keys.
    cursor = tmp_path / ".cursor"
    plugin_servers = json.loads((cursor / "brigade" / "mcp.json").read_text())["servers"]
    assert set(plugin_servers) == {"brigade", "brigade-code-graph", LEGACY_EVIDENCE}
    owned = json.loads((cursor / "brigade" / "install-state.json").read_text())["mcp"]
    assert {"brigade-code-graph", LEGACY_EVIDENCE} <= set(owned)
    assert not {LEGACY_GRAPH, "brigade-evidence"} & set(owned)


def test_cursor_never_renames_user_authored_legacy_entries(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cursor_user_cmd, "_home_dir", lambda: tmp_path)
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    authored = {"command": "my-own-graph"}
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {LEGACY_GRAPH: authored}}))

    assert _cursor_install("--rename-legacy-servers") == 0
    live = json.loads((cursor / "mcp.json").read_text())["mcpServers"]
    assert live[LEGACY_GRAPH] == authored
    assert "brigade-code-graph" in live


def test_cursor_rename_keeps_legacy_key_when_owned_canonical_entry_was_edited(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cursor_user_cmd, "_home_dir", lambda: tmp_path)
    assert _cursor_install() == 0
    cursor = tmp_path / ".cursor"
    mcp_path = _downgrade_to_legacy_keys(cursor)
    # Brigade also owns a canonical entry that the user has since edited, so the
    # rename cannot install its replacement.
    mcp = json.loads(mcp_path.read_text())
    state_path = cursor / "brigade" / "install-state.json"
    state = json.loads(state_path.read_text())
    original = {"command": "brigade-code-graph-mcp"}
    mcp["mcpServers"]["brigade-code-graph"] = {"command": "user-edited"}
    state["mcp"]["brigade-code-graph"] = cursor_user_cmd._digest_value(original)
    mcp_path.write_text(json.dumps(mcp))
    state_path.write_text(json.dumps(state))
    capsys.readouterr()

    assert _cursor_install("--rename-legacy-servers") == 1
    payload = json.loads(capsys.readouterr().out)
    live = json.loads(mcp_path.read_text())["mcpServers"]
    assert LEGACY_GRAPH in live
    assert live["brigade-code-graph"] == {"command": "user-edited"}
    assert any(item.get("name") == LEGACY_GRAPH for item in payload["conflicts"])
    assert LEGACY_GRAPH in json.loads(state_path.read_text())["mcp"]


# --------------------------------------------------------------------------- #
# brigade mcp sync: notice by default, opt-in rename of the catalog key
# --------------------------------------------------------------------------- #


def _catalog_with_legacy_graph(target):
    assert mcp_cmd.init(target=target, json_output=True) == 0
    assert mcp_cmd.add(target=target, name=LEGACY_GRAPH, command="graphtrail-mcp", json_output=True) == 0


def test_mcp_sync_keeps_legacy_key_and_notes_the_rename(tmp_path, capsys):
    _catalog_with_legacy_graph(tmp_path)
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert LEGACY_GRAPH in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    assert any(f"mcp__{LEGACY_GRAPH}__* to mcp__brigade-code-graph__*" in note for note in payload["notes"])


def test_mcp_sync_rename_flag_moves_catalog_key_and_owned_projection(tmp_path, capsys):
    _catalog_with_legacy_graph(tmp_path)
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0
    native = tmp_path / ".mcp.json"
    doc = json.loads(native.read_text())
    doc["mcpServers"][LEGACY_EVIDENCE] = {"command": "user-authored"}
    native.write_text(json.dumps(doc))
    capsys.readouterr()

    assert (
        mcp_cmd.sync(target=tmp_path, harness="claude", write=False, rename_legacy_servers=True, json_output=True) == 0
    )
    capsys.readouterr()
    assert LEGACY_GRAPH in mcp_cmd.load_canonical(tmp_path)[0]

    rc = mcp_cmd.sync(target=tmp_path, harness="claude", write=True, rename_legacy_servers=True, json_output=True)
    assert rc == 0
    servers, _, _ = mcp_cmd.load_canonical(tmp_path)
    assert set(servers) == {"brigade-code-graph"}
    assert servers["brigade-code-graph"].command == "graphtrail-mcp"
    live = json.loads(native.read_text())["mcpServers"]
    assert "brigade-code-graph" in live and LEGACY_GRAPH not in live
    assert live[LEGACY_EVIDENCE] == {"command": "user-authored"}


def test_mcp_sync_rename_flag_refuses_name_filter(tmp_path, capsys):
    _catalog_with_legacy_graph(tmp_path)
    capsys.readouterr()
    rc = mcp_cmd.sync(target=tmp_path, name=LEGACY_GRAPH, write=True, rename_legacy_servers=True, json_output=True)
    assert rc == 2
    assert "--rename-legacy-servers cannot be combined with --name" in capsys.readouterr().out


def test_mcp_sync_rename_keeps_working_legacy_entry_when_canonical_key_is_foreign(tmp_path, capsys):
    _catalog_with_legacy_graph(tmp_path)
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0
    native = tmp_path / ".mcp.json"
    doc = json.loads(native.read_text())
    foreign = {"command": "someone-elses-graph"}
    doc["mcpServers"]["brigade-code-graph"] = foreign
    native.write_text(json.dumps(doc))
    before = native.read_text()
    capsys.readouterr()

    rc = mcp_cmd.sync(target=tmp_path, harness="claude", write=True, rename_legacy_servers=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    # The legacy integration keeps working, and the catalog key does not move.
    assert native.read_text() == before
    assert set(mcp_cmd.load_canonical(tmp_path)[0]) == {LEGACY_GRAPH}
    conflict = [i for i in payload["items"] if i["server"] == "brigade-code-graph"]
    assert [i["action"] for i in conflict] == ["conflict"]
    assert any(f"kept server '{LEGACY_GRAPH}'" in note and "claude" in note for note in payload["notes"])


def test_mcp_sync_rename_one_harness_at_a_time_ends_with_canonical_only(tmp_path, capsys):
    _catalog_with_legacy_graph(tmp_path)
    for harness_name in ("claude", "cursor"):
        assert mcp_cmd.sync(target=tmp_path, harness=harness_name, write=True, json_output=True) == 0
    claude, cursor = tmp_path / ".mcp.json", tmp_path / ".cursor" / "mcp.json"
    capsys.readouterr()

    assert (
        mcp_cmd.sync(target=tmp_path, harness="claude", write=True, rename_legacy_servers=True, json_output=True) == 0
    )
    capsys.readouterr()
    assert set(mcp_cmd.load_canonical(tmp_path)[0]) == {"brigade-code-graph"}
    assert set(json.loads(claude.read_text())["mcpServers"]) == {"brigade-code-graph"}

    # A plain sync points at the leftover instead of silently leaving it.
    assert mcp_cmd.sync(target=tmp_path, harness="cursor", write=False, json_output=True) == 0
    notes = json.loads(capsys.readouterr().out)["notes"]
    assert any(f"cursor still has Brigade's '{LEGACY_GRAPH}' entry" in note for note in notes)

    assert (
        mcp_cmd.sync(target=tmp_path, harness="cursor", write=True, rename_legacy_servers=True, json_output=True) == 0
    )
    capsys.readouterr()
    assert set(json.loads(cursor.read_text())["mcpServers"]) == {"brigade-code-graph"}
    assert set(json.loads(claude.read_text())["mcpServers"]) == {"brigade-code-graph"}
    ownership = json.loads(mcp_cmd.state_path(tmp_path).read_text())["ownership"]
    assert LEGACY_GRAPH not in ownership["cursor"][".cursor/mcp.json"]


def test_mcp_sync_rename_leftover_stays_when_its_harness_has_a_foreign_canonical_key(tmp_path, capsys):
    _catalog_with_legacy_graph(tmp_path)
    for harness_name in ("claude", "cursor"):
        assert mcp_cmd.sync(target=tmp_path, harness=harness_name, write=True, json_output=True) == 0
    assert (
        mcp_cmd.sync(target=tmp_path, harness="claude", write=True, rename_legacy_servers=True, json_output=True) == 0
    )
    cursor = tmp_path / ".cursor" / "mcp.json"
    doc = json.loads(cursor.read_text())
    doc["mcpServers"]["brigade-code-graph"] = {"command": "someone-elses-graph"}
    cursor.write_text(json.dumps(doc))
    before = cursor.read_text()
    capsys.readouterr()

    rc = mcp_cmd.sync(target=tmp_path, harness="cursor", write=True, rename_legacy_servers=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert cursor.read_text() == before
    assert any(f"kept the cursor entry '{LEGACY_GRAPH}'" in note for note in payload["notes"])


def test_mcp_sync_notes_a_catalog_with_both_keys_and_rename_exits_1(tmp_path, capsys):
    _catalog_with_legacy_graph(tmp_path)
    assert (
        mcp_cmd.add(target=tmp_path, name="brigade-code-graph", command="brigade-code-graph-mcp", json_output=True) == 0
    )
    capsys.readouterr()

    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=False, json_output=True) == 0
    notes = json.loads(capsys.readouterr().out)["notes"]
    assert any(f"both '{LEGACY_GRAPH}' and its canonical key" in note for note in notes)

    rc = mcp_cmd.sync(target=tmp_path, harness="claude", write=True, rename_legacy_servers=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["rename_collisions"] == [LEGACY_GRAPH]
    assert set(mcp_cmd.load_canonical(tmp_path)[0]) == {LEGACY_GRAPH, "brigade-code-graph"}


_GRAPH_URL = "https://graph.example.test/mcp"


def _adopted_remote_graph(target, harness, native_text):
    """Seed a remote legacy graph entry with native settings, then adopt it."""
    assert mcp_cmd.init(target=target, json_output=True) == 0
    adapter = mcp_cmd.ADAPTERS[harness]
    path = target / adapter.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(native_text)
    server, _ = mcp_adapters.server_from_dict(LEGACY_GRAPH, {"transport": "http", "url": _GRAPH_URL})
    mcp_cmd._write_canonical(target, {LEGACY_GRAPH: server})
    assert mcp_cmd.sync(target=target, harness=harness, adopt=True, write=True, json_output=True) == 0
    return adapter, path


def test_mcp_sync_rename_keeps_adopted_codex_native_auth(tmp_path, capsys):
    adapter, path = _adopted_remote_graph(
        tmp_path,
        "codex",
        f'[mcp_servers.{LEGACY_GRAPH}]\nurl = "{_GRAPH_URL}"\nbearer_token_env_var = "FAKE_TOKEN"\n',
    )
    assert adapter.read_file(path.read_text())[LEGACY_GRAPH]["bearer_token_env_var"] == "FAKE_TOKEN"
    capsys.readouterr()

    rc = mcp_cmd.sync(target=tmp_path, harness="codex", write=True, rename_legacy_servers=True, json_output=True)
    assert rc == 0
    live = adapter.read_file(path.read_text())
    assert set(live) == {"brigade-code-graph"}
    assert live["brigade-code-graph"]["url"] == _GRAPH_URL
    assert live["brigade-code-graph"]["bearer_token_env_var"] == "FAKE_TOKEN"
    record = json.loads(mcp_cmd.state_path(tmp_path).read_text())["ownership"]["codex"][adapter.path]
    assert set(record) == {"brigade-code-graph"}
    assert record["brigade-code-graph"]["native_auth_keys"] == {"bearer_token_env_var": True}
    assert "FAKE_TOKEN" not in mcp_cmd.state_path(tmp_path).read_text()

    # The renamed entry is owned and current: a later plain sync keeps the auth.
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["files_written"] == []
    assert adapter.read_file(path.read_text())["brigade-code-graph"]["bearer_token_env_var"] == "FAKE_TOKEN"


def test_mcp_sync_rename_keeps_adopted_opencode_native_headers(tmp_path, capsys):
    native = {
        "mcp": {
            LEGACY_GRAPH: {
                "type": "remote",
                "url": _GRAPH_URL,
                "headers": {"Authorization": "Bearer {env:FAKE_TOKEN}"},
            }
        }
    }
    adapter, path = _adopted_remote_graph(tmp_path, "opencode", json.dumps(native))
    capsys.readouterr()

    rc = mcp_cmd.sync(target=tmp_path, harness="opencode", write=True, rename_legacy_servers=True, json_output=True)
    assert rc == 0
    live = adapter.read_file(path.read_text())
    assert set(live) == {"brigade-code-graph"}
    assert live["brigade-code-graph"]["headers"] == {"Authorization": "Bearer {env:FAKE_TOKEN}"}


# --------------------------------------------------------------------------- #
# legacy config key: the file load_config reads, and the ignored case
# --------------------------------------------------------------------------- #


def _write_workspace_config(path, **keys):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "depth": "repo", "harnesses": ["claude"], "owner": "claude", "includes": [], **keys}
    path.write_text(json.dumps(payload))


def test_legacy_config_key_found_in_the_legacy_workspace_config(tmp_path):
    legacy_dir = config.LEGACY_WORKSPACE_DIRNAMES[0]
    _write_workspace_config(tmp_path / legacy_dir / "config.json", **{config.LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY: 7})
    assert not config.config_path(tmp_path).exists()
    loaded = config.load_config(tmp_path)
    assert loaded is not None and loaded.code_graph_delta_timeout_seconds == 7.0
    assert config.legacy_config_keys(tmp_path) == [
        (config.LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY, config.CODE_GRAPH_DELTA_TIMEOUT_KEY, False)
    ]


@pytest.mark.parametrize("with_canonical", [False, True])
def test_doctor_legacy_config_key_message_matches_what_load_config_reads(tmp_path, monkeypatch, with_canonical):
    keys = {config.LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY: 12}
    if with_canonical:
        keys[config.CODE_GRAPH_DELTA_TIMEOUT_KEY] = 30
    _write_workspace_config(config.config_path(tmp_path), **keys)
    monkeypatch.setattr("brigade.registry.all_stations", lambda: [])
    monkeypatch.setattr("brigade.component_report.doctor_checks", lambda: [])

    rows = {
        name: (status, detail) for status, name, detail, _scope in doctor._gather_checks(doctor.build_context(tmp_path))
    }
    status, detail = rows["legacy config key"]
    assert status == doctor.INFO
    if with_canonical:
        assert "is ignored because" in detail and "until v0.33.0" not in detail
    else:
        assert "is read while" in detail and "is absent" in detail


# --------------------------------------------------------------------------- #
# hints name the executable that resolved
# --------------------------------------------------------------------------- #


def test_evidence_doctor_timeout_hint_names_the_resolved_executable(monkeypatch):
    from pathlib import Path

    from brigade import managed
    from brigade.station import DoctorContext

    tool = managed.resolve("brigade-evidence")
    monkeypatch.setattr(managed.component_bins, "resolve", lambda name, **kw: "/x/brigade-evidence")
    monkeypatch.setattr(managed.proc, "run", lambda args, **kw: managed.proc.Result(code=124, stdout="", stderr=""))
    results = tool.doctor(DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[]))
    assert any("run `brigade-evidence doctor` manually" in detail for _status, _name, detail in results)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    set_home(monkeypatch, str(tmp_path))
