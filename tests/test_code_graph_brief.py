"""The code-graph brief records what it showed as data, not only markdown (#1648)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from brigade import agents, aboyeur, proc
from brigade.aboyeur import artifacts, briefs
from brigade.roster import Agent, Roster
from tests.run_test_helpers import run_aboyeur_guarded

FLOORED_MARKDOWN = """# Context Pack: fix extract_delta_files

_schema v7 - 1 entry points - 1 callers - 0 callees - 2 related files - relevance floor name-coverage-v1_

## Entry points

- `extract_delta_files` (function) - src/brigade/context_eval.py:80-102

## Callers

- `_context_eval_for_run` -> `extract_delta_files` - src/brigade/aboyeur/prompts.py:528 -> src/brigade/context_eval.py

## Related files

- src/brigade/aboyeur/prompts.py
- src/brigade/context_eval.py
"""

FLOORED_JSON = {
    "schema_version": 7,
    "task": "fix extract_delta_files",
    "confident": True,
    "relevance_floor": {
        "rule": "name-coverage-v1",
        "min_name_coverage": 0.55,
        "candidates": 40,
        "kept": 1,
        "dropped": 39,
    },
    "entry_points": [
        {
            "id": "sym-extract",
            "kind": "function",
            "name": "extract_delta_files",
            "qualified_name": "extract_delta_files",
            "file_path": "src/brigade/context_eval.py",
            "start_line": 80,
            "end_line": 102,
            "signature": "def extract_delta_files(...)",
            "score": 19.666,
        }
    ],
    "callers": [],
    "callees": [],
    "related_files": ["src/brigade/aboyeur/prompts.py", "src/brigade/context_eval.py"],
}

NO_CONTEXT_MARKDOWN = """# Context Pack: fix typo in CHANGELOG

_schema v7 - 0 entry points - 0 callers - 0 callees - 0 related files - relevance floor name-coverage-v1_

No confident code context for this task. No indexed symbol matched a code identifier, file path, or distinctive name in the task, so no entry points are listed. Search the repository directly if the change touches code.
"""

SUMMARY = "_schema v7 - 1 entry points - 0 callers - 0 callees - 1 related files - relevance floor task-coverage-v2_"


def _shown(task: str, graph: str) -> str:
    """Attached brief text in the engine's shape: heading, task, summary, graph sections."""
    return f"## Code graph context\n\n# Context Pack: {task}\n\n{SUMMARY}\n\n{graph}"


NO_CONTEXT_JSON = {
    "schema_version": 7,
    "task": "fix typo in CHANGELOG",
    "confident": False,
    "relevance_floor": {
        "rule": "name-coverage-v1",
        "min_name_coverage": 0.55,
        "candidates": 6,
        "kept": 0,
        "dropped": 6,
    },
    "entry_points": [],
    "callers": [],
    "callees": [],
    "related_files": [],
}


def _graph_db(tmp_path):
    db = tmp_path / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir(parents=True)
    db.write_text("")
    return db


def _fake_engine(monkeypatch, markdown: str, payload: object, calls: list | None = None):
    monkeypatch.setattr(aboyeur, "_graphtrail_bin", lambda: "/bin/graphtrail")

    def fake_run(args, **kw):
        if calls is not None:
            calls.append(list(args))
        if "--json" in args:
            text = payload if isinstance(payload, str) else json.dumps(payload)
            return proc.Result(code=0, stdout=text, stderr="")
        return proc.Result(code=0, stdout=markdown, stderr="")

    monkeypatch.setattr(aboyeur.proc, "run", fake_run)


def test_floored_engine_brief_records_entry_points_and_files(tmp_path, monkeypatch):
    db = _graph_db(tmp_path)
    calls: list = []
    _fake_engine(monkeypatch, FLOORED_MARKDOWN, FLOORED_JSON, calls)

    brief = aboyeur.code_graph_brief(tmp_path, "fix extract_delta_files")

    assert calls == [
        ["/bin/graphtrail", "--db", str(db), "context", "fix extract_delta_files", "--markdown", "--limit", "8"],
        ["/bin/graphtrail", "--db", str(db), "context", "fix extract_delta_files", "--json", "--limit", "8"],
    ]
    assert brief.attached is True
    assert brief.confident is True
    assert brief.floor_applied is True
    assert "extract_delta_files" in brief.text
    assert brief.symbols == (
        {
            "id": "sym-extract",
            "qualified_name": "extract_delta_files",
            "file_path": "src/brigade/context_eval.py",
            "score": 19.666,
        },
    )
    assert brief.files == ("src/brigade/aboyeur/prompts.py", "src/brigade/context_eval.py")


def test_no_confident_context_brief_is_attached_and_marked(tmp_path, monkeypatch):
    _graph_db(tmp_path)
    _fake_engine(monkeypatch, NO_CONTEXT_MARKDOWN, NO_CONTEXT_JSON)

    brief = aboyeur.code_graph_brief(tmp_path, "fix typo in CHANGELOG")

    assert brief.attached is True
    assert brief.confident is False
    assert brief.floor_applied is True
    assert brief.symbols == ()
    assert brief.files == ()
    assert "No confident code context for this task." in brief.text
    assert brief.text.startswith(briefs.CODE_GRAPH_HEADING)


def test_multiline_task_still_detects_a_floored_engine(tmp_path, monkeypatch):
    _graph_db(tmp_path)
    calls: list = []
    task = "fix extract_delta_files\n\nreading the wrong keys\nand more\ndetail"
    markdown = FLOORED_MARKDOWN.replace("# Context Pack: fix extract_delta_files", f"# Context Pack: {task}")
    _fake_engine(monkeypatch, markdown, FLOORED_JSON, calls)

    brief = aboyeur.code_graph_brief(tmp_path, task)

    assert [call[5] for call in calls] == ["--markdown", "--json"]
    assert brief.floor_applied is True
    assert brief.symbols is not None and brief.symbols[0]["id"] == "sym-extract"


def test_unfloored_pack_still_records_brief_data(tmp_path, monkeypatch):
    _graph_db(tmp_path)
    calls: list = []
    unfloored_markdown = FLOORED_MARKDOWN.replace(" - relevance floor name-coverage-v1", "")
    unfloored_json = {key: value for key, value in FLOORED_JSON.items() if key != "relevance_floor"}
    _fake_engine(monkeypatch, unfloored_markdown, unfloored_json, calls)

    brief = aboyeur.code_graph_brief(tmp_path, "fix extract_delta_files")

    assert [call[5] for call in calls] == ["--markdown", "--json"]
    assert all("--relevance-floor" not in call for call in calls)
    assert brief.attached is True
    assert brief.confident is True
    assert brief.floor_applied is False
    assert brief.symbols is not None and brief.symbols[0]["id"] == "sym-extract"
    assert briefs.shown_brief_files(brief) == ["src/brigade/aboyeur/prompts.py", "src/brigade/context_eval.py"]


def test_relevance_floor_is_passed_only_when_the_operator_enables_it(tmp_path, monkeypatch):
    _graph_db(tmp_path)
    calls: list = []
    monkeypatch.setenv("BRIGADE_BRIEF_RELEVANCE_FLOOR", "1")
    _fake_engine(monkeypatch, FLOORED_MARKDOWN, FLOORED_JSON, calls)

    brief = aboyeur.code_graph_brief(tmp_path, "fix extract_delta_files")

    assert [call[5:] for call in calls] == [
        ["--markdown", "--limit", "8", "--relevance-floor"],
        ["--json", "--limit", "8", "--relevance-floor"],
    ]
    assert brief.floor_applied is True


def test_relevance_floor_falls_back_when_the_engine_lacks_the_flag(tmp_path, monkeypatch):
    _graph_db(tmp_path)
    calls: list = []
    monkeypatch.setenv("BRIGADE_BRIEF_RELEVANCE_FLOOR", "1")
    monkeypatch.setattr(aboyeur, "_graphtrail_bin", lambda: "/bin/graphtrail")
    old_markdown = FLOORED_MARKDOWN.replace(" - relevance floor name-coverage-v1", "")
    old_json = {key: value for key, value in FLOORED_JSON.items() if key not in {"confident", "relevance_floor"}}

    def fake_run(args, **kw):
        calls.append(list(args))
        if "--relevance-floor" in args:
            return proc.Result(code=2, stdout="", stderr="error: unexpected argument '--relevance-floor'")
        if "--json" in args:
            return proc.Result(code=0, stdout=json.dumps(old_json), stderr="")
        return proc.Result(code=0, stdout=old_markdown, stderr="")

    monkeypatch.setattr(aboyeur.proc, "run", fake_run)

    brief = aboyeur.code_graph_brief(tmp_path, "fix extract_delta_files")

    assert [("--relevance-floor" in call, call[5]) for call in calls] == [
        (True, "--markdown"),
        (False, "--markdown"),
        (False, "--json"),
    ]
    assert brief.attached is True
    assert brief.confident is True
    assert brief.floor_applied is False
    assert brief.symbols is not None


def test_missing_confident_field_counts_as_confident(tmp_path, monkeypatch):
    _graph_db(tmp_path)
    payload = {key: value for key, value in FLOORED_JSON.items() if key not in {"confident", "relevance_floor"}}
    _fake_engine(monkeypatch, FLOORED_MARKDOWN, payload)

    brief = aboyeur.code_graph_brief(tmp_path, "fix extract_delta_files")

    assert brief.confident is True
    assert brief.floor_applied is False
    assert brief.files == ("src/brigade/aboyeur/prompts.py", "src/brigade/context_eval.py")


def test_unreadable_json_keeps_the_markdown_brief_without_data(tmp_path, monkeypatch):
    _graph_db(tmp_path)
    _fake_engine(monkeypatch, FLOORED_MARKDOWN, "not json")

    brief = aboyeur.code_graph_brief(tmp_path, "fix extract_delta_files")

    assert brief.attached is True
    assert "extract_delta_files" in brief.text
    assert brief.confident is True
    assert brief.symbols is None
    assert brief.files is None


def test_arbitration_keeps_the_recorded_brief_data():
    brief = aboyeur.CodeGraphBrief(
        attached=True,
        text="## Code graph context\n\n- src/a.py\n" + ("x\n" * 400),
        bytes=900,
        confident=True,
        floor_applied=True,
        symbols=({"id": "a", "qualified_name": "a", "file_path": "src/a.py", "score": 1.0},),
        files=("src/a.py",),
    )

    kept = aboyeur.arbitrate_briefs("fix a", code_graph=brief, drift_impact=aboyeur.DriftImpactBrief(attached=False))

    assert kept.code_graph.attached is True
    assert kept.code_graph.floor_applied is True
    assert kept.code_graph.symbols == brief.symbols
    assert kept.code_graph.files == ("src/a.py",)


def _roster():
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "ollama:llama3.3", "write code"),
        },
        max_workers=2,
    )


def _payload(code_graph):
    return artifacts._run_payload(
        task="fix a",
        cwd=None,
        lock_workspace=None,
        roster=_roster(),
        dry_run=True,
        read_only=False,
        status="completed",
        started_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
        code_graph=code_graph,
        include_git=False,
    )


def test_run_payload_records_only_files_the_brief_showed():
    brief = aboyeur.CodeGraphBrief(
        attached=True,
        text=_shown("fix a", "## Entry points\n\n- `a` (function) - src/a.py:1-2\n"),
        bytes=60,
        confident=True,
        floor_applied=True,
        symbols=({"id": "a", "qualified_name": "a", "file_path": "src/a.py", "score": 2.5},),
        files=("src/a.py", "src/cut_by_truncation.py"),
    )

    payload = _payload(brief)

    assert payload["code_graph_brief"] == {
        "attached": True,
        "bytes": 60,
        "confident": True,
        "floor_applied": True,
        "symbols": [{"id": "a", "qualified_name": "a", "file_path": "src/a.py", "score": 2.5}],
        "files": ["src/a.py"],
    }


def test_run_payload_drops_symbols_the_attached_text_no_longer_shows():
    brief = aboyeur.CodeGraphBrief(
        attached=True,
        text="## Code graph context\n\n# Context Pack: fix extract_delta_files\n",
        bytes=93,
        confident=True,
        floor_applied=True,
        symbols=(
            {
                "id": "sym-extract",
                "qualified_name": "extract_delta_files",
                "file_path": "src/brigade/context_eval.py",
                "score": 1.0,
            },
        ),
        files=("src/brigade/context_eval.py",),
    )

    recorded = _payload(brief)["code_graph_brief"]

    assert recorded["symbols"] == []
    assert recorded["files"] == []


def test_task_text_naming_a_truncated_file_does_not_count_as_shown():
    task = "fix extract_delta_files in src/brigade/context_eval.py"
    brief = aboyeur.CodeGraphBrief(
        attached=True,
        text=_shown(task, "## Entry points\n\n\n[GraphTrail context truncated to 4000 chars.]\n"),
        bytes=120,
        confident=True,
        floor_applied=True,
        symbols=(
            {
                "id": "sym-extract",
                "qualified_name": "extract_delta_files",
                "file_path": "src/brigade/context_eval.py",
                "score": 1.0,
            },
        ),
        files=("src/brigade/context_eval.py",),
    )

    recorded = _payload(brief)["code_graph_brief"]

    assert recorded["symbols"] == []
    assert recorded["files"] == []
    assert briefs.shown_brief_files(brief) == []


def test_files_in_entry_and_edge_lines_count_as_shown():
    graph = (
        "## Entry points\n\n- `run` (function) - src/app.py:5-7\n\n"
        "## Callers\n\n- `main` -> `run` - src/cli.py:12 -> src/app.py\n\n"
        "## Callees\n\n- `run` -> `helper` - src/app.py:6 -> src/lib.py\n"
    )
    brief = aboyeur.CodeGraphBrief(
        attached=True,
        text=_shown("fix run", graph),
        bytes=200,
        symbols=({"id": "r", "qualified_name": "run", "file_path": "src/app.py", "score": 1.0},),
        files=("src/app.py", "src/cli.py", "src/lib.py", "src/app.pyi"),
    )

    assert briefs.shown_brief_files(brief) == ["src/app.py", "src/cli.py", "src/lib.py"]


def test_shown_symbols_match_on_file_as_well_as_name():
    graph = "## Entry points\n\n- `register` (function) - src/brigade/cli/fleet_dot.py:10-20\n"
    brief = aboyeur.CodeGraphBrief(
        attached=True,
        text=_shown("register fleet and dot commands", graph),
        bytes=200,
        symbols=(
            {"id": "a", "qualified_name": "register", "file_path": "src/brigade/cli/fleet.py", "score": 2.0},
            {"id": "b", "qualified_name": "register", "file_path": "src/brigade/cli/fleet_dot.py", "score": 1.0},
        ),
        files=("src/brigade/cli/fleet.py", "src/brigade/cli/fleet_dot.py"),
    )

    recorded = _payload(brief)["code_graph_brief"]

    assert [symbol["id"] for symbol in recorded["symbols"]] == ["b"]
    assert recorded["files"] == ["src/brigade/cli/fleet_dot.py"]


def test_unfloored_summary_line_still_anchors_the_graph_sections():
    text = (
        "## Code graph context\n\n# Context Pack: fix src/a.py\n\n"
        "_schema v7 - 1 entry points - 0 callers - 0 callees - 1 related files_\n\n"
        "## Entry points\n\n- `run` (function) - src/b.py:1-2\n"
    )
    brief = aboyeur.CodeGraphBrief(
        attached=True, text=text, bytes=len(text), symbols=(), files=("src/a.py", "src/b.py")
    )

    assert briefs.shown_brief_files(brief) == ["src/b.py"]


def test_run_payload_keeps_the_old_shape_without_brief_data():
    brief = aboyeur.CodeGraphBrief(attached=True, text="## Code graph context\n\ngraph\n", bytes=30)

    assert _payload(brief)["code_graph_brief"] == {"attached": True, "bytes": 30}
    assert _payload(None)["code_graph_brief"] == {"attached": False, "bytes": 0}


def test_run_payload_records_a_no_confident_context_brief():
    brief = aboyeur.CodeGraphBrief(
        attached=True,
        text="## Code graph context\n\nNo confident code context for this task.\n",
        bytes=64,
        confident=False,
        floor_applied=True,
        symbols=(),
        files=(),
    )

    assert _payload(brief)["code_graph_brief"] == {
        "attached": True,
        "bytes": 64,
        "confident": False,
        "floor_applied": True,
        "symbols": [],
        "files": [],
    }


def test_context_eval_prefers_recorded_files_over_markdown(tmp_path):
    sidecar = tmp_path / "graph-delta.json"
    nodes = [{"file_path": "src/a.py"}, {"file_path": "src/b.py"}]
    sidecar.write_text(json.dumps({"ok": True, "code_reference_nodes": nodes}) + "\n")
    delta = {"ok": True, "sidecar_path": str(sidecar)}
    brief = aboyeur.CodeGraphBrief(
        attached=True,
        text=_shown("fix a", "## Related files\n\n- src/a.py\n- src/noise.py\n- tests/test_noise.py\n"),
        bytes=80,
        floor_applied=True,
        symbols=(),
        files=("src/a.py", "src/noise.py"),
    )

    result = aboyeur._context_eval_for_run(brief, delta)

    assert result is not None
    assert result["brief_files_source"] == "recorded"
    assert result["counts"]["brief_files"] == 2
    assert result["brief_hit_rate"] == 0.5
    assert result["brief_precision"] == 0.5
    assert result["brief_f05"] == 0.5


def test_context_eval_falls_back_to_markdown_for_briefs_without_data(tmp_path):
    sidecar = tmp_path / "graph-delta.json"
    sidecar.write_text(json.dumps({"ok": True, "code_reference_nodes": [{"file_path": "src/a.py"}]}) + "\n")
    delta = {"ok": True, "sidecar_path": str(sidecar)}
    brief = aboyeur.CodeGraphBrief(attached=True, text="- `src/a.py:1`\n- `src/b.py:2`\n", bytes=30)

    result = aboyeur._context_eval_for_run(brief, delta)

    assert result is not None
    assert result["brief_files_source"] == "markdown"
    assert result["brief_hit_rate"] == 1.0
    assert result["brief_precision"] == 0.5


def test_run_receipt_carries_structured_brief_from_a_floored_engine(monkeypatch, tmp_path):
    work = tmp_path / "work"
    _graph_db(work)
    _fake_engine(monkeypatch, FLOORED_MARKDOWN, FLOORED_JSON)

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        if "assignments" in prompt:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    output_dir = tmp_path / "run"

    assert run_aboyeur_guarded("fix extract_delta_files", _roster(), cwd=work, output_dir=output_dir) == 0

    recorded = json.loads((output_dir / "run.json").read_text())["code_graph_brief"]
    assert recorded["attached"] is True
    assert recorded["confident"] is True
    assert recorded["floor_applied"] is True
    assert recorded["symbols"][0]["id"] == "sym-extract"
    assert recorded["files"] == ["src/brigade/aboyeur/prompts.py", "src/brigade/context_eval.py"]
