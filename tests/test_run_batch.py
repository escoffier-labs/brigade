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
    payload = batch(task("c", depends_on=["b", "a"]), task("b"), task("a"))
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
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch(task("b"), task("a"))))
    args = ["run", "batch", str(path), "--dry-run"] + (["--json"] if as_json else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if as_json:
        plan = json.loads(output)
        assert plan["layers"] == [["a", "b"]]
        assert plan["fingerprint"] == run_batch.request_fingerprint(batch(task("b"), task("a")))
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
        *[task(f"task:{i}", text="é" * (run_batch.MAX_TEXT_BYTES // 2)) for i in range(run_batch.MAX_TASKS)]
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
    payload = batch(task("a"), task("b"), task("c", depends_on=["b", "a"]))
    changed = copy.deepcopy(payload)
    changed["synthesis"] = None
    changed["tasks"][0]["depends_on"] = []
    changed["tasks"][2]["depends_on"].reverse()
    assert run_batch.request_fingerprint(payload) == run_batch.request_fingerprint(changed)


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
