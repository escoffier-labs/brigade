"""Offline drive of native T3 batch enrollment (`brigade t3 open`)."""

import json
import os
from pathlib import Path

TASK_TEXT = "verify-brigade t3 enrollment marker"

ROSTER = """\
orchestrator = "coder"

[agents.coder]
cli = "codex"
role = "coder"

[t3_targets.coder]
origin_environment = "env-origin"
origin_thread = "thread-parent"
target_environment = "env-target"
target_project = "project-example"
provider_instance = "codex-example"
driver = "codex"
model = "example-model"
isolation = "worktree"

[t3_targets.coder.options]
reasoningEffort = "high"
"""

BATCH = {
    "schema": "brigade.run_batch.v1",
    "request_id": "verify-brigade-t3",
    "tasks": [
        {"task_id": "one", "text": f"{TASK_TEXT} one", "seat": "coder", "isolation": "worktree"},
        {"task_id": "two", "text": f"{TASK_TEXT} two", "seat": "coder", "isolation": "worktree"},
    ],
}


def add_parser(sub, helper):
    parser = sub.add_parser("t3-enrollment", help="Enroll a two-task batch in a clean temp workspace.")
    parser.add_argument("--target", required=True, help="Temp target created by new-target.")
    parser.set_defaults(func=lambda args: command(args, helper))


def _mode(path: Path) -> str:
    """Report the POSIX mode when the platform has one."""
    if os.name != "posix":
        return "platform-default"
    return oct(path.stat().st_mode & 0o777)


def command(args, helper):
    root = helper.state_root(args)
    target = helper.guard_target(Path(args.target), root)
    work = target / "t3-workspace"
    work.mkdir(mode=0o700)
    records = [
        helper.run(["git", "-C", str(work), "init", "-q", "-b", "main"], root=root, name="t3-git-init", timeout=60),
        helper.run(
            ["git", "-C", str(work), "config", "user.email", "drive@example.invalid"],
            root=root,
            name="t3-git-email",
            timeout=60,
        ),
        helper.run(
            ["git", "-C", str(work), "config", "user.name", "Drive"],
            root=root,
            name="t3-git-name",
            timeout=60,
        ),
    ]
    (work / "tracked.txt").write_text("base\n", encoding="utf-8")
    records.append(helper.run(["git", "-C", str(work), "add", "tracked.txt"], root=root, name="t3-git-add", timeout=60))
    records.append(
        helper.run(
            ["git", "-C", str(work), "commit", "-qm", "drive baseline"],
            root=root,
            name="t3-git-commit",
            timeout=60,
        )
    )
    brigade_dir = work / ".brigade"
    brigade_dir.mkdir(mode=0o700)
    (brigade_dir / "roster.toml").write_text(ROSTER, encoding="utf-8")
    batch = target / "batch.json"
    batch.write_text(json.dumps(BATCH), encoding="utf-8")

    base = helper.brigade_args(args) + ["t3", "open", str(batch), "--target", str(work)]
    first = helper.run(base, root=root, name="t3-open")
    replay = helper.run(base, root=root, name="t3-open-replay")

    report = first.get("json") or {}
    run_dir = work / ".brigade" / "runs" / str(report.get("run_id"))
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text(encoding="utf-8")) if run_json.is_file() else {}
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal_text = journal.read_text(encoding="utf-8") if journal.is_file() else TASK_TEXT
    assets = sorted(run_dir.glob("native-input-*.json")) if run_dir.is_dir() else []
    tasks = report.get("tasks") or []
    ok = (
        all(record["returncode"] == 0 for record in records)
        and first["returncode"] == 0
        and replay["returncode"] == 0
        and report.get("schema") == "brigade.t3_enrollment.v1"
        and [task.get("task_id") for task in tasks] == ["one", "two"]
        and all(task.get("state") == "enrolled" for task in tasks)
        and (replay.get("json") or {}).get("run_id") == report.get("run_id")
        and meta.get("status") == "awaiting-native-parent"
        and TASK_TEXT not in journal_text
        and len(assets) == 2
        and _mode(run_dir) in {"0o700", "platform-default"}
        and {_mode(asset) for asset in assets} <= {"0o600", "platform-default"}
    )
    return helper.emit(
        {
            "action": "t3-enrollment",
            "ok": ok,
            "run_id": report.get("run_id"),
            "tasks": [{"task_id": task.get("task_id"), "state": task.get("state")} for task in tasks],
            "run_status": meta.get("status"),
            "assets": [{"name": asset.name, "mode": _mode(asset)} for asset in assets],
            "journal_mentions_task_text": TASK_TEXT in journal_text,
            "git": [helper.slim(record) for record in records],
            "open": helper.slim(first),
            "replay": helper.slim(replay),
        },
        0 if ok else 3,
    )
