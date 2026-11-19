"""Offline contract and CLI checks for explicit batch dry runs."""

import copy
import hashlib
import json
from unittest.mock import Mock

import pytest

from brigade import aboyeur, agents, cli, run_batch


def task(task_id="a", **kwargs):
    return {"task_id": task_id, "text": "Inspect the example", "seat": "coder", **kwargs}


def batch(*tasks, **kwargs):
    return {"schema": run_batch.SCHEMA, "request_id": "request-1", "tasks": list(tasks or [task()]), **kwargs}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], "invalid-payload"),
        (batch(schema="brigade.run_batch.v2"), "unsupported-schema"),
        (batch(schema=None), "unsupported-schema"),
        (batch(request_id=""), "invalid-request-id"),
        (batch(tasks=[]), "task-count"),
        (batch(tasks=[task(str(i)) for i in range(9)]), "task-count"),
        (batch(tasks=[{"text": "x", "seat": "coder"}]), "invalid-task-id"),
        (batch(task("bad/id")), "invalid-task-id"),
        (batch(task(), task()), "duplicate-task-id"),
        (batch(task(text=" \n")), "empty-text"),
        (batch(task(text="x" * (run_batch.MAX_TEXT_BYTES + 1))), "text-limit"),
        (batch(task(seat="")), "invalid-seat"),
        (batch(task(isolation="container")), "invalid-isolation"),
        (batch(task(isolation=None)), "invalid-isolation"),
        (batch(task(read_only="yes")), "invalid-read-only"),
        (batch(task(read_only=1)), "invalid-read-only"),
        (batch(task(depends_on="b")), "invalid-dependencies"),
        (batch(task(depends_on=["b", "b"])), "invalid-dependencies"),
        (batch(task(depends_on=["missing"])), "unknown-dependency"),
        (batch(task(depends_on=["a"])), "self-dependency"),
        (batch(task("a", depends_on=["b"]), task("b", depends_on=["a"])), "dependency-cycle"),
        (batch(synthesis=True), "invalid-synthesis"),
        (batch(synthesis={"seat": "coder", "text": ""}), "empty-text"),
        (batch(extra=True), "invalid-payload"),
        (batch(task(extra=True)), "invalid-payload"),
    ],
)
def test_validation_errors(payload, code):
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.validate_batch(payload)
    assert caught.value.code == code


def test_cycle_reports_actual_closed_path():
    with pytest.raises(run_batch.BatchError, match="a -> b -> c -> a"):
        run_batch.validate_batch(
            batch(task("a", depends_on=["b"]), task("b", depends_on=["c"]), task("c", depends_on=["a"]))
        )


def test_same_text_and_seat_remain_distinct_and_layers_are_stable():
    payload = batch(task("c", depends_on=["b", "a"]), task("b", read_only=True), task("a"))
    plan = run_batch.plan_batch(payload)
    assert plan["layers"] == [["a", "b"], ["c"]]
    assert [t["task_id"] for t in plan["tasks"]] == ["a", "b", "c"]
    assert [t["seat"] for t in plan["tasks"]] == ["coder"] * 3
    assert plan["synthesis"] is None
    assert plan == run_batch.plan_batch(batch(*reversed(payload["tasks"])))


def test_fingerprint_and_retry_comparison():
    payload = batch(task())
    reordered = json.loads(json.dumps(payload, sort_keys=True))
    fingerprint = run_batch.request_fingerprint(payload)
    assert fingerprint == run_batch.request_fingerprint(reordered)
    assert run_batch.compare_request(None, payload) == "new"
    assert run_batch.compare_request(fingerprint, reordered) == "same"
    changed = copy.deepcopy(payload)
    changed["tasks"][0]["text"] += " again"
    assert run_batch.compare_request(fingerprint, changed) == "conflict"
    assert run_batch.request_fingerprint(batch(synthesis={"seat": "coder", "text": "Summarize results"})) != fingerprint


@pytest.fixture
def no_models(monkeypatch):
    planner = Mock(side_effect=AssertionError("planner called"))
    model = Mock(side_effect=AssertionError("model called"))
    runner = Mock(side_effect=AssertionError("run called"))
    monkeypatch.setattr(aboyeur, "plan", planner)
    monkeypatch.setattr(agents, "run_agent", model)
    monkeypatch.setattr(aboyeur, "run", runner)
    yield
    planner.assert_not_called()
    model.assert_not_called()
    runner.assert_not_called()


@pytest.mark.parametrize("as_json", [False, True])
def test_dry_run_cli(tmp_path, capsys, no_models, as_json):
    payload = batch(task("b", read_only=True), task("a"))
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(payload))
    args = ["run", "batch", str(path), "--dry-run"] + (["--json"] if as_json else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if as_json:
        plan = json.loads(output)
        assert plan["layers"] == [["a", "b"]]
        assert plan["fingerprint"] == run_batch.request_fingerprint(payload)
    else:
        assert "a (coder)" in output
        assert "fingerprint:" in output
    assert not (tmp_path / ".brigade").exists()


def test_synthesis_execution_refused(tmp_path, capsys, no_models):
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch(synthesis={"seat": "coder", "text": "Summarize"})))
    assert cli.main(["run", "batch", str(path)]) == 2
    assert "synthesis-not-implemented" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw,code",
    [(b"{", "invalid-json"), (b"\xff", "invalid-json"), (b" " * (run_batch.MAX_INPUT_BYTES + 1), "input-limit")],
)
def test_file_errors(tmp_path, capsys, no_models, raw, code):
    path = tmp_path / "batch.json"
    path.write_bytes(raw)
    assert cli.main(["run", "batch", str(path), "--dry-run", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == code


def test_missing_file(tmp_path, capsys, no_models):
    assert cli.main(["run", "batch", str(tmp_path / "missing"), "--dry-run"]) == 2
    assert "input-read" in capsys.readouterr().err


def test_bounds_and_lifecycle_identity():
    from brigade import run_task_journal, run_tasks

    payload = batch(
        *[
            task(f"task:{i}", text="é" * (run_batch.MAX_TEXT_BYTES // 2), read_only=True)
            for i in range(run_batch.MAX_TASKS)
        ]
    )
    canonical = run_batch.validate_batch(payload)
    assert len(canonical["tasks"]) == run_task_journal.MAX_BATCH_MEMBERS
    for row in canonical["tasks"]:
        spec = run_tasks.TaskSpec(row["task_id"], row["seat"], "0" * 64)
        assert spec.task_id == row["task_id"]
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.validate_batch(batch(task(text="é" * (run_batch.MAX_TEXT_BYTES // 2 + 1))))
    assert caught.value.code == "text-limit"


def test_canonical_defaults_and_dependencies():
    payload = batch(task("a"), task("b", read_only=True), task("c", depends_on=["b", "a"]))
    changed = copy.deepcopy(payload)
    changed["synthesis"] = None
    changed["tasks"][0]["depends_on"] = []
    changed["tasks"][2]["depends_on"].reverse()
    assert run_batch.request_fingerprint(payload) == run_batch.request_fingerprint(changed)


SHA = "a" * 40


def test_plan_records_effective_isolation_and_read_only():
    plan = run_batch.plan_batch(batch(task("a"), task("b", read_only=True)))
    assert [(row["isolation"], row["read_only"]) for row in plan["tasks"]] == [
        ("shared", False),
        ("shared", True),
    ]
    assert plan["base_sha"] is None


def test_non_default_isolation_and_read_only_change_the_fingerprint():
    default = run_batch.request_fingerprint(batch(task("a")))
    assert run_batch.request_fingerprint(batch(task("a", read_only=True))) != default
    worktree = batch(task("a", isolation="worktree"))
    assert run_batch.request_fingerprint(worktree, base_sha=SHA) != default


def test_same_layer_shared_writable_tasks_are_refused_with_task_ids():
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.validate_batch(batch(task("b"), task("a")))
    assert caught.value.code == "shared-writable-concurrency"
    assert "a, b" in str(caught.value)


def test_documented_concurrency_escapes_are_accepted():
    assert run_batch.validate_batch(batch(task("a")))["tasks"][0]["isolation"] == "shared"
    layered = run_batch.validate_batch(batch(task("a"), task("b", depends_on=["a"])))
    assert [row["task_id"] for row in layered["tasks"]] == ["a", "b"]
    run_batch.validate_batch(batch(task("a"), task("b", read_only=True)))
    run_batch.plan_batch(batch(task("a", isolation="worktree"), task("b", isolation="worktree")), base_sha=SHA)


def test_worktree_plan_requires_a_valid_pinned_base():
    payload = batch(task("a", isolation="worktree"))
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.plan_batch(payload)
    assert caught.value.code == "base-unpinned"
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.plan_batch(payload, base_sha="A" * 40)
    assert caught.value.code == "invalid-base-sha"
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.plan_batch(payload, base_sha=SHA[:-1])
    assert caught.value.code == "invalid-base-sha"
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.plan_batch(batch(task("a")), base_sha=SHA)
    assert caught.value.code == "unexpected-base-sha"
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.request_fingerprint(payload)
    assert caught.value.code == "base-unpinned"


def test_retry_against_a_different_head_is_a_conflict():
    payload = batch(task("a", isolation="worktree"))
    stored = run_batch.request_fingerprint(payload, base_sha=SHA)
    assert run_batch.compare_request(stored, payload, base_sha=SHA) == "same"
    assert run_batch.compare_request(stored, payload, base_sha="b" * 40) == "conflict"


def _git_checkout(path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("as_json", [False, True])
def test_worktree_dry_run_pins_a_clean_head(tmp_path, capsys, no_models, as_json):
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _git_checkout(repo)
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch(task("a", isolation="worktree"), task("b", read_only=True))))
    argv = ["run", "batch", str(path), "--dry-run", "--cwd", str(repo)] + (["--json"] if as_json else [])
    assert cli.main(argv) == 0
    output = capsys.readouterr().out
    if as_json:
        plan = json.loads(output)
        assert plan["base_sha"] == sha
        assert [row["isolation"] for row in plan["tasks"]] == ["worktree", "shared"]
        assert [row["read_only"] for row in plan["tasks"]] == [False, True]
    else:
        assert f"base: {sha}" in output


def test_worktree_dry_run_refuses_a_dirty_checkout(tmp_path, capsys, no_models):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_checkout(repo)
    (repo / "uncommitted.txt").write_text("local edit\n")
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch(task("a", isolation="worktree"))))
    assert cli.main(["run", "batch", str(path), "--dry-run", "--cwd", str(repo), "--json"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "base-dirty"
    assert "uncommitted" in error["message"]


def test_worktree_dry_run_refuses_a_checkout_without_a_revision(tmp_path, capsys, no_models):
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch(task("a", isolation="worktree"))))
    assert cli.main(["run", "batch", str(path), "--dry-run", "--cwd", str(tmp_path), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "base-unavailable"


def test_worktree_batch_is_accepted_and_pinned_before_run_preflight(tmp_path, capsys, monkeypatch):
    from brigade import run_batch_workspace, run_tasks
    from tests._home import set_home

    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _git_checkout(repo)
    # Brigade state is never part of the pinned base, so the roster stays untracked.
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "roster.toml").write_text(
        'orchestrator = "chef"\n[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.coder]\ncli = "codex"\nrole = "implement"\n'
    )
    # The launch point refuses this task after acceptance: its path is already taken.
    cache = run_batch_workspace.cache_root()
    cache.mkdir(parents=True)
    taken = cache / "taken"
    taken.mkdir()
    monkeypatch.setattr(run_batch_workspace, "expected_path", lambda repo_root, run_id, task_id: taken)
    launched = Mock(side_effect=AssertionError("worker launched"))
    monkeypatch.setattr(agents, "run_agent", launched)

    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch(task("a", isolation="worktree"))))
    assert cli.main(["run", "batch", str(path), "--cwd", str(repo), "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    # Acceptance and the pinned base are durable before the run preflight, not an
    # artifact of the exit code: the request marker and the journal both hold them.
    run_dir = next((repo / ".brigade" / "runs").glob("batch-*"))
    marker = json.loads((run_dir / "batch-request.json").read_text())
    assert marker["base_sha"] == sha
    assert marker["tasks"][0]["isolation"] == "worktree"
    assert [(s.handle.spec.task_id, s.status, s.handle.attempt_id) for s in run_tasks.read_tasks(run_dir)] == [
        ("a", "queued", None)
    ]
    assert result["tasks"]["a"]["refusal"] == {"code": run_batch_workspace.WORKSPACE_EXISTS, "reason": "path"}
    launched.assert_not_called()
    assert sorted(p.name for p in cache.iterdir()) == ["taken"]


@pytest.mark.parametrize("raw", [b'{"schema":"x","schema":"y"}', b"[" * 2000])
def test_ambiguous_or_nested_json(tmp_path, capsys, no_models, raw):
    path = tmp_path / "batch.json"
    path.write_bytes(raw)
    assert cli.main(["run", "batch", str(path), "--dry-run", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid-json"


def test_validation_cli_and_synthesis(tmp_path, capsys, no_models):
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch(task(depends_on=["missing"]))))
    assert cli.main(["run", "batch", str(path), "--dry-run", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unknown-dependency"
    path.write_text(json.dumps(batch(synthesis={"text": "Summarize findings", "seat": "writer"})))
    assert cli.main(["run", "batch", str(path), "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["synthesis"] == {"seat": "writer"}
    assert cli.main(["run", "batch", str(path), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "synthesis-not-implemented"


def test_fingerprint_golden_vector():
    """Pins the canonical bytes so another implementation, or a json.dumps change, can be checked."""
    payload = {
        "schema": run_batch.SCHEMA,
        "request_id": "request-1",
        "tasks": [
            {"task_id": "b", "text": "café \U0001f600\n", "seat": "coder", "depends_on": ["a"]},
            {"task_id": "a", "text": "Inspect", "seat": "coder"},
        ],
    }
    canonical = (
        '{"request_id":"request-1","schema":"brigade.run_batch.v1","synthesis":null,"tasks":'
        '[{"depends_on":[],"seat":"coder","task_id":"a","text":"Inspect"},'
        '{"depends_on":["a"],"seat":"coder","task_id":"b","text":"caf\\u00e9 \\ud83d\\ude00\\n"}]}'
    )
    digest = "d69a5ad741c986a250fa6e9d2089a2550a5dc334de268ec9ee94dc76d53e1d48"
    assert hashlib.sha256(canonical.encode("ascii")).hexdigest() == digest
    assert run_batch.request_fingerprint(payload) == digest


@pytest.mark.parametrize("stored", ["ABC", "0" * 63, "G" * 64, "A" * 64, 7])
def test_compare_request_refuses_malformed_stored_fingerprint(stored):
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.compare_request(stored, batch())
    assert caught.value.code == "invalid-fingerprint"


def test_row_errors_name_the_row():
    with pytest.raises(run_batch.BatchError, match=r"tasks\[1\]"):
        run_batch.validate_batch(batch(task("a"), "not-an-object"))
    with pytest.raises(run_batch.BatchError, match=r"tasks\[1\]\.task_id"):
        run_batch.validate_batch(batch(task("a"), task("bad/id")))
