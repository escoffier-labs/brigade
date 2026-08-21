"""Issue #631 CLI-CONTRACT slice: versioned runs list/show/latest/watch JSON.

These tests are the CLI-owned contract gate. The browser slice must consume
these payloads and must not grow a second run-artifact reader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli
from brigade import runs_cmd


PLANTED_ENV_VALUE = "planted-env-OPENAI-not-a-real-key"
PLANTED_SECRET = "sk-live-EXAMPLESECRETVALUE99"
PLANTED_HOME = "/home/example-user/.config/brigade/secret.env"
PLANTED_ABS_HOME = "/home/someuser"
PLANTED_WIN_HOME = r"C:\Users\win-user"
PLANTED_WIN_HOME_FWD = "C:/Users/win-user"
PLANTED_DOUBLE_SLASH_HOME = "/home//alice"
PLANTED_SPACED_HOME = "/Users/John Smith"
PLANTED_APOSTROPHE_HOME = "/home/o'brien"
PLANTED_VISIBLE = "planted-visible-marker"
PLANTED_TASK = (
    f"inspect {PLANTED_VISIBLE} under {PLANTED_ABS_HOME}/project/task "
    f"{PLANTED_WIN_HOME}\\x {PLANTED_WIN_HOME_FWD}/x "
    f"{PLANTED_DOUBLE_SLASH_HOME}/x {PLANTED_SPACED_HOME}/x "
    f"{PLANTED_APOSTROPHE_HOME}/x"
)
PLANTED_ERROR = f"run error in {PLANTED_ABS_HOME}/project/error.py"
PLANTED_ABS_COMMAND = f"{PLANTED_ABS_HOME}/.venv/bin/pytest -q planted-verify"
PLANTED_ABS_FAILURE = f"worker failed in {PLANTED_ABS_HOME}/project/src/module.py"
PLANTED_ABS_FINAL = f"wrote {PLANTED_ABS_HOME}/project/final.txt"
PLANTED_PLAN_TASK = f"plan work in {PLANTED_ABS_HOME}/project/plan.py"
PLANTED_WORKER_TASK = f"implement in {PLANTED_ABS_HOME}/project/worker.py"
PLANTED_WORKER_DETAIL = f"worker wrote {PLANTED_ABS_HOME}/project/out.py"
PLANTED_WORKER_FAILURE = f"worker failure at {PLANTED_ABS_HOME}/project/fail.py"
PLANTED_SYNTHESIS_DETAIL = f"synthesis read {PLANTED_ABS_HOME}/project/synth.md"
PLANTED_ROSTER_ROLE = f"role notes {PLANTED_ABS_HOME}/project/roster.md"
PLANTED_ROSTER_REASONING = f"reason from {PLANTED_ABS_HOME}/project/reason.md"
PLANTED_ALLOW_MODEL = f"{PLANTED_ABS_HOME}/models/custom"
PLANTED_PARENT_RUN = f"{PLANTED_ABS_HOME}/project/parent-run"
PLANTED_BRANCH_POINT = f"{PLANTED_ABS_HOME}/project/branch-event"
PLANTED_LINEAGE_KIND = f"child of {PLANTED_ABS_HOME}/project"
PLANTED_BRIEF_PATH = f"{PLANTED_ABS_HOME}/project/code-graph-brief.json"
PLANTED_BRIEF_DETAIL = f"brief body {PLANTED_ABS_HOME}/project/brief.md"
PLANTED_EVENT_METHOD = f"{PLANTED_ABS_HOME}/project/event-method"
PLANTED_EVENT_ITEM_TYPE = f"{PLANTED_ABS_HOME}/project/item-type"
PLANTED_PROMPT = "PLANTED-LONG-PROMPT " + ("ignore previous instructions " * 80)
UNEXPECTED_CONTRACT_KEY = "unexpected_extra"
UNEXPECTED_CONTRACT_VALUE = "should-be-dropped"
PLANTED_NEEDLES = (
    PLANTED_ENV_VALUE,
    PLANTED_SECRET,
    PLANTED_HOME,
    PLANTED_PROMPT,
    PLANTED_ABS_HOME,
    PLANTED_TASK,
    PLANTED_ABS_COMMAND,
    PLANTED_ABS_FAILURE,
    PLANTED_ABS_FINAL,
    PLANTED_ERROR,
    PLANTED_PLAN_TASK,
    PLANTED_WORKER_TASK,
    PLANTED_WORKER_DETAIL,
    PLANTED_WORKER_FAILURE,
    PLANTED_SYNTHESIS_DETAIL,
    PLANTED_ROSTER_ROLE,
    PLANTED_ROSTER_REASONING,
    PLANTED_ALLOW_MODEL,
    PLANTED_PARENT_RUN,
    PLANTED_BRANCH_POINT,
    PLANTED_LINEAGE_KIND,
    PLANTED_BRIEF_PATH,
    PLANTED_BRIEF_DETAIL,
    PLANTED_EVENT_METHOD,
    PLANTED_EVENT_ITEM_TYPE,
    UNEXPECTED_CONTRACT_VALUE,
    PLANTED_WIN_HOME,
    PLANTED_WIN_HOME_FWD,
    PLANTED_DOUBLE_SLASH_HOME,
    PLANTED_SPACED_HOME,
    PLANTED_APOSTROPHE_HOME,
)
HOME_PREFIX_MARKERS = ("/home/", "/Users/", "C:\\Users\\", "C:/Users/")
# Leftovers the pre-#1064 regex left behind, plus Windows/double-slash usernames.
HOME_LEAK_FRAGMENTS = (
    "C:\\Users\\",
    "C:/Users/",
    "/home//",
    " Smith/",
    "'brien",
    "win-user",
)
FORBIDDEN_KEYS = frozenset(
    {
        "env",
        "auth_token",
        "owner_token",
        "prompt",
        "transcript",
        "stdout",
        "stderr",
        "stdout_log",
        "stderr_log",
        "cwd",
        "log_path",
        "control_transport",
        "artifacts",
        "handoff",
    }
)
WATCH_KNOWN_TYPES = frozenset({"watch", "run", "plan", "event", "workers", "synthesis", "final", "summary"})


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _walk_payload(value: object):
    """Yield every key and value in a JSON-compatible payload."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _walk_payload(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk_payload(inner)
    else:
        yield value


def _assert_no_home_prefix(value: object) -> None:
    """Walk every key and value; no home prefix or regex-edge leftover may remain."""
    for item in _walk_payload(value):
        if not isinstance(item, str):
            continue
        for marker in HOME_PREFIX_MARKERS:
            assert marker not in item, f"home-prefix leaked into contract field: {item!r}"
        for fragment in HOME_LEAK_FRAGMENTS:
            assert fragment not in item, f"home-prefix fragment leaked into contract field: {item!r}"


def _assert_allowlist(payload: object) -> None:
    if isinstance(payload, dict):
        assert UNEXPECTED_CONTRACT_KEY not in payload
    for item in _walk_payload(payload):
        if not isinstance(item, str):
            continue
        assert item not in FORBIDDEN_KEYS, f"forbidden key {item!r} leaked into contract payload"
        assert item != UNEXPECTED_CONTRACT_KEY
        for needle in PLANTED_NEEDLES:
            assert needle not in item, f"planted value leaked into contract payload: {needle!r}"
    _assert_no_home_prefix(payload)


def _assert_raw_clean(raw: str) -> None:
    for needle in PLANTED_NEEDLES:
        assert needle not in raw, f"planted value leaked into JSON output: {needle!r}"
    assert PLANTED_HOME.split("/")[2] not in raw
    assert PLANTED_ABS_HOME.split("/")[2] not in raw
    assert UNEXPECTED_CONTRACT_KEY not in raw
    for marker in HOME_PREFIX_MARKERS:
        assert marker not in raw, f"home-prefix leaked into JSON text: {marker!r}"
    for fragment in HOME_LEAK_FRAGMENTS:
        assert fragment not in raw, f"home-prefix fragment leaked into JSON text: {fragment!r}"
    assert json.dumps(PLANTED_WIN_HOME)[1:-1] not in raw


def _plant_sensitive_run(run_dir: Path, *, task: str = PLANTED_TASK) -> None:
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "task": task,
            "error": PLANTED_ERROR,
            "cwd": PLANTED_HOME,
            "log_path": f"{PLANTED_HOME}/run.log",
            "auth_token": PLANTED_SECRET,
            "prompt": PLANTED_PROMPT,
            "transcript": PLANTED_PROMPT,
            "stdout": PLANTED_SECRET,
            "stderr": PLANTED_SECRET,
            "env": {"OPENAI_API_KEY": PLANTED_ENV_VALUE, "HOME": PLANTED_HOME},
            "orchestrator": "chef",
            "dry_run": False,
            "read_only": False,
            "status": "ok",
            "started_at": "2026-08-21T10:00:00Z",
            "finished_at": "2026-08-21T10:00:04Z",
            "duration_seconds": 4.0,
            "artifacts": PLANTED_HOME,
            "handoff": f"{PLANTED_HOME}/handoff.md",
            "control_transport": {"owner_token": PLANTED_SECRET},
            UNEXPECTED_CONTRACT_KEY: UNEXPECTED_CONTRACT_VALUE,
            "failure": {
                "phase": "worker",
                "kind": "exit-error",
                "detail": PLANTED_ABS_FAILURE,
            },
            "lineage": {
                "kind": PLANTED_LINEAGE_KIND,
                "parent_run_id": PLANTED_PARENT_RUN,
                "branch_point_event_id": PLANTED_BRANCH_POINT,
                "env": {"OPENAI_API_KEY": PLANTED_ENV_VALUE},
                "prompt": PLANTED_PROMPT,
                UNEXPECTED_CONTRACT_KEY: UNEXPECTED_CONTRACT_VALUE,
            },
            "code_graph_brief": {
                "attached": True,
                "bytes": 32,
                "pending_count": 1,
                "path": PLANTED_BRIEF_PATH,
                "detail": PLANTED_BRIEF_DETAIL,
                UNEXPECTED_CONTRACT_KEY: UNEXPECTED_CONTRACT_VALUE,
            },
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "max_workers": 1,
            "timeout_seconds": 60.0,
            "allow_models": ["codex", PLANTED_ALLOW_MODEL, f"{PLANTED_DOUBLE_SLASH_HOME}/models/custom"],
            "agents": {
                "chef": {
                    "cli": "codex",
                    "model": "gpt-5.5",
                    "reasoning": PLANTED_ROSTER_REASONING,
                    "role": PLANTED_ROSTER_ROLE,
                    "timeout_seconds": 60.0,
                    "env": {"OPENAI_API_KEY": PLANTED_ENV_VALUE},
                }
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"stage": 1, "worker": "coder", "task": PLANTED_PLAN_TASK},
                {"stage": 2, "worker": "reviewer", "task": f"plan {PLANTED_SPACED_HOME}/project/plan.py"},
            ]
        },
    )
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "coder",
                    "task": PLANTED_WORKER_TASK,
                    "ok": True,
                    "detail": PLANTED_WORKER_DETAIL,
                    "text": PLANTED_PROMPT,
                    "stdout": PLANTED_SECRET,
                    "stderr": PLANTED_SECRET,
                    "stdout_log": f"{PLANTED_HOME}/worker.stdout.log",
                    "stderr_log": f"{PLANTED_HOME}/worker.stderr.log",
                    "failure": {
                        "class": "tool",
                        "detail": PLANTED_WORKER_FAILURE,
                    },
                },
                {
                    "worker": "reviewer",
                    "task": f"review {PLANTED_APOSTROPHE_HOME}/project/worker.py",
                    "ok": True,
                    "detail": f"reviewed {PLANTED_APOSTROPHE_HOME}/project/out.py",
                },
            ],
            "ground_truth": {
                "available": True,
                "cwd": PLANTED_HOME,
                "verify_receipts": [
                    {
                        "run_id": "verify-planted",
                        "status": "ok",
                        "started_at": "2026-08-21T10:00:01Z",
                        "duration_seconds": 1.5,
                        "commands": [
                            {
                                "command": PLANTED_ABS_COMMAND,
                                "exit_code": 0,
                                "duration_seconds": 1.4,
                            },
                            {
                                "command": rf"{PLANTED_WIN_HOME}\.venv\bin\pytest -q planted-win",
                                "exit_code": 0,
                                "duration_seconds": 0.4,
                            },
                            {
                                "command": f"{PLANTED_WIN_HOME_FWD}/.venv/bin/pytest -q planted-win-fwd",
                                "exit_code": 0,
                                "duration_seconds": 0.3,
                            },
                        ],
                    }
                ],
            },
        },
    )
    _write_json(
        run_dir / "synthesis.json",
        {
            "orchestrator": "chef",
            "mode": "merge",
            "result": {"ok": True, "detail": PLANTED_SYNTHESIS_DETAIL, "text": PLANTED_PROMPT},
        },
    )
    (run_dir / "final.txt").write_text(PLANTED_ABS_FINAL + "\n")
    events = run_dir / "events"
    events.mkdir()
    (events / "coder.jsonl").write_text(
        json.dumps(
            {
                "method": PLANTED_EVENT_METHOD,
                "params": {
                    "auth_token": PLANTED_SECRET,
                    "cwd": PLANTED_HOME,
                    "prompt": PLANTED_PROMPT,
                    "stdout": PLANTED_SECRET,
                    "item": {"id": "item-1", "type": PLANTED_EVENT_ITEM_TYPE},
                },
            }
        )
        + "\n"
    )


def _write_legacy_run(run_dir: Path) -> None:
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "task": "legacy inspect",
            "cwd": "/repo",
            "orchestrator": "chef",
            "status": "ok",
            "started_at": "2026-05-26T14:00:00Z",
            "finished_at": "2026-05-26T14:00:02Z",
            "duration_seconds": 2.0,
        },
    )
    _write_json(run_dir / "plan.json", {"assignments": [{"worker": "coder", "task": "legacy work"}]})


def test_runs_list_json_emits_versioned_schema_shape(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    _write_legacy_run(runs_root / "20260526-140000-aaaaaa")

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == runs_cmd.RUNS_LIST_SCHEMA
    assert set(payload) == {"schema", "runs", "skipped_invalid"}
    assert payload["skipped_invalid"] == 0
    assert len(payload["runs"]) == 1
    summary = payload["runs"][0]
    assert set(summary) <= runs_cmd.RUN_SUMMARY_FIELDS
    assert summary["run_id"] == "20260526-140000-aaaaaa"
    assert summary["status"] == "ok"
    assert "path" not in summary
    assert str(runs_root) not in json.dumps(payload)


def test_runs_show_and_latest_json_share_one_detail_serializer(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "20260526-140000-bbbbbb"
    _write_legacy_run(run_dir)

    assert cli.main(["runs", "show", run_dir.name, "--cwd", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert cli.main(["runs", "latest", "--cwd", str(tmp_path), "--json"]) == 0
    latest = json.loads(capsys.readouterr().out)

    assert shown == latest
    assert shown["schema"] == runs_cmd.RUN_DETAIL_SCHEMA
    assert set(shown) == runs_cmd.RUN_DETAIL_KEYS
    assert shown["run"]["run_id"] == "20260526-140000-bbbbbb"
    assert "lineage" not in shown["run"]


def test_runs_watch_json_records_are_versioned_line_valid_objects(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "20260526-140000-cccccc"
    _write_legacy_run(run_dir)
    (run_dir / "final.txt").write_text("done\n")

    assert cli.main(["runs", "watch", run_dir.name, "--cwd", str(tmp_path), "--json", "--interval", "0"]) == 0
    raw = capsys.readouterr().out
    lines = [line for line in raw.splitlines() if line.strip()]
    assert lines
    seen_types: set[str] = set()
    for line in lines:
        record = json.loads(line)
        assert record["schema"] == runs_cmd.RUN_WATCH_SCHEMA
        assert "type" in record
        record_type = record["type"]
        if record_type not in WATCH_KNOWN_TYPES:
            continue
        seen_types.add(record_type)
    assert "watch" in seen_types
    assert "run" in seen_types
    assert "summary" in seen_types


def _assert_artifacts_keep_home_needles(run_dir: Path) -> None:
    """Home-prefix redaction is JSON-only; on-disk artifacts stay verbatim."""
    disk_text = "".join(
        path.read_text()
        for path in (
            run_dir / "run.json",
            run_dir / "roster.json",
            run_dir / "plan.json",
            run_dir / "worker-results.json",
            run_dir / "synthesis.json",
            run_dir / "final.txt",
            run_dir / "events" / "coder.jsonl",
        )
    )
    for needle in (
        PLANTED_ABS_HOME,
        PLANTED_TASK,
        PLANTED_ERROR,
        PLANTED_ABS_FAILURE,
        PLANTED_PLAN_TASK,
        PLANTED_WORKER_TASK,
        PLANTED_WORKER_DETAIL,
        PLANTED_WORKER_FAILURE,
        PLANTED_SYNTHESIS_DETAIL,
        PLANTED_ROSTER_ROLE,
        PLANTED_ALLOW_MODEL,
        PLANTED_ABS_COMMAND,
        PLANTED_ABS_FINAL,
        PLANTED_BRIEF_PATH,
        PLANTED_EVENT_METHOD,
        PLANTED_WIN_HOME,
        PLANTED_WIN_HOME_FWD,
        PLANTED_DOUBLE_SLASH_HOME,
        PLANTED_SPACED_HOME,
        PLANTED_APOSTROPHE_HOME,
    ):
        escaped = json.dumps(needle)[1:-1]
        assert needle in disk_text or escaped in disk_text, f"artifact lost planted home path: {needle!r}"


def test_json_contracts_omit_planted_env_secret_home_and_prompt(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "20260821-100000-planted"
    _plant_sensitive_run(run_dir)
    _assert_artifacts_keep_home_needles(run_dir)

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--json"]) == 0
    listed_raw = capsys.readouterr().out
    listed = json.loads(listed_raw)
    _assert_allowlist(listed)
    _assert_raw_clean(listed_raw)
    assert listed["schema"] == runs_cmd.RUNS_LIST_SCHEMA
    assert listed["runs"][0]["task"]
    assert PLANTED_VISIBLE in listed["runs"][0]["task"]

    assert cli.main(["runs", "show", run_dir.name, "--cwd", str(tmp_path), "--json"]) == 0
    shown_raw = capsys.readouterr().out
    shown = json.loads(shown_raw)
    _assert_allowlist(shown)
    _assert_raw_clean(shown_raw)
    assert shown["schema"] == runs_cmd.RUN_DETAIL_SCHEMA
    assert PLANTED_VISIBLE in shown["run"]["task"]
    assert shown["briefs"]
    assert shown["briefs"][0]["name"] == "code-graph"
    assert shown["briefs"][0]["bytes"] == 32

    assert cli.main(["runs", "latest", "--cwd", str(tmp_path), "--json"]) == 0
    latest_raw = capsys.readouterr().out
    latest = json.loads(latest_raw)
    _assert_allowlist(latest)
    _assert_raw_clean(latest_raw)
    assert latest == shown

    assert cli.main(["runs", "watch", run_dir.name, "--cwd", str(tmp_path), "--json", "--interval", "0"]) == 0
    watched_raw = capsys.readouterr().out
    _assert_raw_clean(watched_raw)
    frames = [json.loads(line) for line in watched_raw.splitlines() if line.strip()]
    assert frames
    watch_saw_visible = False
    for frame in frames:
        assert frame["schema"] == runs_cmd.RUN_WATCH_SCHEMA
        _assert_allowlist(frame)
        if frame.get("type") == "run" and PLANTED_VISIBLE in str(frame.get("task", "")):
            watch_saw_visible = True
    assert watch_saw_visible, "watch JSON never surfaced the planted visible marker"

    shown_run = shown["run"]
    assert "failure" in shown_run
    assert PLANTED_ABS_HOME not in json.dumps(shown)
    assert shown_run["failure"]["detail"] == PLANTED_ABS_FAILURE.replace(PLANTED_ABS_HOME, "~")
    assert shown_run["error"] == PLANTED_ERROR.replace(PLANTED_ABS_HOME, "~")
    assert shown["plan"]["assignments"][0]["task"] == PLANTED_PLAN_TASK.replace(PLANTED_ABS_HOME, "~")
    worker = shown["workers"]["results"][0]
    assert worker["detail"] == PLANTED_WORKER_DETAIL.replace(PLANTED_ABS_HOME, "~")
    assert worker["failure"]["detail"] == PLANTED_WORKER_FAILURE.replace(PLANTED_ABS_HOME, "~")
    assert shown["synthesis"]["result"]["detail"] == PLANTED_SYNTHESIS_DETAIL.replace(PLANTED_ABS_HOME, "~")
    assert shown["roster"]["agents"]["chef"]["role"] == PLANTED_ROSTER_ROLE.replace(PLANTED_ABS_HOME, "~")
    verify_command = shown["verification"][0]["commands"][0]["command"]
    assert verify_command == PLANTED_ABS_COMMAND.replace(PLANTED_ABS_HOME, "~")
    final_frames = [frame for frame in frames if frame.get("type") == "final"]
    assert final_frames
    assert final_frames[0]["text"] == PLANTED_ABS_FINAL.replace(PLANTED_ABS_HOME, "~")

    _assert_artifacts_keep_home_needles(run_dir)
    assert cli.main(["runs", "show", run_dir.name, "--cwd", str(tmp_path)]) == 0
    human = capsys.readouterr().out
    assert PLANTED_VISIBLE in human
    assert PLANTED_TASK in human
    assert PLANTED_ERROR in human
    assert PLANTED_ABS_FAILURE in human
    assert PLANTED_PLAN_TASK in human
    assert PLANTED_WORKER_DETAIL in human
    assert PLANTED_SYNTHESIS_DETAIL in human
    assert PLANTED_ABS_COMMAND in human
    assert PLANTED_ABS_FINAL in human
    assert PLANTED_ABS_HOME in human
    assert PLANTED_WIN_HOME in human
    assert PLANTED_WIN_HOME_FWD in human
    assert PLANTED_DOUBLE_SLASH_HOME in human
    assert PLANTED_SPACED_HOME in human
    assert PLANTED_APOSTROPHE_HOME in human
    _assert_artifacts_keep_home_needles(run_dir)


def test_runs_list_json_counts_skipped_invalid(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    _write_legacy_run(runs_root / "20260526-140000-valid1")
    missing = runs_root / "20260526-150000-missing"
    missing.mkdir(parents=True)
    corrupt = runs_root / "20260526-160000-corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "run.json").write_text("not json")

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == runs_cmd.RUNS_LIST_SCHEMA
    assert payload["skipped_invalid"] == 2
    assert [run["run_id"] for run in payload["runs"]] == ["20260526-140000-valid1"]


def test_legacy_run_without_lineage_still_serializes(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "20260101-090000-legacy"
    _write_legacy_run(run_dir)

    assert runs_cmd.show(run_dir, json_output=True) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["schema"] == runs_cmd.RUN_DETAIL_SCHEMA
    assert set(detail) == runs_cmd.RUN_DETAIL_KEYS
    assert "lineage" not in detail["run"]
    assert "parent_run_id" not in detail["run"]

    assert runs_cmd.list_runs(cwd=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["runs"][0]["run_id"] == "20260101-090000-legacy"
    assert "parent_run_id" not in listed["runs"][0]
    assert "lineage" not in listed["runs"][0]


def test_show_json_includes_recorded_children_from_sibling_receipts(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_legacy_run(parent)
    _write_legacy_run(child)
    payload = json.loads((child / "run.json").read_text())
    payload["lineage"] = {
        "kind": "child",
        "parent_run_id": parent.name,
        "branch_point_event_id": "20260821-120000-parent-000003-bbbbbbbbbbbb",
        "env": {"OPENAI_API_KEY": PLANTED_ENV_VALUE},
        "prompt": PLANTED_PROMPT,
        UNEXPECTED_CONTRACT_KEY: UNEXPECTED_CONTRACT_VALUE,
    }
    payload[UNEXPECTED_CONTRACT_KEY] = UNEXPECTED_CONTRACT_VALUE
    _write_json(child / "run.json", payload)

    assert cli.main(["runs", "show", parent.name, "--cwd", str(tmp_path), "--json"]) == 0
    shown_raw = capsys.readouterr().out
    shown = json.loads(shown_raw)
    _assert_allowlist(shown)
    _assert_raw_clean(shown_raw)
    assert shown["run"]["lineage"] == {
        "children": [
            {
                "run_id": child.name,
                "status": "ok",
                "branch_point_event_id": "20260821-120000-parent-000003-bbbbbbbbbbbb",
            }
        ]
    }


def test_child_lineage_is_allowlisted_on_show_and_list(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    child = runs_root / "20260821-130000-child"
    _write_legacy_run(child)
    payload = json.loads((child / "run.json").read_text())
    payload["lineage"] = {
        "kind": "child",
        "parent_run_id": "20260821-120000-parent",
        "branch_point_event_id": "20260821-120000-parent-000003-bbbbbbbbbbbb",
        "shared_prefix": {
            "event_sequence": 3,
            "event_digest": "b" * 64,
            "previous_digest": "a" * 64,
            "secret": PLANTED_SECRET,
            "cwd": PLANTED_HOME,
            UNEXPECTED_CONTRACT_KEY: UNEXPECTED_CONTRACT_VALUE,
        },
        "env": {"OPENAI_API_KEY": PLANTED_ENV_VALUE},
        "prompt": PLANTED_PROMPT,
        UNEXPECTED_CONTRACT_KEY: UNEXPECTED_CONTRACT_VALUE,
    }
    payload[UNEXPECTED_CONTRACT_KEY] = UNEXPECTED_CONTRACT_VALUE
    _write_json(child / "run.json", payload)

    assert cli.main(["runs", "show", child.name, "--cwd", str(tmp_path), "--json"]) == 0
    shown_raw = capsys.readouterr().out
    shown = json.loads(shown_raw)
    _assert_allowlist(shown)
    _assert_raw_clean(shown_raw)
    assert shown["run"]["lineage"] == {
        "kind": "child",
        "parent_run_id": "20260821-120000-parent",
        "branch_point_event_id": "20260821-120000-parent-000003-bbbbbbbbbbbb",
        "shared_prefix": {
            "event_sequence": 3,
            "event_digest": "b" * 64,
            "previous_digest": "a" * 64,
        },
    }

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--json"]) == 0
    listed_raw = capsys.readouterr().out
    listed = json.loads(listed_raw)
    _assert_allowlist(listed)
    _assert_raw_clean(listed_raw)
    assert listed["runs"][0]["parent_run_id"] == "20260821-120000-parent"


def test_runs_list_json_does_not_scan_sibling_run_json(tmp_path, capsys, monkeypatch):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_legacy_run(parent)
    _write_legacy_run(child)
    payload = json.loads((child / "run.json").read_text())
    payload["started_at"] = "2026-08-21T13:00:00Z"
    payload["lineage"] = {"kind": "child", "parent_run_id": parent.name}
    _write_json(child / "run.json", payload)

    reads: list[str] = []
    original_read = runs_cmd._read_json

    def counting_read(path: Path):
        if path.name == "run.json":
            reads.append(path.parent.name)
        return original_read(path)

    def boom_recorded_children(run_dir: Path):
        raise AssertionError(f"list path scanned siblings for {run_dir}")

    monkeypatch.setattr(runs_cmd, "_read_json", counting_read)
    monkeypatch.setattr(runs_cmd, "_recorded_children", boom_recorded_children)

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [run["run_id"] for run in listed["runs"]] == [child.name, parent.name]
    assert listed["runs"][0]["parent_run_id"] == parent.name
    assert "parent_run_id" not in listed["runs"][1]
    assert reads == [parent.name, child.name] or reads == [child.name, parent.name]
    assert reads.count(parent.name) == 1
    assert reads.count(child.name) == 1


def test_show_json_bounds_oversized_sibling_child_status(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    parent = runs_root / "20260821-120000-parent"
    child = runs_root / "20260821-130000-child"
    _write_legacy_run(parent)
    _write_legacy_run(child)
    payload = json.loads((child / "run.json").read_text())
    payload["status"] = "x" * 50_000
    payload["lineage"] = {
        "kind": "child",
        "parent_run_id": parent.name,
        "branch_point_event_id": "y" * 50_000,
        UNEXPECTED_CONTRACT_KEY: UNEXPECTED_CONTRACT_VALUE,
    }
    payload[UNEXPECTED_CONTRACT_KEY] = UNEXPECTED_CONTRACT_VALUE
    _write_json(child / "run.json", payload)

    assert cli.main(["runs", "show", parent.name, "--cwd", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    _assert_allowlist(shown)
    child_entry = shown["run"]["lineage"]["children"][0]
    assert child_entry["run_id"] == child.name
    assert len(child_entry["status"]) <= 400
    assert len(child_entry["branch_point_event_id"]) <= 400
    assert child_entry["status"].endswith("...")
    assert child_entry["branch_point_event_id"].endswith("...")
    assert UNEXPECTED_CONTRACT_KEY not in child_entry

    assert cli.main(["runs", "show", parent.name, "--cwd", str(tmp_path)]) == 0
    human = capsys.readouterr().out
    assert "x" * 50_000 in human
    assert "y" * 50_000 in human


def test_human_list_and_show_remain_text_when_json_omitted(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "20260526-140000-human"
    _write_legacy_run(run_dir)

    assert cli.main(["runs", "list", "--cwd", str(tmp_path)]) == 0
    listed = capsys.readouterr().out
    assert listed.startswith("2026-05-26T14:00:00Z [ok]")
    assert "brigade.runs-list.v1" not in listed

    assert cli.main(["runs", "show", run_dir.name, "--cwd", str(tmp_path)]) == 0
    shown = capsys.readouterr().out
    assert shown.startswith(f"run: {run_dir.resolve()}")
    assert "brigade.run-detail.v1" not in shown


def test_allowlisted_rejects_allowlist_key_without_cleaned_counterpart():
    """An allowlist key with no cleaned overlay cannot pass a raw source value."""
    source = {"task": "raw-task", "secret": "raw-secret"}
    cleaned = {"task": "clean-task"}
    with pytest.raises(AssertionError, match="cleaned counterpart"):
        runs_cmd._allowlisted(
            runs_cmd._merge_source(source, cleaned),
            frozenset({"task", "secret"}),
        )
    payload = runs_cmd._allowlisted(
        runs_cmd._merge_source(source, {**cleaned, "secret": None}),
        frozenset({"task", "secret"}),
    )
    assert payload == {"task": "clean-task"}
