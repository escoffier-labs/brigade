"""Failing-first tests for issue #362 code/evidence engine facades.

These tests define the executable subprocess contract for `brigade code`,
`brigade search` compatibility aliases, and `brigade evidence crawl|search`.
They should fail until the facade implementation lands.
"""

from __future__ import annotations

import importlib
import json
import stat

import pytest

from brigade import (
    cli,
    code_references,
    completions,
    component_manifest,
    context_cmd,
    evidence_brief,
    evidence_cmd,
    proc,
    roadmap_cmd,
    search_cmd,
)

REFERENCE = {
    "schema": "brigade.code-reference.v1",
    "repository": "escoffier-labs/brigade",
    "revision": {"commit": "a" * 40},
    "file_path": "src/brigade/receipts_cmd.py",
    "qualified_name": "brigade.receipts_cmd._metadata_with_delta",
    "symbol_kind": "function",
    "source_span": {"start_line": 787, "line_count": 3},
    "change_kind": "changed",
}

ISSUE_362_CODE_COMMANDS = (
    "brigade code sync",
    "brigade code context",
    "brigade code impact",
)

ISSUE_362_SEARCH_ALIASES = (
    "brigade search sync",
    "brigade search context",
    "brigade search impact",
)

ISSUE_362_EVIDENCE_COMMANDS = (
    "brigade evidence crawl",
    "brigade evidence search",
)


def _patch_graphtrail(monkeypatch, *, run_calls: list | None = None):
    def fake_run(args, timeout=30.0, **kwargs):
        if run_calls is not None:
            run_calls.append({"args": list(args), "timeout": timeout, "kwargs": kwargs})
        exit_code = 0
        if args and args[-1].isdigit() and len(args) >= 2 and args[-2] == "--test-exit":
            exit_code = int(args[-1])
        stdout = '{"ok": true}\n' if "--json" in args else "graphtrail-stdout\n"
        return proc.Result(exit_code, stdout, "")

    monkeypatch.setattr(context_cmd, "_graphtrail_bin", lambda: "/fake/graphtrail")
    monkeypatch.setattr(proc, "run", fake_run)


def _patch_miseledger(monkeypatch, *, run_calls: list | None = None):
    def fake_run(args, timeout=30.0, **kwargs):
        if run_calls is not None:
            run_calls.append({"args": list(args), "timeout": timeout, "kwargs": kwargs})
        exit_code = 0
        if args and args[-1].isdigit() and len(args) >= 2 and args[-2] == "--test-exit":
            exit_code = int(args[-1])
        stdout = '{"matches": []}\n' if "--json" in args else "miseledger-stdout\n"
        return proc.Result(exit_code, stdout, "")

    monkeypatch.setattr(evidence_brief, "_miseledger_bin", lambda: "/fake/miseledger")
    monkeypatch.setattr(proc, "run", fake_run)


# --- parser / discovery -----------------------------------------------------


def test_parser_registers_brigade_code_group():
    with pytest.raises(SystemExit) as exc:
        cli.main(["code", "--help"])
    assert exc.value.code == 0


def test_parser_keeps_search_group_and_sync_plan_subcommand():
    with pytest.raises(SystemExit) as exc:
        cli.main(["search", "sync", "plan", "--help"])
    assert exc.value.code == 0


def test_command_tree_exposes_code_and_executable_search_aliases():
    tree = completions._command_tree()
    assert "code" in tree["brigade"]
    assert set(tree["brigade code"]) >= {"sync", "context", "impact"}
    assert set(tree["brigade search"]) >= {"status", "doctor", "sync", "context", "impact"}
    assert "plan" in tree["brigade search sync"]


def test_command_tree_exposes_executable_evidence_crawl_and_search():
    tree = completions._command_tree()
    assert set(tree["brigade evidence"]) >= {"status", "doctor", "crawl", "search", "export"}
    assert "plan" in tree["brigade evidence crawl"]
    assert "plan" in tree["brigade evidence export"]


@pytest.mark.parametrize("shell", ("bash", "zsh", "fish"))
def test_completion_scripts_advertise_issue_362_commands(shell):
    renderer = {"bash": completions.bash_script, "zsh": completions.zsh_script, "fish": completions.fish_script}[shell]
    script = renderer()
    for fragment in ("code", "context", "impact"):
        assert fragment in script
    assert "evidence" in script
    assert "crawl" in script
    assert "search" in script


def test_roadmap_command_contract_includes_issue_362_paths(tmp_path):
    payload = roadmap_cmd.command_contract_payload(tmp_path)
    commands = set(payload["cli_commands"])
    for command in (*ISSUE_362_CODE_COMMANDS, *ISSUE_362_SEARCH_ALIASES, *ISSUE_362_EVIDENCE_COMMANDS):
        assert command in commands
    inventory = payload["expected_inventory"]
    assert "- `brigade code sync`" in inventory
    assert "- `brigade evidence search`" in inventory


def test_search_alias_compatibility_window_is_documented():
    code_cmd = importlib.import_module("brigade.code_cmd")
    minor_releases = getattr(code_cmd, "SEARCH_ALIAS_RETENTION_MINOR_RELEASES", 0)
    days = getattr(code_cmd, "SEARCH_ALIAS_RETENTION_DAYS", 0)
    assert minor_releases >= 2
    assert days >= 90


# --- brigade code: GraphTrail forwarding ------------------------------------


def test_code_sync_forwards_exact_argv_and_stdout(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)
    space = tmp_path / "my repo"
    space.mkdir()

    rc = cli.main(["code", "sync", str(space)])

    assert rc == 0
    assert calls == [{"args": ["/fake/graphtrail", "sync", str(space)], "timeout": 900.0, "kwargs": {}}]
    assert capsys.readouterr().out == "graphtrail-stdout\n"


def test_code_sync_uses_configured_timeout(monkeypatch, tmp_path):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_CODE_SYNC_TIMEOUT_SECONDS", "42.5")

    assert cli.main(["code", "sync", str(tmp_path)]) == 0
    assert calls[0]["timeout"] == 42.5


@pytest.mark.parametrize("value", ("", "zero", "0", "-1", "nan", "inf"))
def test_code_sync_rejects_invalid_configured_timeout_before_starting_engine(monkeypatch, tmp_path, capsys, value):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_CODE_SYNC_TIMEOUT_SECONDS", value)

    assert cli.main(["code", "sync", str(tmp_path)]) == 2
    assert calls == []
    assert "BRIGADE_CODE_SYNC_TIMEOUT_SECONDS" in capsys.readouterr().err


def test_code_context_forwards_remaining_args(monkeypatch, capsys):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["code", "context", "wire facades", "--json", "--limit", "5"])

    assert rc == 0
    assert calls[0]["args"] == ["/fake/graphtrail", "context", "wire facades", "--json", "--limit", "5"]
    assert calls[0]["timeout"] == 30.0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.parametrize(
    ("argv", "expected_engine_argv"),
    [
        (["code", "context", "--json", "--limit", "5"], ["context", "--json", "--limit", "5"]),
        (["search", "context", "--json", "--limit", "5"], ["context", "--json", "--limit", "5"]),
    ],
)
def test_graphtrail_passthrough_leaves_accept_leading_flags(monkeypatch, argv, expected_engine_argv):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(argv)

    assert rc == 0
    assert calls[0]["args"] == ["/fake/graphtrail", *expected_engine_argv]


def test_code_impact_propagates_child_exit_code(monkeypatch):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["code", "impact", "brigade.cli.main", "--test-exit", "3"])

    assert rc == 3
    assert calls[0]["args"][:3] == ["/fake/graphtrail", "impact", "brigade.cli.main"]


def test_code_sync_missing_graphtrail_exits_127_with_diagnostic(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(context_cmd, "_graphtrail_bin", lambda: None)

    rc = cli.main(["code", "sync", str(tmp_path)])

    assert rc == 127
    err = capsys.readouterr().err
    assert "graphtrail" in err.lower()
    assert "add" in err.lower() or "install" in err.lower()


def test_code_sync_uses_graphtrail_bin_override(monkeypatch, tmp_path):
    override = tmp_path / "graphtrail"
    override.write_text("#!/bin/sh\n")
    override.chmod(override.stat().st_mode | stat.S_IXUSR)
    calls: list[list[str]] = []

    monkeypatch.setenv("GRAPHTRAIL_BIN", str(override))
    monkeypatch.setattr(proc, "which", lambda _: pytest.fail("resolver should use GRAPHTRAIL_BIN first"))
    monkeypatch.setattr(proc, "run", lambda args, **_: calls.append(args) or proc.Result(0, "", ""))

    assert cli.main(["code", "sync", str(tmp_path)]) == 0
    assert calls == [[str(override), "sync", str(tmp_path)]]


def test_code_sync_timeout_returns_124(monkeypatch, tmp_path):
    _patch_graphtrail(monkeypatch)

    def timeout_run(args, timeout=30.0, **kwargs):
        return proc.Result(124, "", "timeout after 0.1s\n")

    monkeypatch.setattr(proc, "run", timeout_run)

    rc = cli.main(["code", "sync", str(tmp_path)])

    assert rc == 124


# --- brigade search: tested compatibility aliases ---------------------------


@pytest.mark.parametrize(
    ("alias_argv", "expected_engine_argv"),
    [
        (["search", "sync", "{path}"], ["sync", "{path}"]),
        (["search", "context", "task", "--json"], ["context", "task", "--json"]),
        (["search", "impact", "brigade.cli.main", "--depth", "2"], ["impact", "brigade.cli.main", "--depth", "2"]),
    ],
)
def test_search_executable_verbs_alias_code_facades(monkeypatch, tmp_path, alias_argv, expected_engine_argv):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)
    path = tmp_path / "space path"
    path.mkdir()
    argv = [part.format(path=str(path)) for part in alias_argv]

    rc = cli.main(argv)

    assert rc == 0
    assert calls[0]["args"] == ["/fake/graphtrail", *[part.format(path=str(path)) for part in expected_engine_argv]]


def test_search_sync_plan_stays_plan_only_and_preserves_json_contract(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    _patch_graphtrail(monkeypatch, run_calls=calls)

    rc = cli.main(["search", "sync", "plan", "--target", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert calls == []
    assert payload["station"] == "search"
    assert payload["kind"] == "sync"
    assert ["graphtrail", "sync", str(tmp_path.resolve())] in payload["commands"]


# --- brigade evidence: MiseLedger forwarding --------------------------------


def test_evidence_crawl_forwards_exact_argv_with_space_path(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    _patch_miseledger(monkeypatch, run_calls=calls)
    space = tmp_path / "evidence root"
    space.mkdir()

    rc = cli.main(["evidence", "crawl", "files", str(space)])

    assert rc == 0
    assert calls == [{"args": ["/fake/miseledger", "crawl", "files", str(space)], "timeout": 900.0, "kwargs": {}}]
    assert capsys.readouterr().out == "miseledger-stdout\n"


def test_evidence_crawl_uses_configured_timeout(monkeypatch):
    calls: list[dict] = []
    _patch_miseledger(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_EVIDENCE_CRAWL_TIMEOUT_SECONDS", "42.5")

    assert cli.main(["evidence", "crawl", "sessions"]) == 0
    assert calls[0]["timeout"] == 42.5


@pytest.mark.parametrize("value", ("", "zero", "0", "-1", "nan", "inf"))
def test_evidence_crawl_rejects_invalid_configured_timeout_before_starting_engine(monkeypatch, capsys, value):
    calls: list[dict] = []
    _patch_miseledger(monkeypatch, run_calls=calls)
    monkeypatch.setenv("BRIGADE_EVIDENCE_CRAWL_TIMEOUT_SECONDS", value)

    assert cli.main(["evidence", "crawl", "sessions"]) == 2
    assert calls == []
    assert "BRIGADE_EVIDENCE_CRAWL_TIMEOUT_SECONDS" in capsys.readouterr().err


def test_evidence_search_forwards_code_reference_before_lexical_fallback(monkeypatch, capsys):
    calls: list[dict] = []
    _patch_miseledger(monkeypatch, run_calls=calls)
    ref = code_references.canonical_json(REFERENCE)
    code_references.validate(REFERENCE)

    rc = cli.main(["evidence", "search", "needle", "--code-reference", ref, "--json"])

    assert rc == 0
    assert calls[0]["args"] == ["/fake/miseledger", "search", "needle", "--code-reference", ref, "--json"]
    assert calls[0]["timeout"] == 30.0
    assert json.loads(capsys.readouterr().out) == {"matches": []}


def test_evidence_search_accepts_a_leading_code_reference(monkeypatch):
    calls: list[dict] = []
    _patch_miseledger(monkeypatch, run_calls=calls)
    ref = code_references.canonical_json(REFERENCE)

    rc = cli.main(["evidence", "search", "--code-reference", ref])

    assert rc == 0
    assert calls[0]["args"] == ["/fake/miseledger", "search", "--code-reference", ref]


def test_evidence_search_propagates_child_exit_code(monkeypatch):
    calls: list[dict] = []
    _patch_miseledger(monkeypatch, run_calls=calls)

    rc = cli.main(["evidence", "search", "needle", "--test-exit", "5"])

    assert rc == 5
    assert calls[0]["args"][:3] == ["/fake/miseledger", "search", "needle"]


def test_evidence_crawl_missing_miseledger_exits_127_with_diagnostic(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(evidence_brief, "_miseledger_bin", lambda: None)

    rc = cli.main(["evidence", "crawl", "sessions"])

    assert rc == 127
    err = capsys.readouterr().err
    assert "miseledger" in err.lower()


def test_evidence_crawl_uses_miseledger_bin_override(monkeypatch, tmp_path):
    override = tmp_path / "miseledger"
    override.write_text("#!/bin/sh\n")
    override.chmod(override.stat().st_mode | stat.S_IXUSR)
    calls: list[list[str]] = []

    monkeypatch.setenv("MISELEDGER_BIN", str(override))
    monkeypatch.setattr(proc, "which", lambda _: pytest.fail("facade should use _miseledger_bin"))
    monkeypatch.setattr(proc, "run", lambda args, **_: calls.append(args) or proc.Result(0, "", ""))

    assert cli.main(["evidence", "crawl", "sessions"]) == 0
    assert calls == [[str(override), "crawl", "sessions"]]


def test_evidence_search_timeout_returns_124(monkeypatch):
    _patch_miseledger(monkeypatch)

    def timeout_run(args, timeout=30.0, **kwargs):
        return proc.Result(124, "", "timeout after 0.1s\n")

    monkeypatch.setattr(proc, "run", timeout_run)

    rc = cli.main(["evidence", "search", "needle"])

    assert rc == 124


def test_evidence_crawl_timeout_returns_124(monkeypatch):
    _patch_miseledger(monkeypatch)

    def timeout_run(args, timeout=30.0, **kwargs):
        return proc.Result(124, "", "timeout after 0.1s\n")

    monkeypatch.setattr(proc, "run", timeout_run)

    assert cli.main(["evidence", "crawl", "sessions"]) == 124


def test_evidence_crawl_plan_stays_plan_only_and_preserves_json_contract(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []
    _patch_miseledger(monkeypatch, run_calls=calls)

    rc = cli.main(["evidence", "crawl", "plan", "--target", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert calls == []
    assert payload["kind"] == "crawl"
    assert ["miseledger", "crawl", "sessions"] in payload["commands"]


# --- legacy health/plan JSON contracts (must keep passing) ------------------


def test_search_status_json_contract_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(search_cmd.proc, "which", lambda cmd: None)
    payload = search_cmd.status_payload(tmp_path)
    assert {
        "target",
        "station",
        "summary",
        "health",
        "installed",
        "next_commands",
        "docs",
        "boundaries",
        "tools",
        "pipeline",
    } <= set(payload.keys())
    assert payload["station"] == "search"


def test_search_doctor_json_contract_unchanged(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(search_cmd.proc, "which", lambda cmd: None)
    rc = search_cmd.doctor(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["station"] == "search"
    assert "tools" in payload


def test_evidence_status_json_contract_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)
    payload = evidence_cmd.status_payload(tmp_path)
    assert {
        "target",
        "installed",
        "health",
        "summary",
        "status",
        "doctor",
        "export_cursor_present",
        "next_commands",
        "docs",
        "boundaries",
        "pipeline",
    } <= set(payload.keys())


def test_evidence_export_plan_json_contract_unchanged(tmp_path):
    payload = evidence_cmd.export_plan_payload(target=tmp_path)
    assert payload["kind"] == "export"
    assert any("receipts export miseledger" in " ".join(cmd) for cmd in payload["commands"])


def test_component_shim_ids_remain_required():
    assert set(component_manifest.KNOWN_COMPONENT_IDS) >= {
        "graphtrail",
        "graphtrail-mcp",
        "miseledger",
        "sessionfind",
    }


def test_proc_run_uses_argv_lists_not_shell(monkeypatch, tmp_path):
    """Facade implementations must call proc.run with argv lists (no shell=True)."""
    observed: list[dict] = []

    def fake_run(args, timeout=30.0, **kwargs):
        observed.append({"args": args, "shell": kwargs.get("shell")})
        return proc.Result(0, "", "")

    monkeypatch.setattr(context_cmd, "_graphtrail_bin", lambda: "/fake/graphtrail")
    monkeypatch.setattr(proc, "run", fake_run)

    assert cli.main(["code", "sync", str(tmp_path / "space dir")]) == 0

    assert observed
    assert observed[0]["shell"] is not True
    assert isinstance(observed[0]["args"], list)
