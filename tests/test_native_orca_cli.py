"""Caller-mediated CLI scenarios against the Orca 1.4.269 contract."""

import json
from pathlib import Path

import pytest

from brigade import cli, native_child_custody, native_t3_enrollment, run_tasks

from tests.test_native_orca import DISPATCH, RUN, TASK, _fixture
from tests.test_native_t3_claim import TASK_TEXT, _batch, _git, _run_dir, _workspace

VERSION = "1.4.269"
SECOND_TASK = "task_000000000005"
pytestmark = pytest.mark.skipif(
    not native_child_custody.supported_platform(), reason="Native enrollment requires private custody"
)


class Coordinator:
    def __init__(self, tmp_path, capsys):
        self.home = tmp_path
        self.workspace = _workspace(tmp_path, targets=())
        self.capsys = capsys
        self.batch = _batch(tmp_path)
        roster = self.workspace / ".brigade" / "roster.toml"
        with roster.open("a") as stream:
            stream.write(
                f'\n[orca_targets.coder]\nagent = "codex"\nworktree = "new-child"\norca_version = "{VERSION}"\n'
            )
        self.run_id = ""

    def invoke(self, *args, error=None):
        rc = cli.main(["orca", *map(str, args), "--target", str(self.workspace)])
        out, err = self.capsys.readouterr()
        if error is not None:
            assert rc == 2 and not out, err
            assert f"[{error}]" in err
            assert TASK_TEXT not in err
            return err
        assert rc == 0 and not err, err
        return json.loads(out)

    def payload(self, name, data):
        path = self.home / f"{name}.json"
        path.write_text(json.dumps(data))
        return path

    def open(self):
        self.run_id = self.invoke("open", self.batch)["run_id"]
        return self.run_id

    def claim(self, task="one", orca_task=TASK, **kwargs):
        return self.invoke("claim", self.run_id, task, "--orca-run", RUN, "--orca-task", orca_task, **kwargs)

    def accept(self, payload, exit_code=0, **kwargs):
        return self.invoke(
            "accepted",
            self.run_id,
            "one",
            "--native-json",
            self.payload("start", payload),
            "--exit-code",
            exit_code,
            **kwargs,
        )

    def observe(self, show, *args, **kwargs):
        return self.invoke("observe", self.run_id, "one", "--native-json", self.payload("show", show), *args, **kwargs)

    def limit_one_worker(self):
        roster = self.workspace / ".brigade" / "roster.toml"
        roster.write_text(roster.read_text() + "\n[limits]\nmax_workers = 1\n")

    def status(self):
        return self.invoke("status", self.run_id, "--json")

    def start(self):
        grant = self.claim()
        argv = grant["argv"]
        assert argv[:5] == ["orca-ide", "orchestration", "worker-start", "--environment", "local"]
        assert argv[5:11] == ["--retry-request", grant["retry_request"], "--run", RUN, "--task", TASK]
        assert argv[-2:] == ["--json", "--full"]
        assert TASK_TEXT not in json.dumps(grant)
        receipt = _fixture("worker-start")
        receipt["result"]["mutation"]["requestId"] = grant["retry_request"]
        return receipt


@pytest.fixture
def coordinator(tmp_path, capsys):
    return Coordinator(tmp_path, capsys)


def test_orca_1_4_269_ambiguous_start_restart_and_notice(coordinator):
    coordinator.limit_one_worker()
    run_id = coordinator.open()
    receipt = coordinator.start()
    assert coordinator.open() == run_id  # new CLI invocation, same durable enrollment
    assert "already-claimed" in coordinator.claim(error="already-claimed")
    uncertain = json.loads(json.dumps(receipt))
    uncertain["result"]["state"] = "outcome_unknown"
    coordinator.accept(uncertain, 1, error="start-unknown")
    notice = json.loads((Path(__file__).parent / "fixtures/native_orca/start-unconfirmed-1.4.269.json").read_text())
    assert notice["orca_version"] == VERSION and "stablyai/orca#46155" in notice["source"]
    assert "Synthetic private text" not in coordinator.accept(notice["payload"], error="start-unknown")
    coordinator.claim("two", SECOND_TASK, error="capacity-denied")
    assert coordinator.status()["tasks"][0]["capacity_reason"] == "start-unconfirmed"
    assert not coordinator.status()["tasks"][0]["correlated"]
    remote = json.loads(json.dumps(receipt))
    remote["result"]["server"] = {"environmentId": "example"}
    coordinator.accept(remote, error="native-mismatch")
    coordinator.accept(receipt)
    assert coordinator.accept(receipt)["replayed"]
    assert coordinator.status()["tasks"][0]["correlated"]


def test_orca_1_4_269_recovers_only_the_original_worker_after_a_restart(coordinator):
    run_id = coordinator.open()
    receipt = coordinator.start()
    receipt["result"]["state"] = "outcome_unknown"
    start_file = coordinator.payload("start", receipt)
    show = _fixture("worker-show")
    show["result"]["worker"]["state"] = "start_unknown"
    show_file = coordinator.payload("show", show)
    args = ("hold-resolve", run_id, "one", "--native-json", start_file, "--exit-code", 1, "--show-json", show_file)
    coordinator.invoke(*args, error="start-unknown")
    show = _fixture("worker-show")
    show["result"]["dispatch"]["id"] = "ctx_000000000009"
    coordinator.payload("show", show)
    coordinator.invoke(*args, error="start-unknown")
    coordinator.payload("show", _fixture("worker-show"))
    recovered = coordinator.invoke(*args)
    assert recovered["mode"] == "operator-asserted" and recovered["dispatch_id"] == DISPATCH
    assert coordinator.invoke(*args)["replayed"]
    assert coordinator.claim(error="already-claimed")


def test_orca_1_4_269_circuit_breaker_never_allocates_another_launch(coordinator):
    coordinator.limit_one_worker()
    run_id = coordinator.open()
    coordinator.start()
    refusal = _fixture("task-not-startable")
    refusal["error"]["data"].update(retryOf=DISPATCH, failureCount=3)
    result = coordinator.accept(refusal, 1)
    assert result["reason"] == "circuit-broken"
    status = coordinator.status()
    assert status["capacity"]["live"] == 1
    assert not status["tasks"][0]["correlated"]
    coordinator.invoke("ack", run_id, "one", "--result-id", result["result_id"])
    assert coordinator.status()["capacity"]["live"] == 0
    coordinator.claim(error="already-claimed")
    final = coordinator.invoke("finalize", run_id)
    assert (final["status"], final["terminals"], final["acknowledged"]) == ("failed", 1, 1)


@pytest.mark.parametrize("reason", ["start-failed", "start-refused"])
def test_orca_1_4_269_definite_refusal_frees_capacity_after_ack(coordinator, reason):
    coordinator.limit_one_worker()
    run_id = coordinator.open()
    ready = coordinator.start()
    if reason == "start-failed":
        refusal = json.loads(json.dumps(ready))
        refusal["result"]["state"] = "failed"
    else:
        refusal = _fixture("task-not-startable")
    result = coordinator.accept(refusal, 1)
    assert result["status"] == "failed" and result["reason"] == reason
    result_id = result["result_id"]
    replay = coordinator.accept(refusal, 1)
    assert replay["replayed"] and replay["result_id"] == result_id
    coordinator.accept(ready, error="task-terminal")
    row = coordinator.status()["tasks"][0]
    assert row["terminal"] and row["start_refusal"] == reason and row["result_id"] == result_id
    coordinator.claim("two", SECOND_TASK, error="capacity-denied")
    coordinator.invoke("ack", run_id, "one", "--result-id", result_id)
    assert coordinator.status()["capacity"]["live"] == 0
    assert coordinator.claim("two", SECOND_TASK)["state"] == "claimed"


@pytest.mark.parametrize(
    "state",
    [
        "start_unknown",
        "outcome_unknown",
        "failed-with-residual-worker",
        "failed-with-resources",
        "failed-without-resources",
    ],
)
def test_orca_1_4_269_unknown_start_keeps_the_slot(coordinator, state):
    coordinator.limit_one_worker()
    run_id = coordinator.open()
    receipt = coordinator.start()
    if state == "failed-with-residual-worker":
        receipt["result"].update(state="failed", recovery="This start left a worker session open.")
    elif state == "failed-with-resources":
        receipt["result"].update(state="failed", residualResources=["synthetic worker resource"])
    elif state == "failed-without-resources":
        receipt["result"]["state"] = "failed"
        del receipt["result"]["residualResources"]
    else:
        receipt["result"]["state"] = state
    coordinator.accept(receipt, 1, error="start-unknown")
    row = coordinator.status()["tasks"][0]
    assert row["capacity_reason"] == "start-unconfirmed" and not row["terminal"]
    coordinator.claim("two", SECOND_TASK, error="capacity-denied")
    coordinator.invoke("finalize", run_id, error="finalize-live")


@pytest.mark.parametrize(
    ("status", "circuit"),
    [("running", False), ("dispatched", False), (None, False), ("dispatched", True), ("completed", False)],
)
def test_orca_1_4_269_not_startable_with_unproven_liveness_keeps_capacity(coordinator, status, circuit):
    coordinator.limit_one_worker()
    run_id = coordinator.open()
    coordinator.start()
    refusal = _fixture("task-not-startable")
    data = refusal["error"]["data"]
    if status is None:
        del data["status"]
    else:
        data["status"] = status
    if circuit:
        data.update(retryOf=DISPATCH, failureCount=3)
    coordinator.accept(refusal, 1, error="start-unknown")
    assert coordinator.status()["capacity"]["live"] == 1
    coordinator.invoke("ack", run_id, "one", "--result-id", "a" * 64, error="task-nonterminal")
    coordinator.claim("two", SECOND_TASK, error="capacity-denied")


@pytest.mark.parametrize("status", ["running", "failed"])
def test_orca_1_4_269_not_startable_after_unknown_start_keeps_capacity(coordinator, status):
    coordinator.limit_one_worker()
    run_id = coordinator.open()
    unknown = coordinator.start()
    unknown["result"]["state"] = "outcome_unknown"
    coordinator.accept(unknown, 1, error="start-unknown")

    refusal = _fixture("task-not-startable")
    refusal["error"]["data"]["status"] = status
    coordinator.accept(refusal, 1, error="start-unknown")
    assert coordinator.status()["capacity"]["live"] == 1
    coordinator.claim("two", SECOND_TASK, error="capacity-denied")
    coordinator.invoke("finalize", run_id, error="finalize-live")


@pytest.mark.parametrize("state", ["ready", "failed"])
def test_orca_1_4_269_full_start_receipt_requires_run_id(coordinator, state):
    coordinator.limit_one_worker()
    coordinator.open()
    receipt = coordinator.start()
    receipt["result"]["state"] = state
    del receipt["result"]["runId"]
    coordinator.accept(receipt, int(state == "failed"), error="native-mismatch")
    coordinator.claim("two", SECOND_TASK, error="capacity-denied")


@pytest.mark.parametrize(("field", "wrong_value"), [("taskId", SECOND_TASK), ("runId", "run_000000000009")])
def test_orca_1_4_269_refusal_for_another_identity_keeps_the_slot(coordinator, field, wrong_value):
    coordinator.limit_one_worker()
    coordinator.open()
    coordinator.start()
    refusal = _fixture("task-not-startable")
    refusal["error"]["data"][field] = wrong_value
    coordinator.accept(refusal, 1, error="native-mismatch")
    coordinator.claim("two", SECOND_TASK, error="capacity-denied")


def test_orca_1_4_269_refusal_replay_repairs_an_interrupted_result(coordinator, monkeypatch):
    coordinator.limit_one_worker()
    run_id = coordinator.open()
    ready = coordinator.start()
    refusal = _fixture("task-not-startable")

    def interrupted(*args, **kwargs):
        raise run_tasks.TaskJournalError("simulated interrupted observation")

    with monkeypatch.context() as patch:
        patch.setattr(run_tasks, "observe_task", interrupted)
        coordinator.accept(refusal, 1, error="observation-conflict")

    row = coordinator.status()["tasks"][0]
    assert row["start_refusal"] == "start-refused" and row["result_id"] is None
    coordinator.accept(ready, error="task-terminal")
    coordinator.claim("two", SECOND_TASK, error="capacity-denied")
    with pytest.raises(SystemExit) as exc:
        cli.main(["orca", "accepted", "--help"])
    assert exc.value.code == 0
    assert "matching refusal" in " ".join(coordinator.capsys.readouterr().out.split())
    repaired = coordinator.accept(refusal, 1)
    coordinator.invoke("ack", run_id, "one", "--result-id", repaired["result_id"])
    assert coordinator.status()["capacity"]["live"] == 0


def test_orca_1_4_269_human_status_explains_the_refusal_and_capacity(coordinator, capsys):
    coordinator.limit_one_worker()
    run_id = coordinator.open()
    coordinator.start()
    coordinator.accept(_fixture("task-not-startable"), 1)
    assert cli.main(["orca", "status", run_id, "--target", str(coordinator.workspace)]) == 0
    out, err = capsys.readouterr()
    assert not err
    lines = out.splitlines()
    assert lines[0].startswith(run_id) and lines[0].endswith("(workspace workers 1/1)")
    assert lines[1] == "  one failed (start-refused) (holds capacity: awaiting-ack)"


def test_orca_1_4_269_partial_completion_retained_tab_and_cleanup(coordinator):
    run_id = coordinator.open()
    coordinator.accept(coordinator.start())
    structured = _fixture("worker-show-structured")
    partial = coordinator.observe(structured)
    assert partial["result_id"] is None and partial["reason"] == "result-transfer-missing"
    coordinator.invoke("ack", run_id, "one", "--result-id", "a" * 64, error="task-nonterminal")
    inbox = partial["inbox_argv"]
    assert "inbox" in inbox and "--full" in inbox
    assert inbox[inbox.index("--limit") + 1] == "100"
    unreadable = _fixture("worker-show-structured")
    unreadable["result"]["projection"]["liveness"]["verdict"] = "unknown"
    mail = coordinator.payload("inbox", _fixture("worker-done"))
    transcript = coordinator.payload("transcript", _fixture("worker-read"))
    assert (
        coordinator.observe(unreadable, "--worker-done-json", mail, "--transcript-json", transcript)["result_id"]
        is None
    )
    full = coordinator.observe(
        structured,
        "--worker-done-json",
        mail,
        "--transcript-json",
        transcript,
    )
    result_id = full["result_id"]
    assert result_id
    coordinator.invoke("ack", run_id, "one", "--result-id", result_id)
    row = coordinator.status()["tasks"][0]
    assert row["capacity_reason"] == "structured-tab-open"
    assert row["terminal"] and row["acked"] and row["holds_capacity"]
    coordinator.invoke("finalize", run_id, error="finalize-live")
    released = coordinator.observe(_fixture("worker-show-released"))
    assert released["result_id"] == result_id
    assert not coordinator.status()["tasks"][0]["holds_capacity"]
    report = coordinator.invoke("finalize", run_id)
    assert report["terminals"] == 1
    saved = _run_dir(coordinator.workspace, run_id) / "events" / "lifecycle.jsonl"
    data = saved.read_text()
    for private in (TASK_TEXT, "synthetic worker text", "synthetic transcript text", "structworker_example"):
        assert private not in data
    assert coordinator.invoke("ack", run_id, "one", "--result-id", result_id)["replayed"]


def test_orca_1_4_269_cancel_requires_exact_stop_and_exit(coordinator):
    run_id = coordinator.open()
    coordinator.accept(coordinator.start())
    request = coordinator.invoke("cancel", run_id, "one")
    assert request["argv"][2] == "worker-stop"
    stop = _fixture("worker-stop")
    stop["result"]["mutation"]["requestId"] = request["argv"][8]
    show = _fixture("worker-show-stopped")
    stop_file = coordinator.payload("stop", stop)
    args = ("cancel", run_id, "one", "--native-json", stop_file, "--show-json", coordinator.payload("show", show))
    stop["result"]["state"] = "stop_unknown"
    coordinator.payload("stop", stop)
    assert coordinator.invoke(*args, "--exit-code", 1)["result_id"] is None
    stop["result"]["state"] = "stopped"
    coordinator.payload("stop", stop)
    show["result"]["projection"]["liveness"]["verdict"] = "unknown"
    coordinator.payload("show", show)
    assert coordinator.invoke(*args, "--exit-code", 0)["result_id"] is None
    coordinator.payload("show", _fixture("worker-show-stopped"))
    result = coordinator.invoke(*args, "--exit-code", 0)
    assert result["status"] == "cancelled" and result["result_id"]
    assert coordinator.invoke(*args, "--exit-code", 0)["result_id"] == result["result_id"]


def test_orca_1_4_269_budget_and_abandoned_capacity(coordinator):
    run_id = coordinator.open()
    path = _run_dir(coordinator.workspace, run_id) / "run.json"
    meta = json.loads(path.read_text())
    meta["run_budget"] = {
        "schema": "brigade.run_budget.v1",
        "schema_version": 1,
        "ceilings": {"wall_clock_seconds": None, "worker_dispatch_count": 1},
    }
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    coordinator.start()
    coordinator.claim("two", SECOND_TASK, error="budget-exhausted")
    assert coordinator.invoke("hold-resolve", run_id, "one", "--abandon")["mode"] == "abandoned-unknown"
    coordinator.invoke("finalize", run_id, error="finalize-abandoned")
    row = coordinator.status()["tasks"][0]
    assert row["capacity_reason"] == "abandoned-unknown" and not row["terminal"]


def test_orca_1_4_269_unsupported_hands_off_only_at_admission(coordinator):
    roster = coordinator.workspace / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text().replace(VERSION, "1.4.265"))
    err = coordinator.invoke("open", coordinator.batch, error="orca-unsupported")
    assert err.splitlines()[:2] == [
        "error [orca-unsupported]: task one version-unsupported",
        "error [orca-unsupported]: task two version-unsupported",
    ]
    assert "brigade run batch" in err
    assert not (coordinator.workspace / ".brigade" / "runs").exists()


def test_orca_1_4_269_cleanup_from_a_coordinator_at_a_new_head(coordinator):
    run_id = coordinator.open()
    coordinator.accept(coordinator.start())
    _git(coordinator.workspace, "commit", "--allow-empty", "-m", "advance")
    coordinator.claim("two", SECOND_TASK, error="source-changed")
    result = coordinator.observe(_fixture("worker-show-structured"))
    assert result["result_id"] is None
    assert coordinator.status()["run_id"] == run_id


@pytest.mark.parametrize(
    "fixture, state, reason",
    [
        ("worker-show-kept-open", None, "kept-open"),
        ("worker-show", "abandoned", "abandoned-unknown"),
    ],
)
def test_orca_1_4_269_status_explains_unreleased_workers(coordinator, fixture, state, reason):
    coordinator.open()
    coordinator.accept(coordinator.start())
    show = _fixture(fixture)
    if state is not None:
        show["result"]["worker"]["state"] = state
    assert coordinator.observe(show)["result_id"] is None
    row = coordinator.status()["tasks"][0]
    assert row["holds_capacity"] and row["capacity_reason"] == reason


def test_orca_1_4_269_workspace_capacity_includes_t3_workers(coordinator):
    from tests.test_native_t3_claim import _claim

    roster = coordinator.workspace / ".brigade/roster.toml"
    text = roster.read_text().replace("[agents.lead]", "[limits]\nmax_workers = 1\n[agents.lead]")
    roster.write_text(
        text
        + """
[agents.other]
cli = "codex"
role = "coder"
[t3_targets.other]
origin_environment = "env-target"
origin_thread = "thread-parent"
target_environment = "env-target"
target_project = "project-test"
provider_instance = "codex-test"
driver = "codex"
model = "launch-model"
isolation = "worktree"
""",
        encoding="utf-8",
    )
    t3_batch = coordinator.home / "t3-batch.json"
    batch = json.loads(coordinator.batch.read_text())
    batch["request_id"] = "t3-request"
    for task in batch["tasks"]:
        task["seat"] = "other"
    t3_batch.write_text(json.dumps(batch))
    t3_run = native_t3_enrollment.enroll(batch_path=t3_batch, target=coordinator.workspace)["run_id"]
    _claim(coordinator.workspace, t3_run, "one")
    coordinator.open()
    assert coordinator.status()["capacity"] == {"live": 1, "max_workers": 1, "diagnostic": None}
    coordinator.claim(error="capacity-denied")
