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


def test_older_engine_without_floor_marker_keeps_the_markdown_path(tmp_path, monkeypatch):
    _graph_db(tmp_path)
    calls: list = []
    old_markdown = FLOORED_MARKDOWN.replace(" - relevance floor name-coverage-v1", "")
    _fake_engine(monkeypatch, old_markdown, FLOORED_JSON, calls)

    brief = aboyeur.code_graph_brief(tmp_path, "fix extract_delta_files")

    assert len(calls) == 1
    assert "--markdown" in calls[0]
    assert brief.attached is True
    assert brief.confident is True
    assert brief.floor_applied is False
    assert brief.symbols is None
    assert brief.files is None


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
        text="## Code graph context\n\n- `a` (function) - src/a.py:1-2\n",
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
        text="## Code graph context\n\n- src/a.py\n- src/noise.py\n- tests/test_noise.py\n",
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
