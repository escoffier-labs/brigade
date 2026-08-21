"""Issue #631 CLI-CONTRACT slice: versioned runs list/show/latest/watch JSON."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli
from brigade import runs_cmd


PLANTED_ENV_VALUE = "planted-env-OPENAI-not-a-real-key"
PLANTED_SECRET = "sk-live-EXAMPLESECRETVALUE99"
PLANTED_HOME = "/home/example-user/.config/brigade/secret.env"
PLANTED_PROMPT = "PLANTED-LONG-PROMPT " + ("ignore previous instructions " * 80)
PLANTED_NEEDLES = (PLANTED_ENV_VALUE, PLANTED_SECRET, PLANTED_HOME, PLANTED_PROMPT)
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


def _assert_allowlist(payload: object) -> None:
    for item in _walk_payload(payload):
        if not isinstance(item, str):
            continue
        assert item not in FORBIDDEN_KEYS, f"forbidden key {item!r} leaked into contract payload"
        for needle in PLANTED_NEEDLES:
            assert needle not in item, f"planted value leaked into contract payload: {needle!r}"


def _assert_raw_clean(raw: str) -> None:
    for needle in PLANTED_NEEDLES:
        assert needle not in raw, f"planted value leaked into JSON output: {needle!r}"
    assert PLANTED_HOME.split("/")[2] not in raw


def _plant_sensitive_run(run_dir: Path, *, task: str = "inspect the planted run") -> None:
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "task": task,
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
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "max_workers": 1,
            "timeout_seconds": 60.0,
            "allow_models": ["codex"],
            "agents": {
                "chef": {
                    "cli": "codex",
                    "model": "gpt-5.5",
                    "role": "plan",
                    "timeout_seconds": 60.0,
                    "env": {"OPENAI_API_KEY": PLANTED_ENV_VALUE},
                }
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {"assignments": [{"stage": 1, "worker": "coder", "task": "implement it"}]},
    )
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "coder",
                    "task": "implement it",
                    "ok": True,
                    "detail": "",
                    "text": PLANTED_PROMPT,
                    "stdout": PLANTED_SECRET,
                    "stderr": PLANTED_SECRET,
                    "stdout_log": f"{PLANTED_HOME}/worker.stdout.log",
                    "stderr_log": f"{PLANTED_HOME}/worker.stderr.log",
                }
            ]
        },
    )
    _write_json(
        run_dir / "synthesis.json",
        {"orchestrator": "chef", "result": {"ok": True, "detail": "", "text": PLANTED_PROMPT}},
    )
    (run_dir / "final.txt").write_text("final answer\n")
    events = run_dir / "events"
    events.mkdir()
    (events / "coder.jsonl").write_text(
        json.dumps(
            {
                "method": "agent/message",
                "params": {
                    "auth_token": PLANTED_SECRET,
                    "cwd": PLANTED_HOME,
                    "prompt": PLANTED_PROMPT,
                    "stdout": PLANTED_SECRET,
                    "item": {"id": "item-1", "type": "commandExecution"},
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


def test_json_contracts_omit_planted_env_secret_home_and_prompt(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "20260821-100000-planted"
    _plant_sensitive_run(run_dir)

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--json"]) == 0
    listed_raw = capsys.readouterr().out
    listed = json.loads(listed_raw)
    _assert_allowlist(listed)
    _assert_raw_clean(listed_raw)
    assert listed["schema"] == runs_cmd.RUNS_LIST_SCHEMA

    assert cli.main(["runs", "show", run_dir.name, "--cwd", str(tmp_path), "--json"]) == 0
    shown_raw = capsys.readouterr().out
    shown = json.loads(shown_raw)
    _assert_allowlist(shown)
    _assert_raw_clean(shown_raw)
    assert shown["schema"] == runs_cmd.RUN_DETAIL_SCHEMA

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
    for frame in frames:
        assert frame["schema"] == runs_cmd.RUN_WATCH_SCHEMA
        _assert_allowlist(frame)


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
        },
        "env": {"OPENAI_API_KEY": PLANTED_ENV_VALUE},
        "prompt": PLANTED_PROMPT,
    }
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
