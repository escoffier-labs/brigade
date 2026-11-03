"""Brigade readers of the code graph index follow the engine's resolution rule.

A repository first indexed by the renamed engine has only the canonical index.
Each reader must see it, keep using a legacy-only index in place, and prefer
the canonical index when both exist (docs/phase-engine-rename.md, "Resolution
rule"). One representative reader per module group.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from brigade import code_export, component_bins, context_cmd, engine_paths, graphtrail_delta, proc, search_cmd
from brigade.aboyeur import briefs
from brigade.claude_hooks import dependencies
from brigade.operator_cmd import lifecycle
from brigade.work_cmd import footprint

LAYOUTS = ("canonical", "legacy", "both")


@pytest.fixture(autouse=True)
def _no_index_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine_paths.CODE_GRAPH_DB_ENV, raising=False)
    monkeypatch.delenv(engine_paths.LEGACY_CODE_GRAPH_DB_ENV, raising=False)


def _seed(target: Path, layout: str) -> Path:
    """Create the index files for ``layout`` and return the one a reader must use."""
    canonical, legacy = engine_paths.code_graph_db_paths(target)
    present = {"canonical": [canonical], "legacy": [legacy], "both": [canonical, legacy]}[layout]
    for path in present:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    return legacy if layout == "legacy" else canonical


def _fake_engine(monkeypatch: pytest.MonkeyPatch, binary: str = "/x/engine") -> None:
    monkeypatch.setattr(component_bins, "resolve", lambda name, **kw: binary)


def _db_arg(argv: list[str]) -> str:
    return argv[argv.index("--db") + 1]


def _capture_runs(monkeypatch: pytest.MonkeyPatch, stdout: str = "graph output\n") -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any) -> proc.Result:
        calls.append(list(args))
        return proc.Result(code=0, stdout=stdout, stderr="")

    monkeypatch.setattr(proc, "run", fake_run)
    return calls


@pytest.mark.parametrize("layout", LAYOUTS)
def test_aboyeur_brief_reads_the_resolved_index(tmp_path, monkeypatch, layout):
    expected = _seed(tmp_path, layout)
    _fake_engine(monkeypatch)
    calls = _capture_runs(monkeypatch)

    brief = briefs.code_graph_brief(tmp_path, "fix dispatch")

    assert brief.attached is True
    assert calls and {_db_arg(argv) for argv in calls} == {str(expected)}


@pytest.mark.parametrize("layout", LAYOUTS)
def test_context_pack_reads_the_resolved_index(tmp_path, monkeypatch, layout):
    expected = _seed(tmp_path, layout)
    _fake_engine(monkeypatch)
    calls = _capture_runs(monkeypatch, stdout=json.dumps({"schema_version": 1, "entry_points": []}))

    summary = context_cmd._code_graph_summary(tmp_path, {"id": "t1", "text": "lint"})

    assert summary is not None
    assert [_db_arg(argv) for argv in calls] == [str(expected)]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_work_footprint_reads_the_resolved_index(tmp_path, monkeypatch, layout):
    expected = _seed(tmp_path, layout)
    _fake_engine(monkeypatch)
    seen: list[Path] = []

    def runner(target: Path, binary: str, db_path: Path, query: str) -> dict[str, Any]:
        seen.append(db_path)
        return {}

    predicted = footprint.predict_footprint(tmp_path, symbols=["dispatch"], impact_runner=runner)

    assert predicted.get("degraded") is not True
    assert seen == [expected]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_code_export_reads_the_resolved_index(tmp_path, monkeypatch, layout):
    expected = _seed(tmp_path, layout)
    _fake_engine(monkeypatch)
    seen: list[Path] = []

    def fake_run_json(binary: str, db_path: Path, target: Path, verb: str, *extra: str) -> None:
        seen.append(db_path)
        return None

    monkeypatch.setattr(code_export, "_run_json", fake_run_json)

    payload = code_export.export_payload(tmp_path)

    # The stub returns no stats, so the export stops right after the index check.
    assert payload["error"] == "code graph stats unavailable"
    assert seen == [expected]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_status_readers_report_the_resolved_index(tmp_path, monkeypatch, layout):
    expected = _seed(tmp_path, layout)
    _fake_engine(monkeypatch)
    monkeypatch.setattr(search_cmd.proc, "which", lambda cmd: None)
    _capture_runs(monkeypatch, stdout=json.dumps({"ok": True}))

    tool = next(iter(search_cmd.status_payload(tmp_path)["tools"].values()))
    graph = lifecycle._loop_stations_payload(tmp_path)["graph"]

    assert tool["db_present"] is True
    assert tool["db_path"] == str(expected)
    assert graph["status"] == "ok"
    assert str(expected) in graph["detail"]


def test_readers_see_no_index_on_an_unindexed_repo(tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    calls = _capture_runs(monkeypatch)

    assert briefs.code_graph_brief(tmp_path, "fix dispatch").attached is False
    assert context_cmd._code_graph_summary(tmp_path, {"text": "lint"}) is None
    assert calls == []
    graph = lifecycle._loop_stations_payload(tmp_path)["graph"]
    assert graph["status"] == "missing-db"
    assert str(engine_paths.code_graph_db_paths(tmp_path)[0]) in graph["detail"]


def test_readers_honor_the_explicit_variable(tmp_path, monkeypatch):
    _seed(tmp_path, "both")
    explicit = tmp_path / "elsewhere" / "graph.db"
    explicit.parent.mkdir()
    explicit.write_text("")
    monkeypatch.setenv(engine_paths.CODE_GRAPH_DB_ENV, str(explicit))
    _fake_engine(monkeypatch)
    calls = _capture_runs(monkeypatch)

    assert briefs.code_graph_brief(tmp_path, "fix dispatch").attached is True
    assert {_db_arg(argv) for argv in calls} == {str(explicit)}


def test_readers_refuse_conflicting_variables_without_raising(tmp_path, monkeypatch):
    _seed(tmp_path, "both")
    monkeypatch.setenv(engine_paths.CODE_GRAPH_DB_ENV, str(tmp_path / "a.db"))
    monkeypatch.setenv(engine_paths.LEGACY_CODE_GRAPH_DB_ENV, str(tmp_path / "b.db"))
    _fake_engine(monkeypatch)
    monkeypatch.setattr(search_cmd.proc, "which", lambda cmd: None)
    calls = _capture_runs(monkeypatch, stdout="{}")

    assert briefs.code_graph_brief(tmp_path, "fix dispatch").attached is False
    assert context_cmd._code_graph_summary(tmp_path, {"text": "lint"}) is None
    predicted = footprint.predict_footprint(tmp_path, symbols=["dispatch"], impact_runner=lambda *a: {})
    assert "conflicting code graph database overrides" in str(predicted)
    assert "conflicting" in code_export.export_payload(tmp_path)["error"]
    status = search_cmd.status_payload(tmp_path)
    assert next(iter(status["tools"].values()))["db_present"] is False
    assert "conflicting" in status["summary"]
    assert "conflicting" in lifecycle._loop_stations_payload(tmp_path)["graph"]["detail"]
    # Only the doctor probes in the status payload may reach the engine; no reader opens an index.
    assert all("--db" not in argv for argv in calls)


def _recording_engine(tmp_path: Path) -> tuple[Path, Path]:
    """An engine stub that records argv and cwd, then fails, so callers stop after one run."""
    log = tmp_path / "engine-calls.jsonl"
    script = tmp_path / "bin" / "engine"
    script.parent.mkdir()
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    return script, log


@pytest.mark.skipif(os.name == "nt", reason="shebang stub engine")
@pytest.mark.parametrize("layout", LAYOUTS)
def test_hook_dependents_sync_the_resolved_index_from_the_repo_root(tmp_path, monkeypatch, layout):
    repo = tmp_path / "repo"
    repo.mkdir()
    expected = _seed(repo, layout)
    script, log = _recording_engine(tmp_path)
    _fake_engine(monkeypatch, str(script))
    monkeypatch.chdir(tmp_path)

    assert dependencies._dependents(repo, "api.py") == (None, "sync_failed")

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1
    assert _db_arg(calls[0]["argv"]) == str(expected)
    assert calls[0]["argv"][2] == "sync"
    assert Path(calls[0]["cwd"]) == repo


@pytest.mark.skipif(os.name == "nt", reason="shebang stub engine")
def test_an_index_outside_the_repo_still_syncs_the_repo(tmp_path, monkeypatch):
    """`sync` indexes its working directory, so a custom index location must not move it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    custom = tmp_path / "cache" / "project.db"
    custom.parent.mkdir()
    custom.write_bytes(b"")
    monkeypatch.setenv("BRIGADE_CODE_GRAPH_DB", str(custom))
    script, log = _recording_engine(tmp_path)
    _fake_engine(monkeypatch, str(script))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert engine_paths.code_graph_repo_root(custom) is None
    assert dependencies._dependents(repo, "api.py") == (None, "sync_failed")
    graphtrail_delta.capture_before(repo, tmp_path / "run")

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["argv"][2] for call in calls] == ["sync", "sync"]
    assert all(_db_arg(call["argv"]) == str(custom) for call in calls)
    assert all(Path(call["cwd"]) == repo for call in calls)
