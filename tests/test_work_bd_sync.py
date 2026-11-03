"""The bd mirror reads bd only, keys tasks by bd id, and lets readiness honor bd edges."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import cli
from brigade.work_cmd import bd_sync, ledger
from brigade.work_cmd.import_bd import BdImportError

FIXTURES = Path(__file__).parent / "fixtures" / "bd"
VERSIONS = ["1.3.1", "1.3.2"]
# Verbs that change bd state. None may ever reach a subprocess.
MUTATING = ("create", "update", "close", "reopen", "delete", "dep", "sync", "init", "import", "claim", "label")


def fixture(version):
    return FIXTURES / f"parser-{version}.jsonl"


def rows(version):
    return [json.loads(line) for line in fixture(version).read_text().splitlines()]


def by_title(version):
    return {row["title"]: row["id"] for row in rows(version)}


def write_rows(path, items):
    path.write_text("".join(json.dumps(item) + "\n" for item in items))
    return path


def sync(target, capsys, path, version="1.3.2", *extra):
    rc = cli.main(
        ["work", "import", "bd", "--target", str(target), "--from-jsonl", str(path), "--bd-version", version]
        + ["--json", *extra]
    )
    out = capsys.readouterr().out
    return rc, json.loads(out)


def mirror(target):
    tasks = ledger._read_task_ledger(target)["tasks"]
    return {task["metadata"]["bd"]["id"]: task for task in tasks if task.get("source") == "bd"}


def ready(target, capsys):
    assert cli.main(["work", "ready", "--target", str(target), "--explain", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("version", VERSIONS)
def test_bd_export_mirrors_tasks_edges_and_ready_set(tmp_path, capsys, version):
    rc, payload = sync(tmp_path, capsys, fixture(version), version)
    assert rc == 0
    assert payload["bd_version"] == version
    assert len(payload["created"]) == 5
    assert len(payload["edges_added"]) == 4
    ids = by_title(version)
    tasks = mirror(tmp_path)
    assert tasks[ids["Design the parser"]]["status"] == "done"
    assert tasks[ids["Implement the parser"]]["type"] == "feature"
    assert tasks[ids["Implement the parser"]]["acceptance"] == ["Tests pass"]
    assert tasks[ids["Fix a crash found while parsing"]]["priority"] == "high"
    readiness = ready(tmp_path, capsys)
    ready_ids = {item["id"] for item in readiness["ready"]}
    assert ready_ids == {tasks[ids["Implement the parser"]]["id"], tasks[ids["Fix a crash found while parsing"]]["id"]}
    blocked = {item["id"]: item for item in readiness["blocked"]}
    document = tasks[ids["Document the parser"]]["id"]
    assert blocked[document]["reason"] == "open_blockers"
    assert blocked[tasks[ids["Parser epic"]]["id"]]["reason"] == "parent_not_startable"
    assert set(payload["ready"]) == ready_ids


def test_reimport_is_idempotent_and_keyed_by_bd_id(tmp_path, capsys):
    assert sync(tmp_path, capsys, fixture("1.3.2"))[0] == 0
    ledger_path = tmp_path / ".brigade/work/tasks.json"
    before = ledger_path.read_bytes()
    changed = rows("1.3.2")
    for row in changed:
        row["updated_at"] = "2026-11-04T00:00:00Z"
        row["comment_count"] = 7
    rc, payload = sync(tmp_path, capsys, write_rows(tmp_path / "again.jsonl", changed))
    assert rc == 0
    assert payload["created"] == payload["updated"] == payload["edges_added"] == []
    assert payload["unchanged"] == 5
    assert ledger_path.read_bytes() == before
    retitled = rows("1.3.2")
    retitled[0]["title"] = "Fix the parser crash"
    rc, payload = sync(tmp_path, capsys, write_rows(tmp_path / "retitled.jsonl", retitled))
    assert rc == 0
    assert [item["bd_id"] for item in payload["updated"]] == [retitled[0]["id"]]
    tasks = mirror(tmp_path)
    assert len(tasks) == 5
    assert tasks[retitled[0]["id"]]["text"].startswith(f"[bd:{retitled[0]['id']}] Fix the parser crash")


def test_claiming_honors_bd_blockers_and_release_on_close(tmp_path, capsys):
    assert sync(tmp_path, capsys, fixture("1.3.2"))[0] == 0
    ids = by_title("1.3.2")
    tasks = mirror(tmp_path)
    document = tasks[ids["Document the parser"]]["id"]
    implement = tasks[ids["Implement the parser"]]["id"]
    assert cli.main(["work", "claim", document, "--target", str(tmp_path), "--actor", "agent-a", "--json"]) != 0
    assert "open_blockers" in capsys.readouterr().out
    assert cli.main(["work", "claim", implement, "--target", str(tmp_path), "--actor", "agent-a", "--json"]) == 0
    capsys.readouterr()
    # bd closing the claimed issue does not override the live Brigade claim.
    closed = rows("1.3.2")
    for row in closed:
        if row["id"] == ids["Implement the parser"]:
            row["status"] = "closed"
    rc, payload = sync(tmp_path, capsys, write_rows(tmp_path / "closed.jsonl", closed))
    assert rc == 0
    assert payload["kept_brigade_state"][0]["brigade_status"] == "in_progress"
    assert mirror(tmp_path)[ids["Implement the parser"]]["status"] == "in_progress"
    assert cli.main(["work", "task", "done", implement, "--target", str(tmp_path)]) == 0
    capsys.readouterr()
    assert document in {item["id"] for item in ready(tmp_path, capsys)["ready"]}


def test_bd_status_and_dependency_changes_follow_unclaimed_tasks(tmp_path, capsys):
    assert sync(tmp_path, capsys, fixture("1.3.2"))[0] == 0
    ids = by_title("1.3.2")
    document = mirror(tmp_path)[ids["Document the parser"]]["id"]
    changed = rows("1.3.2")
    for row in changed:
        if row["id"] == ids["Document the parser"]:
            row.pop("dependencies")
        if row["id"] == ids["Fix a crash found while parsing"]:
            row["status"] = "in_progress"
    rc, payload = sync(tmp_path, capsys, write_rows(tmp_path / "changed.jsonl", changed))
    assert rc == 0
    assert [item["bd_id"] for item in payload["edges_removed"]] == [ids["Document the parser"]]
    assert payload["status_changed"][0]["to"] == "blocked"
    ready_ids = {item["id"] for item in ready(tmp_path, capsys)["ready"]}
    assert document in ready_ids
    assert mirror(tmp_path)[ids["Fix a crash found while parsing"]]["id"] not in ready_ids
    # Restoring the dependency re-adds it under the same owner.
    rc, payload = sync(tmp_path, capsys, fixture("1.3.2"))
    assert rc == 0
    assert [item["bd_id"] for item in payload["edges_added"]] == [ids["Document the parser"]]
    assert payload["status_changed"][0]["to"] == "pending"


def with_row(items, bd_id, **changes):
    return [{**row, **changes} if row["id"] == bd_id else row for row in items]


def test_brigade_completion_survives_a_bd_close_and_reopen(tmp_path, capsys):
    assert sync(tmp_path, capsys, fixture("1.3.2"))[0] == 0
    bd_id = by_title("1.3.2")["Implement the parser"]
    task_id = mirror(tmp_path)[bd_id]["id"]
    # Brigade finishes the task itself; the mirror never applied that status.
    assert cli.main(["work", "task", "done", task_id, "--target", str(tmp_path)]) == 0
    capsys.readouterr()
    closed = with_row(rows("1.3.2"), bd_id, status="closed")
    assert sync(tmp_path, capsys, write_rows(tmp_path / "closed.jsonl", closed))[0] == 0
    assert mirror(tmp_path)[bd_id]["status"] == "done"
    # bd catching up with the local status must not hand ownership to the mirror.
    rc, payload = sync(tmp_path, capsys, fixture("1.3.2"))
    assert rc == 0
    assert mirror(tmp_path)[bd_id]["status"] == "done"
    assert payload["kept_brigade_state"][0]["brigade_status"] == "done"
    assert task_id not in {item["id"] for item in ready(tmp_path, capsys)["ready"]}


def test_deferred_content_is_applied_once_a_closed_issue_reopens(tmp_path, capsys):
    bd_id = by_title("1.3.2")["Implement the parser"]
    closed = with_row(rows("1.3.2"), bd_id, status="closed")
    assert sync(tmp_path, capsys, write_rows(tmp_path / "closed.jsonl", closed))[0] == 0
    assert mirror(tmp_path)[bd_id]["status"] == "done"
    original = mirror(tmp_path)[bd_id]["text"]
    stale_digest = mirror(tmp_path)[bd_id]["metadata"]["bd"]["content_digest"]
    # Content edited while the issue stays closed is held, and not recorded as applied.
    edited = with_row(rows("1.3.2"), bd_id, status="closed", title="Implement the parser, v2", priority=0)
    rc, payload = sync(tmp_path, capsys, write_rows(tmp_path / "edited.jsonl", edited))
    assert rc == 0
    assert payload["kept_brigade_state"][0]["field"] == "content"
    assert mirror(tmp_path)[bd_id]["text"] == original
    assert mirror(tmp_path)[bd_id]["metadata"]["bd"]["content_digest"] == stale_digest
    # Reopening makes the task editable, so the held content lands in the same sync.
    reopened = with_row(rows("1.3.2"), bd_id, status="open", title="Implement the parser, v2", priority=0)
    rc, payload = sync(tmp_path, capsys, write_rows(tmp_path / "reopened.jsonl", reopened))
    assert rc == 0
    task = mirror(tmp_path)[bd_id]
    assert task["status"] == "pending"
    assert "Implement the parser, v2" in task["text"]
    assert task["metadata"]["bd"]["content_digest"] != stale_digest


def test_a_mirrored_reopen_clears_the_completion_time(tmp_path, capsys):
    bd_id = by_title("1.3.2")["Implement the parser"]
    closed = with_row(rows("1.3.2"), bd_id, status="closed")
    assert sync(tmp_path, capsys, write_rows(tmp_path / "closed.jsonl", closed))[0] == 0
    assert mirror(tmp_path)[bd_id].get("completed_at")
    reopened = with_row(rows("1.3.2"), bd_id, status="open")
    assert sync(tmp_path, capsys, write_rows(tmp_path / "reopened.jsonl", reopened))[0] == 0
    task = mirror(tmp_path)[bd_id]
    assert task["status"] == "pending"
    assert task.get("completed_at") is None


def test_manual_edges_survive_bd_removal(tmp_path, capsys):
    assert sync(tmp_path, capsys, fixture("1.3.2"))[0] == 0
    ids = by_title("1.3.2")
    tasks = mirror(tmp_path)
    crash, document = tasks[ids["Fix a crash found while parsing"]]["id"], tasks[ids["Document the parser"]]["id"]
    assert cli.main(["work", "task", "edge", "add", "blocks", crash, document, "--target", str(tmp_path)]) == 0
    capsys.readouterr()
    assert sync(tmp_path, capsys, fixture("1.3.2"))[0] == 0
    edges = ledger._read_task_ledger(tmp_path)["edges"]
    assert any(edge["source"] == crash and edge["target"] == document for edge in edges)


@pytest.mark.parametrize("where", ["export", "ledger"])
def test_cycles_fail_closed_without_writing(tmp_path, capsys, where):
    ids = by_title("1.3.2")
    items = rows("1.3.2")
    if where == "export":
        # Epic blocked by Document closes epic -> implement -> document -> epic.
        prerequisite, dependent = ids["Document the parser"], ids["Parser epic"]
    else:
        # Document -> Crash is a valid manual edge alone; bd then adds Crash -> Implement.
        assert sync(tmp_path, capsys, fixture("1.3.2"))[0] == 0
        tasks = mirror(tmp_path)
        manual = [
            "blocks",
            tasks[ids["Document the parser"]]["id"],
            tasks[ids["Fix a crash found while parsing"]]["id"],
        ]
        assert cli.main(["work", "task", "edge", "add", *manual, "--target", str(tmp_path)]) == 0
        capsys.readouterr()
        prerequisite, dependent = ids["Fix a crash found while parsing"], ids["Implement the parser"]
    for row in items:
        if row["id"] == dependent:
            dep = {"issue_id": dependent, "depends_on_id": prerequisite, "type": "blocks"}
            row["dependencies"] = [*row.get("dependencies", []), dep]
    ledger_path = tmp_path / ".brigade/work/tasks.json"
    before = ledger_path.read_bytes() if ledger_path.exists() else None
    rc, payload = sync(tmp_path, capsys, write_rows(tmp_path / "cycle.jsonl", items))
    assert rc == 2
    assert payload["reason"] == "dependency_cycle"
    assert (ledger_path.read_bytes() if ledger_path.exists() else None) == before


@pytest.mark.parametrize(
    "version,change,reason",
    [
        ("1.4.0", None, "unsupported_bd_schema"),
        ("2.0.0", None, "unsupported_bd_schema"),
        ("1.3", None, "invalid_bd_version"),
        ("1.3.2", ("status", "review"), "invalid_bd_row"),
        ("1.3.2", ("molecule_id", "m-1"), "invalid_bd_row"),
    ],
)
def test_unknown_schema_fails_closed(tmp_path, capsys, version, change, reason):
    items = rows("1.3.2")
    if change:
        items[0][change[0]] = change[1]
    rc, payload = sync(tmp_path, capsys, write_rows(tmp_path / "drift.jsonl", items), version)
    assert rc == 2
    assert payload["reason"] == reason
    assert payload["error"]
    assert not (tmp_path / ".brigade/work/tasks.json").exists()


def test_snapshot_inbox_import_uses_the_same_version_pin(tmp_path, capsys):
    rc = cli.main(
        ["work", "import", "--from-bd-jsonl", str(fixture("1.3.2")), "--bd-version", "1.9.0"]
        + ["--target", str(tmp_path), "--json"]
    )
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "unsupported_bd_schema"


def test_missing_bd_is_a_clear_nonzero_exit(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    rc = cli.main(["work", "import", "bd", "--target", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "bd (Beads) was not found" in err
    assert "--from-jsonl" in err
    assert not (tmp_path / ".brigade/work/tasks.json").exists()


def fake_bd(tmp_path, *, version="1.3.2", export=None):
    """A bd stand-in that logs argv, so tests run where bd is not installed."""
    log = tmp_path / "bd-argv.log"
    script = tmp_path / "bin" / "bd"
    script.parent.mkdir()
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['version', '--json']:\n"
        f"    print(json.dumps({{'version': {version!r}, 'schema_version': 1}}))\n"
        "elif sys.argv[1:] == ['export']:\n"
        f"    sys.stdout.write(open({str(export or fixture('1.3.2'))!r}).read())\n"
        "else:\n"
        "    sys.exit(9)\n"
    )
    script.chmod(0o755)
    return script, log


def test_live_bd_read_runs_only_version_and_export(tmp_path, capsys):
    script, log = fake_bd(tmp_path)
    target = tmp_path / "repo"
    target.mkdir()
    for _ in range(2):
        rc = cli.main(["work", "import", "bd", "--target", str(target), "--bd-bin", str(script), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["bd_version"] == "1.3.2"
        assert payload["origin"]["command"] == "bd export"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [["version", "--json"], ["export"]] * 2
    assert len(mirror(target)) == 5


def test_relative_bd_bin_resolves_from_the_caller_directory(tmp_path, capsys, monkeypatch):
    script, log = fake_bd(tmp_path)
    target = tmp_path / "repo"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["work", "import", "bd", "--target", str(target), "--bd-bin", "./bin/bd", "--json"])
    assert rc == 0, capsys.readouterr()
    assert [json.loads(line) for line in log.read_text().splitlines()] == [["version", "--json"], ["export"]]
    assert script.is_file()


def test_live_bd_with_unpinned_release_fails_before_export(tmp_path, capsys):
    script, log = fake_bd(tmp_path, version="1.4.0")
    rc = cli.main(["work", "import", "bd", "--target", str(tmp_path), "--bd-bin", str(script), "--json"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "unsupported_bd_schema"
    assert [json.loads(line) for line in log.read_text().splitlines()] == [["version", "--json"]]


def test_no_mutating_bd_command_can_reach_a_subprocess(tmp_path, capsys, monkeypatch):
    seen = []
    real_run = subprocess.run

    def guarded_run(argv, *args, **kwargs):
        if Path(str(argv[0])).name == "bd":
            seen.append(list(argv[1:]))
            if set(argv[1:]) & set(MUTATING) or "-o" in argv or "--output" in argv:
                raise AssertionError(f"mutating bd command reached subprocess: {argv}")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(bd_sync.subprocess, "run", guarded_run)
    script, _log = fake_bd(tmp_path)
    assert cli.main(["work", "import", "bd", "--target", str(tmp_path), "--bd-bin", str(script)]) == 0
    capsys.readouterr()
    assert seen == [["version", "--json"], ["export"]]
    for verb in MUTATING + ("export -o out.jsonl", "version"):
        with pytest.raises(BdImportError) as exc:
            bd_sync._run_bd(str(script), tuple(verb.split()), cwd=tmp_path)
        assert exc.value.reason == "bd_command_not_allowed"
    assert seen == [["version", "--json"], ["export"]]


def test_dry_run_reports_without_writing(tmp_path, capsys):
    rc, payload = sync(tmp_path, capsys, fixture("1.3.2"), "1.3.2", "--dry-run")
    assert rc == 0
    assert payload["dry_run"] is True
    assert len(payload["created"]) == 5
    assert len(payload["ready"]) == 2
    assert not (tmp_path / ".brigade").exists()


def git(target, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    identity = ["-c", "user.name=Example Tester", "-c", "user.email=tester@example.invalid"]
    subprocess.run(["git", "-C", str(target), *identity, *args], check=True, capture_output=True, env=env)


def test_outcomes_export_round_trip(tmp_path, capsys):
    target = tmp_path / "repo"
    target.mkdir()
    git(target, "init", "-q", "-b", "main")
    git(target, "remote", "add", "origin", "https://github.com/example-org/parser-demo.git")
    ids = by_title("1.3.2")
    implement_bd = ids["Implement the parser"]
    git(target, "commit", "-q", "--allow-empty", "-m", "feat: add the parser (#12)", "-m", f"Refs {implement_bd}")
    git(target, "commit", "-q", "--allow-empty", "-m", f"docs: mention {implement_bd}.1 child")
    assert sync(target, capsys, fixture("1.3.2"))[0] == 0
    implement = mirror(target)[implement_bd]["id"]
    assert cli.main(["work", "task", "done", implement, "--target", str(target)]) == 0
    capsys.readouterr()
    closeout = target / ".brigade/work/closeouts/c-1/closeout.json"
    closeout.parent.mkdir(parents=True)
    closeout.write_text(
        json.dumps(
            {
                "closeout_id": "c-1",
                "status": "ready",
                "ready": True,
                "created_at": "2026-11-03T00:00:00+00:00",
                "task": {"id": implement},
                "verification": {"run_id": "run-1", "status": "completed"},
            }
        )
    )
    output = tmp_path / "out" / "outcomes.jsonl"
    rc = cli.main(["work", "export", "bd-outcomes", "--target", str(target), "--output", str(output), "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "schema": "brigade.bd-outcomes.v1",
        "output": str(output.resolve()),
        "records": 5,
        "with_receipts": 1,
        "with_pr_links": 1,
    }
    records = {record["bd_id"]: record for record in map(json.loads, output.read_text().splitlines())}
    assert set(records) == set(ids.values())
    record = records[implement_bd]
    assert record["tasks"][0]["task_id"] == implement
    assert record["tasks"][0]["status"] == "done"
    assert record["pr_links"][0]["url"] == "https://github.com/example-org/parser-demo/pull/12"
    assert len(record["commits"]) == 1
    assert record["receipts"] == [
        {
            "kind": "work_closeout",
            "id": "c-1",
            "status": "ready",
            "ready": True,
            "created_at": "2026-11-03T00:00:00+00:00",
            "verification_run_id": "run-1",
            "verification_status": "completed",
        }
    ]
    assert records[ids["Design the parser"]]["tasks"][0]["status"] == "done"
    first = output.read_bytes()
    assert cli.main(["work", "export", "bd-outcomes", "--target", str(target), "--output", str(output)]) == 0
    capsys.readouterr()
    assert output.read_bytes() == first


def test_outcomes_export_refuses_bd_storage(tmp_path, capsys):
    rc = cli.main(
        ["work", "export", "bd-outcomes", "--target", str(tmp_path), "--output", str(tmp_path / ".beads/x.jsonl")]
        + ["--json"]
    )
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "bd_output_refused"
    assert not (tmp_path / ".beads").exists()
