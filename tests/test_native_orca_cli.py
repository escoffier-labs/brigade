"""Caller-mediated CLI scenarios against the Orca 1.4.269 contract."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier, Event

import pytest

from brigade import cli, native_child_custody, native_child_journal, native_orca_verbs, native_t3_enrollment, run_tasks
from brigade import runguard

from tests.test_native_orca import DISPATCH, RUN, TASK, _fixture
from tests.test_native_t3_claim import TASK_TEXT, _batch, _events, _git, _run_dir, _workspace

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


@pytest.mark.parametrize("verb", ["accepted", "hold-resolve"])
@pytest.mark.parametrize(
    ("row", "field", "value", "error"),
    [
        (None, None, None, None),
        ("terminalResource", "releaseState", "retained", "start-unknown"),
        ("terminalResource", "ownershipState", None, "start-unknown"),
        ("dispatch", "id", "ctx_000000000009", "native-mismatch"),
        ("worker", "dispatchId", "ctx_000000000009", "native-mismatch"),
        ("projection", "liveness", {"verdict": "unknown"}, "start-unknown"),
        ("projection", "liveness", None, "start-unknown"),
        ("observation", "status", "live", "start-unknown"),
        ("dispatch", "status", "circuit_broken", "circuit-broken"),
        ("dispatch", "hostScope", {"kind": "remote"}, "remote-mismatch"),
        ("terminalResource", "terminalHandle", "term_other", "start-unknown"),
        ("terminalResource", "terminalHandle", None, "start-unknown"),
        ("terminalResource", "worktreeId", "worktree_other", "start-unknown"),
        ("terminalResource", "worktreeId", None, "start-unknown"),
        ("result", "terminalResource", None, "start-unknown"),
        ("residual", None, {"kind": "terminal", "id": "term_other"}, "start-unknown"),
        ("residual", None, {"kind": "helper", "id": "term_example"}, "start-unknown"),
        ("residual", None, {"kind": "terminal"}, "start-unknown"),
        ("residual", None, {"kind": "worktree", "id": 42}, "start-unknown"),
        ("residual", None, "term_example", "start-unknown"),
        ("exit_code", None, 0, "start-unknown"),
    ],
)
def test_orca_1_4_271_recovers_failed_start_only_after_release_and_exit(coordinator, verb, row, field, value, error):
    coordinator.limit_one_worker()
    roster = coordinator.workspace / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text().replace(VERSION, "1.4.271"))
    run_id = coordinator.open()
    receipt = _recovery_fixture("start-failed-residual")
    receipt["result"]["mutation"]["requestId"] = coordinator.claim()["retry_request"]
    coordinator.accept(receipt, 1, error="start-unknown")
    coordinator.invoke("finalize", run_id, error="finalize-live")

    show = _recovery_fixture("worker-show-released-exited")
    exit_code = 1
    if row == "exit_code":
        exit_code = value
    elif row == "residual":
        receipt["result"]["residualResources"].append(value)
    elif row is not None:
        target = show["result"] if row == "result" else show["result"][row]
        if value is None:
            del target[field]
        else:
            target[field] = value
    if error in ("circuit-broken", "remote-mismatch"):
        show["result"]["dispatch"]["id"] = "ctx_000000000009"
    args = (
        verb,
        run_id,
        "one",
        "--native-json",
        coordinator.payload("start", receipt),
        "--exit-code",
        exit_code,
        "--show-json",
        coordinator.payload("show", show),
    )
    result = coordinator.invoke(*args, error=error)
    if error is not None:
        assert coordinator.status()["capacity"]["live"] == 1
        coordinator.invoke("finalize", run_id, error="finalize-live")
        return

    assert (result["mode"], result["reason"], result["status"]) == ("start-refusal", "start-failed", "failed")
    assert result["worktree_retained"] is True
    assert not coordinator.status()["tasks"][0]["correlated"]
    replay = coordinator.invoke(*args)
    assert replay["replayed"] and replay["result_id"] == result["result_id"]
    assert replay["worktree_retained"] is True
    coordinator.invoke("ack", run_id, "one", "--result-id", result["result_id"])
    assert coordinator.status()["capacity"]["live"] == 0
    final = coordinator.invoke("finalize", run_id)
    assert (final["status"], final["terminals"], final["acknowledged"]) == ("failed", 1, 1)


def _recovery_fixture(name):
    path = Path(__file__).parent / "fixtures" / "native_orca" / f"{name}-1.4.271.json"
    return json.loads(path.read_text())["payload"]


@pytest.mark.parametrize("kind", ["worktree", "terminal", "empty", "empty-with-recovery"])
def test_failed_start_retained_worktree_in_status_after_ack(coordinator, kind):
    run_id = coordinator.open()
    ready = coordinator.start()
    receipt = _recovery_fixture("start-failed-residual")
    receipt["result"]["mutation"] = ready["result"]["mutation"]
    receipt["result"]["residualResources"] = [
        entry for entry in receipt["result"]["residualResources"] if entry["kind"] == kind
    ]
    show = _recovery_fixture("worker-show-released-exited")
    if kind == "empty":
        del receipt["result"]["recovery"]
        show["result"]["dispatch"]["status"] = "circuit_broken"
    result = coordinator.invoke(
        "hold-resolve",
        run_id,
        "one",
        "--native-json",
        coordinator.payload("start", receipt),
        "--exit-code",
        1,
        "--show-json",
        coordinator.payload("show", show),
        error="start-unknown" if kind == "empty-with-recovery" else None,
    )
    if kind == "empty-with-recovery":
        assert "worktree_retained" not in coordinator.status()["tasks"][0]
        return
    assert result["reason"] == "start-failed"
    retained = kind == "worktree"
    assert result.get("worktree_retained", False) is retained
    coordinator.invoke("ack", run_id, "one", "--result-id", result["result_id"])
    status = coordinator.status()
    assert status["capacity"]["live"] == 0
    assert status["tasks"][0]["acked"]
    assert status["tasks"][0]["worktree_retained"] is retained
    fact = native_child_journal.latest(_events(coordinator.workspace, run_id), "task.native-capacity", "one")
    assert fact is not None and set(fact) == {"task_id", "attempt_id", "reason", "worktree_retained"}
    assert fact["worktree_retained"] is retained
    if retained:
        rc = cli.main(["orca", "status", run_id, "--target", str(coordinator.workspace)])
        out, err = coordinator.capsys.readouterr()
        assert rc == 0 and not err
        assert "(worktree retained)" in out


@pytest.mark.parametrize("verb", ["accepted", "hold-resolve"])
@pytest.mark.parametrize("reason", ["start-refused", "start-failed", "circuit-broken"])
def test_definite_start_refusal_with_show_option(coordinator, verb, reason):
    run_id = coordinator.open()
    receipt = coordinator.start()
    if reason == "start-failed":
        receipt["result"]["state"] = "failed"
    else:
        receipt = _fixture("task-not-startable")
        if reason == "circuit-broken":
            receipt["error"]["data"].update(retryOf=DISPATCH, failureCount=3)
    show = _recovery_fixture("worker-show-released-exited") if verb == "accepted" else None
    result = coordinator.invoke(
        verb,
        run_id,
        "one",
        "--native-json",
        coordinator.payload("start", receipt),
        "--exit-code",
        1,
        "--show-json",
        coordinator.payload("show", show),
    )
    assert result["reason"] == reason and result["status"] == "failed"


def test_accepted_with_show_rejects_outcome_unknown_without_journaling(coordinator):
    run_id = coordinator.open()
    receipt = coordinator.start()
    receipt["result"]["state"] = "outcome_unknown"
    before = _events(coordinator.workspace, run_id)
    coordinator.invoke(
        "accepted",
        run_id,
        "one",
        "--native-json",
        coordinator.payload("start", receipt),
        "--exit-code",
        1,
        "--show-json",
        coordinator.payload("show", _fixture("worker-show")),
        error="start-unknown",
    )
    assert _events(coordinator.workspace, run_id) == before


def test_failed_start_recovery_closes_lost_dispatch_request(coordinator, monkeypatch):
    run_id = coordinator.open()

    def interrupted(*args, **kwargs):
        raise native_child_journal.refuse("journal-refused", "synthetic interruption before dispatch request")

    with monkeypatch.context() as patch:
        patch.setattr(native_child_journal, "record_dispatch_requested", interrupted)
        coordinator.claim(error="journal-refused")
    assert not native_child_journal.requested_attempts(_events(coordinator.workspace, run_id))
    receipt = _recovery_fixture("start-failed-residual")
    with native_orca_verbs.claimed_task(coordinator.workspace, run_id, "one", resolving=True) as task:
        receipt["result"]["mutation"]["requestId"] = task.binding.checked_key()
    result = coordinator.invoke(
        "hold-resolve",
        run_id,
        "one",
        "--native-json",
        coordinator.payload("start", receipt),
        "--exit-code",
        1,
        "--show-json",
        coordinator.payload("show", _recovery_fixture("worker-show-released-exited")),
    )
    assert result["reason"] == "start-failed"
    assert native_child_journal.requested_attempts(_events(coordinator.workspace, run_id)) == {("coder", 1)}


@pytest.mark.parametrize("hold", ["accept-conflict", "abandoned"])
def test_failed_start_recovery_checks_holds_before_closing_request(coordinator, monkeypatch, hold):
    run_id = coordinator.open()
    ready = coordinator.start()
    if hold == "accept-conflict":
        coordinator.accept(ready)
        conflicting = json.loads(json.dumps(ready))
        conflicting["result"]["dispatchId"] = "ctx_000000000009"
        coordinator.accept(conflicting, error="accept-conflict")
    else:
        coordinator.invoke("hold-resolve", run_id, "one", "--abandon")
    before = _events(coordinator.workspace, run_id)

    def unexpected_close(*args):
        pytest.fail("refused recovery reached the lost-request write")

    monkeypatch.setattr(native_child_journal, "close_lost_request", unexpected_close)
    receipt = _recovery_fixture("start-failed-residual")
    receipt["result"]["mutation"] = ready["result"]["mutation"]
    coordinator.invoke(
        "hold-resolve",
        run_id,
        "one",
        "--native-json",
        coordinator.payload("start", receipt),
        "--exit-code",
        1,
        "--show-json",
        coordinator.payload("show", _recovery_fixture("worker-show-released-exited")),
        error="accept-conflict" if hold == "accept-conflict" else "task-held",
    )
    assert _events(coordinator.workspace, run_id) == before


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


def _set_transcript_page(reply, messages, cursor):
    reply["transcript"].update(messages=messages, returnedMessageCount=len(messages), nextCursor=cursor)
    reply["cursor"] = cursor


def test_observe_waits_for_the_workspace_run_lock(coordinator, monkeypatch):
    run_id = coordinator.open()
    coordinator.accept(coordinator.start())
    path = coordinator.payload("show", _fixture("worker-show-structured"))
    workspace = coordinator.workspace
    contended = Event()
    acquire = runguard._acquire_lock

    def signal_contention(*args, **kwargs):
        try:
            return acquire(*args, **kwargs)
        except runguard.RunLockError:
            contended.set()
            raise

    monkeypatch.setattr(runguard, "_acquire_lock", signal_contention)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with runguard.run_lock(workspace, run_dir=_run_dir(workspace, run_id)):
            pending = pool.submit(coordinator.invoke, "observe", run_id, "one", "--native-json", path)
            assert contended.wait(timeout=5)
        assert pending.result(timeout=10)["reason"] == "result-transfer-missing"


def test_orca_1_4_271_concurrent_closeout(coordinator):
    roster = coordinator.workspace / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text().replace(VERSION, "1.4.271"))
    tasks = ("one", "two", "three")
    coordinator.batch = _batch(coordinator.home, tasks)
    run_id = coordinator.open()
    start_closeout = Barrier(len(tasks), timeout=10)
    receipts = {}

    for index, task in enumerate(tasks, 1):
        orca_task = f"task_{index:012d}"
        dispatch = f"ctx_{index:012d}"
        grant = coordinator.claim(task, orca_task)
        start = _fixture("worker-start")
        start["result"].update(taskId=orca_task, dispatchId=dispatch)
        start["result"]["mutation"]["requestId"] = grant["retry_request"]
        accepted = coordinator.invoke(
            "accepted", run_id, task, "--native-json", coordinator.payload(f"{task}-start", start), "--exit-code", 0
        )
        assert accepted["observe_argv"] == (
            f"orca-ide orchestration worker-show --environment local --dispatch {dispatch} --json --full".split()
        )
        assert accepted["inbox_argv"] == (
            f"orca-ide orchestration inbox --full --environment local --run {RUN} --limit 100 --json".split()
        )
        assert accepted["transcript_argv"] == (
            f"orca-ide orchestration worker-read --environment local --dispatch {dispatch} --source transcript --json".split()
        )

        released = _recovery_fixture("worker-show-released-exited")
        # This fixture records a failed start. Use its release proof for a succeeded worker.
        released["result"]["dispatch"]["status"] = "completed"
        released["result"]["worker"]["state"] = "succeeded"
        payloads = {
            "released": released,
            "inbox": _fixture("worker-done"),
            "transcript": [_recovery_fixture(f"worker-read-page-{page}") for page in (1, 2)],
        }
        receipts[task] = {
            name: coordinator.payload(
                f"{task}-{name}", json.loads(json.dumps(payload).replace(TASK, orca_task).replace(DISPATCH, dispatch))
            )
            for name, payload in payloads.items()
        }

    before = _events(coordinator.workspace, run_id)

    def closeout(task):
        def invoke(verb, *args):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brigade",
                    "orca",
                    verb,
                    run_id,
                    task,
                    *map(str, args),
                    "--target",
                    str(coordinator.workspace),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        evidence = receipts[task]
        start_closeout.wait()
        proof = (
            "--native-json",
            evidence["released"],
            "--worker-done-json",
            evidence["inbox"],
            "--transcript-json",
            evidence["transcript"],
        )
        observed = invoke("observe", *proof)
        assert observed["reason"] == "terminal-evidence-complete"
        assert observed["transcript_clipped"] is True
        result_id = observed["result_id"]
        assert isinstance(result_id, str) and result_id
        assert invoke("ack", "--result-id", result_id)["acknowledged"]
        assert invoke("observe", *proof)["result_id"] == result_id

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        list(pool.map(closeout, tasks))

    after = _events(coordinator.workspace, run_id)
    assert after[: len(before)] == before
    closeout_events = after[len(before) :]
    assert [event.event_type for event in closeout_events[::2]] == ["run.snapshot.checkpointed"] * (3 * len(tasks))
    task_events = closeout_events[1::2]
    assert len(task_events) == 3 * len(tasks)
    for task in tasks:
        assert [
            (event.event_type, event.payload.get("reason"))
            for event in task_events
            if event.payload.get("task_id") == task
        ] == [
            ("task.native-capacity", "released"),
            ("task.observed", None),
            ("task.acknowledged", None),
        ]
    finalized = coordinator.invoke("finalize", run_id)
    assert finalized["status"] == "completed"
    assert finalized["tasks"] == finalized["terminals"] == finalized["acknowledged"] == 3
    status = coordinator.status()
    assert status["status"] == "completed"
    assert status["capacity"]["live"] == 0
    assert {row["task_id"] for row in status["tasks"] if row["terminal"] and row["acked"]} == set(tasks)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing-release", "terminal-evidence-complete"),
        ("unknown-liveness", "containment-unconfirmed"),
        ("unfinished-transcript", "result-transfer-missing"),
        ("wrong-inbox-task", "result-transfer-missing"),
        ("wrong-transcript-dispatch", "result-transfer-missing"),
        ("wrong-show-run", "task-mismatch"),
    ],
)
def test_orca_1_4_271_single_observe_requires_closeout_evidence(coordinator, case, reason):
    roster = coordinator.workspace / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text().replace(VERSION, "1.4.271"))
    run_id = coordinator.open()
    coordinator.accept(coordinator.start())
    show = _fixture("worker-show-released")
    inbox = _fixture("worker-done")
    pages = [_recovery_fixture(f"worker-read-page-{page}") for page in (1, 2)]
    if case == "missing-release":
        show = _fixture("worker-show-structured")
        del show["result"]["terminalResource"]
    elif case == "unknown-liveness":
        show["result"]["projection"]["liveness"]["verdict"] = "unknown"
    elif case == "unfinished-transcript":
        pages.pop()
    elif case == "wrong-inbox-task":
        message = inbox["result"]["messages"][0]
        message["payload"] = message["payload"].replace(TASK, SECOND_TASK)
    elif case == "wrong-transcript-dispatch":
        pages[1]["result"]["dispatchId"] = "ctx_000000000009"
    elif case == "wrong-show-run":
        show["result"]["dispatch"]["runId"] = "run_000000000009"
    observed = coordinator.observe(
        show,
        "--worker-done-json",
        coordinator.payload("inbox", inbox),
        "--transcript-json",
        coordinator.payload("transcript", pages),
        error=reason if case == "wrong-show-run" else None,
    )
    if case == "missing-release":
        assert observed["reason"] == reason and observed["result_id"]
        coordinator.invoke("ack", run_id, "one", "--result-id", observed["result_id"])
        assert coordinator.status()["tasks"][0]["capacity_reason"] == "release-unverified"
    else:
        if case != "wrong-show-run":
            assert observed["reason"] == reason and observed["result_id"] is None
        coordinator.invoke("ack", run_id, "one", "--result-id", "a" * 64, error="task-nonterminal")
    assert coordinator.status()["capacity"]["live"] == 1
    coordinator.invoke("finalize", run_id, error="finalize-live")


@pytest.mark.parametrize(
    "case",
    [
        "clipped",
        "unclipped",
        "other-clipping",
        "mixed-clipping",
        "missing-final-text",
        "source-identity-change",
        "source-changed",
        "page-error",
        "cursor-loop",
        "duplicate-page",
        "cursor-mismatch",
        "empty-final-cursor-mismatch",
        "missing-continuation",
        "three-pages",
        "eight-pages",
        "nine-pages",
        "missing-fallback-reason",
    ],
)
def test_orca_1_4_271_transcript_pages(coordinator, case):
    roster = coordinator.workspace / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text().replace(VERSION, "1.4.271"))
    run_id = coordinator.open()
    coordinator.accept(coordinator.start())
    pages = [_recovery_fixture(f"worker-read-page-{number}") for number in (1, 2)]
    first, last = (page["result"] for page in pages)
    if case == "other-clipping":
        first["clipping"] = ["terminal_payload"]
    elif case == "mixed-clipping":
        first["clipping"].append("terminal_payload")
    elif case == "missing-final-text":
        first["transcript"]["messages"][-1]["blocks"][0]["text"] = " "
    elif case == "source-identity-change":
        last["sourceIdentity"] = "other-example-transcript"
    elif case == "source-changed":
        last["fallbackReason"] = "source_changed"
    elif case == "page-error":
        pages[1] = {"ok": False, "error": {"code": "read_failed"}}
    elif case == "cursor-loop":
        middle = deepcopy(pages[0])
        _set_transcript_page(middle["result"], [], first["cursor"])
        pages.insert(1, middle)
    elif case == "duplicate-page":
        middle = deepcopy(pages[0])
        _set_transcript_page(middle["result"], middle["result"]["transcript"]["messages"], "example-cursor-2")
        pages.insert(1, middle)
        _set_transcript_page(last, [], middle["result"]["cursor"])
    elif case == "cursor-mismatch":
        first["cursor"] = "other-example-cursor"
    elif case == "empty-final-cursor-mismatch":
        _set_transcript_page(last, [], "other-example-cursor")
    elif case == "missing-continuation":
        pages.pop()
    elif case == "missing-fallback-reason":
        del first["fallbackReason"]
    elif case in ("three-pages", "eight-pages", "nine-pages"):
        messages = first["transcript"]["messages"][:]
        if case == "three-pages":
            groups, final_messages = [messages[:2], messages[2:4]], messages[4:]
        else:
            groups, final_messages = [[message] for message in messages], []
            if case == "nine-pages":
                groups.insert(1, [{**messages[0], "id": "extra-example-message"}])
        pages = [deepcopy(pages[0]) for _ in groups] + [pages[-1]]
        for index, (page, group) in enumerate(zip(pages[:-1], groups, strict=True), 1):
            _set_transcript_page(page["result"], group, f"example-cursor-{index}")
        final_cursor = "example-cursor-end" if final_messages else pages[-2]["result"]["cursor"]
        _set_transcript_page(last, final_messages, final_cursor)
    transcript = _fixture("worker-read") if case == "unclipped" else pages
    show = _fixture("worker-show-released")
    done_file = coordinator.payload("worker-done", _fixture("worker-done"))
    result = coordinator.observe(
        show,
        "--worker-done-json",
        done_file,
        "--transcript-json",
        coordinator.payload("transcript", transcript),
    )
    if case in ("clipped", "unclipped", "three-pages", "eight-pages", "missing-fallback-reason"):
        assert result["reason"] == "terminal-evidence-complete" and result["result_id"]
        if case == "unclipped":
            assert "transcript_clipped" not in result
        else:
            assert result["transcript_clipped"] is True
        if case == "clipped":
            messages = first["transcript"]["messages"]
            _set_transcript_page(first, messages[:-1], first["cursor"])
            _set_transcript_page(last, messages[-1:], "example-cursor-end")
            split = coordinator.observe(
                show,
                "--worker-done-json",
                done_file,
                "--transcript-json",
                coordinator.payload("transcript", pages),
            )
            assert split["result_id"] == result["result_id"]
            assert split["transcript_clipped"] is True and "Synthetic message" not in json.dumps(split)
            later = coordinator.observe(show)
            assert later["result_id"] == result["result_id"] and later["transcript_clipped"] is True
            journal_file = _run_dir(coordinator.workspace, run_id) / "events" / "lifecycle.jsonl"
            journal_text = journal_file.read_text()
            assert '"transcript_clipped":true' in journal_text
            assert "Synthetic message" not in journal_text and "Synthetic tool output." not in journal_text
        coordinator.invoke("ack", run_id, "one", "--result-id", result["result_id"])
        assert coordinator.status()["capacity"]["live"] == 0
        assert coordinator.invoke("finalize", run_id)["terminals"] == 1
    else:
        assert result["reason"] == "result-transfer-missing" and result["result_id"] is None
        assert "transcript_clipped" not in result


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
    # Bytes, not text mode: Windows text mode writes CRLF and the checkpoint refuses it as writer-bytes.
    path.write_bytes((json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    coordinator.start()
    coordinator.claim("two", SECOND_TASK, error="budget-exhausted")
    assert coordinator.invoke("hold-resolve", run_id, "one", "--abandon")["mode"] == "abandoned-unknown"
    coordinator.invoke("finalize", run_id, error="finalize-abandoned")
    row = coordinator.status()["tasks"][0]
    assert row["capacity_reason"] == "abandoned-unknown" and not row["terminal"]


def test_orca_1_4_269_unsupported_hands_off_only_at_admission(coordinator):
    roster = coordinator.workspace / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text().replace(VERSION, "1.4.269 extra"))
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
