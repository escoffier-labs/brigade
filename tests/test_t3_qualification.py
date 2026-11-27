"""Synthetic evidence for the #2046 matched experiment."""

import json
import shlex
import sys
from copy import deepcopy

import pytest

from brigade import t3_qualification as qualification


@pytest.fixture
def matched_records():
    records = []
    for index, (pair, arm) in enumerate(qualification.schedule()):
        records.append(
            {
                "pair": pair,
                "start_ns": index * 10**10,
                "closed_ns": (index + 1) * 10**10,
                "request_id": f"qualification-{pair}",
                "run_id": f"{arm}-{pair}",
                "parent": {"model": "gpt-6.2-sol", "effort": "medium", "service_tier": "default"}
                if arm == "t3"
                else None,
                "batch_fingerprint": str(pair) * 64,
                "envelope": qualification.envelope("a" * 40, "b" * 40, "c" * 64),
                "record": {
                    "schema": "brigade.orca_qualification.v1",
                    "arm": arm,
                    "configuration_mismatch": False,
                    "all_tasks_terminal": True,
                    "held_capacity": 0,
                    "slot_check_passed": True,
                    "circuit_breaker_check_passed": True,
                    "model": "gpt-6.2-sol",
                    "effort": "low",
                    "source_revision": "a" * 40,
                    "brigade_commit": "b" * 40,
                    "isolation": "worktree",
                    "concurrency": 3,
                    "read_only_enforcement": "prompt-only" if arm == "t3" else "brigade-read-only-policy",
                    "verified_completion_seconds": 6 if arm == "t3" else 7,
                    "timings_seconds": {
                        "queue": 1,
                        "startup": None,
                        "planning": 0,
                        "execution": 4,
                        "synthesis": 0,
                        "delivery": 1 if arm == "t3" else 2,
                    },
                    "errors": {"failed_tasks": 0, "nonterminal_tasks": 0, "circuit_broken_tasks": 0, "other": 0},
                    "parent_interventions": index,
                    "usage": {"input_tokens": None, "output_tokens": None, "cached_input_tokens": None},
                },
            }
        )
    return records


@pytest.mark.parametrize("effort", ["low", "medium"])
def test_complete_matched_pairs(matched_records, effort):
    if effort == "medium":
        for row in matched_records:
            row["envelope"]["effort"] = effort
            row["record"]["effort"] = effort
    report = qualification.summarize(matched_records)
    assert report["completed_pairs"] == 5
    assert report["arms"]["t3"]["median_verified_completion_seconds"] == 6
    assert report["arms"]["run-batch"]["median_verified_completion_seconds"] == 7
    assert report["arms"]["t3"]["usage"] == "not recorded"
    assert report["arms"]["run-batch"]["median_timings_seconds"]["queue_and_startup"] == 1
    assert report["arms"]["run-batch"]["errors"]["other"] == 0


def _abort_folder(tmp_path, workspace, run_id):
    folder = tmp_path / "qualification"
    folder.mkdir()
    (folder / "expected.json").write_text(
        json.dumps({"workspace": str(workspace), "run_ids": {"t3": run_id}, "parent_thread": "thread-parent"}),
        encoding="utf-8",
    )
    return folder


def _status_line(workspace, run_id):
    return shlex.join((*qualification.CLI, "t3", "status", run_id, "--target", str(workspace)))


def _finalize_command(workspace, run_id):
    return shlex.join((*qualification.CLI, "t3", "finalize", run_id, "--target", str(workspace)))


def _operator_lines(workspace, run_id):
    return [
        _status_line(workspace, run_id),
        f"Close this run with `{_finalize_command(workspace, run_id)}` once status shows no live or held tasks. "
        "Resolve any hold first. Finalize records failed unless every task is terminal and acknowledged.",
    ]


def _abort(monkeypatch, folder):
    monkeypatch.setattr(sys, "argv", ["t3_qualification", "abort-t3", str(folder), "parent failed"])
    qualification.main()


def _acknowledge_terminal(workspace, run_id, task_id="done"):
    from brigade import native_t3_capacity, native_t3_lifecycle, run_tasks

    run_dir = native_t3_capacity.runs_root(workspace) / run_id
    result_id = next(result.result_id for result in run_tasks.read_tasks(run_dir))
    native_t3_lifecycle.ack(workspace=workspace, run_id=run_id, task_id=task_id, result_id=result_id)


def test_abort_t3_acknowledges_terminal_tasks_and_keeps_a_live_run_open(tmp_path, monkeypatch, capsys):
    from brigade import native_t3_dispatch, native_t3_lifecycle
    from tests.test_native_t3_claim import THREAD, _claim, _enroll, _reply, _terminal_reply

    workspace, run_id = _enroll(tmp_path, ("done", "running"))
    for task_id in ("done", "running"):
        _claim(workspace, run_id, task_id)
        native_t3_dispatch.accepted(
            workspace=workspace,
            run_id=run_id,
            task_id=task_id,
            parent_thread=THREAD,
            payload=(
                _terminal_reply(workspace, run_id, task_id) if task_id == "done" else _reply(workspace, run_id, task_id)
            ),
        )
    folder = _abort_folder(tmp_path, workspace, run_id)

    _abort(monkeypatch, folder)

    report = native_t3_lifecycle.status(workspace=workspace, run_id=run_id)
    tasks = {task["task_id"]: task for task in report["tasks"]}
    assert tasks["done"]["terminal"] is tasks["done"]["acked"] is True
    assert tasks["running"]["terminal"] is tasks["running"]["acked"] is False
    assert report["status"] == "awaiting-native-parent"
    assert capsys.readouterr().out.splitlines() == _operator_lines(workspace, run_id)
    assert json.loads((folder / "failure.json").read_text(encoding="utf-8")) == {"error": "parent failed"}


def test_abort_t3_acknowledges_an_all_terminal_run_without_finalizing(tmp_path, monkeypatch, capsys):
    from brigade import cli, native_t3_dispatch, native_t3_lifecycle
    from tests.test_native_t3_claim import THREAD, _claim, _enroll, _terminal_reply

    workspace, run_id = _enroll(tmp_path, ("done",))
    _claim(workspace, run_id, "done")
    native_t3_dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="done",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id, "done"),
    )
    folder = _abort_folder(tmp_path, workspace, run_id)

    _abort(monkeypatch, folder)

    report = native_t3_lifecycle.status(workspace=workspace, run_id=run_id)
    assert report["status"] == "awaiting-native-parent"
    assert report["tasks"][0]["acked"] is True
    printed = capsys.readouterr().out.splitlines()
    assert printed == _operator_lines(workspace, run_id)
    instruction = printed[1]
    assert instruction == (
        f"Close this run with `{_finalize_command(workspace, run_id)}` once status shows no live or held tasks. "
        "Resolve any hold first. Finalize records failed unless every task is terminal and acknowledged."
    )

    finalize = shlex.split(printed[1].split("`")[1])
    assert cli.main(finalize[len(qualification.CLI) :]) == 0
    assert native_t3_lifecycle.status(workspace=workspace, run_id=run_id)["status"] == "completed"


def test_abort_t3_leaves_an_already_finalized_run_untouched(tmp_path, monkeypatch, capsys):
    from brigade import native_t3_dispatch, native_t3_lifecycle
    from tests.test_native_t3_claim import THREAD, _claim, _enroll, _terminal_reply

    workspace, run_id = _enroll(tmp_path, ("done",))
    _claim(workspace, run_id, "done")
    native_t3_dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="done",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id, "done"),
    )
    _acknowledge_terminal(workspace, run_id)
    native_t3_lifecycle.finalize(workspace=workspace, run_id=run_id)
    folder = _abort_folder(tmp_path, workspace, run_id)

    _abort(monkeypatch, folder)

    assert capsys.readouterr().out.splitlines() == _operator_lines(workspace, run_id)
    assert native_t3_lifecycle.status(workspace=workspace, run_id=run_id)["status"] == "completed"


def test_abort_t3_prints_the_operator_lines_when_a_hold_lands_after_status(tmp_path, monkeypatch, capsys):
    from brigade import cli, native_t3_dispatch, native_t3_lifecycle
    from tests.test_native_t3_claim import THREAD, _claim, _enroll, _terminal_reply

    workspace, run_id = _enroll(tmp_path, ("done",))
    _claim(workspace, run_id, "done")
    native_t3_dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="done",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id, "done"),
    )
    folder = _abort_folder(tmp_path, workspace, run_id)
    real_status = native_t3_lifecycle.status
    holds = []

    def status_then_hold(**kwargs):
        report = real_status(**kwargs)
        if not holds:
            holds.append("done")
            native_t3_dispatch.hold_resolve(
                workspace=workspace, run_id=run_id, task_id="done", payload=None, abandon=True
            )
        return report

    with monkeypatch.context() as patch:
        patch.setattr(native_t3_lifecycle, "status", status_then_hold)
        _abort(patch, folder)

    printed = capsys.readouterr().out.splitlines()
    assert printed == _operator_lines(workspace, run_id)
    report = real_status(workspace=workspace, run_id=run_id)
    assert report["status"] == "awaiting-native-parent"
    assert report["tasks"][0]["held_reason"] == "abandoned-unknown"

    finalize = shlex.split(printed[1].split("`")[1])
    assert cli.main(finalize[len(qualification.CLI) :]) == 2
    assert "finalize-abandoned" in capsys.readouterr().err
    held = real_status(workspace=workspace, run_id=run_id)
    assert held["status"] == "awaiting-native-parent"
    assert held["tasks"][0]["held_reason"] == "abandoned-unknown"


def test_abort_t3_leaves_a_queued_task_open_for_the_printed_finalize(tmp_path, monkeypatch, capsys):
    from brigade import cli, native_t3_lifecycle
    from tests.test_native_t3_claim import _enroll

    workspace, run_id = _enroll(tmp_path, ("queued",))
    folder = _abort_folder(tmp_path, workspace, run_id)

    _abort(monkeypatch, folder)

    printed = capsys.readouterr().out.splitlines()
    assert printed == _operator_lines(workspace, run_id)
    report = native_t3_lifecycle.status(workspace=workspace, run_id=run_id)
    assert report["status"] == "awaiting-native-parent"
    assert report["tasks"][0]["terminal"] is False

    finalize = shlex.split(printed[1].split("`")[1])
    assert cli.main(finalize[len(qualification.CLI) :]) == 0
    assert native_t3_lifecycle.status(workspace=workspace, run_id=run_id)["status"] == "failed"


def test_abort_t3_ignores_an_ack_error_and_prints_the_operator_lines(tmp_path, monkeypatch, capsys):
    from brigade import native_t3_dispatch, native_t3_lifecycle
    from tests.test_native_t3_claim import THREAD, _claim, _enroll, _terminal_reply

    workspace, run_id = _enroll(tmp_path, ("done",))
    _claim(workspace, run_id, "done")
    native_t3_dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="done",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id, "done"),
    )
    folder = _abort_folder(tmp_path, workspace, run_id)

    def refuse(**kwargs):
        raise OSError("journal unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(native_t3_lifecycle, "ack", refuse)
        _abort(patch, folder)

    assert capsys.readouterr().out.splitlines() == _operator_lines(workspace, run_id)
    assert json.loads((folder / "failure.json").read_text(encoding="utf-8")) == {"error": "parent failed"}
    assert native_t3_lifecycle.status(workspace=workspace, run_id=run_id)["status"] == "awaiting-native-parent"


def test_abort_t3_prints_the_operator_lines_when_status_fails(tmp_path, monkeypatch, capsys):
    from brigade import native_t3_dispatch, native_t3_lifecycle
    from tests.test_native_t3_claim import THREAD, _claim, _enroll, _terminal_reply

    workspace, run_id = _enroll(tmp_path, ("done",))
    _claim(workspace, run_id, "done")
    native_t3_dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="done",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id, "done"),
    )
    folder = _abort_folder(tmp_path, workspace, run_id)

    def refuse(**kwargs):
        raise OSError("journal unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(native_t3_lifecycle, "status", refuse)
        _abort(patch, folder)

    assert capsys.readouterr().out.splitlines() == _operator_lines(workspace, run_id)
    assert json.loads((folder / "failure.json").read_text(encoding="utf-8")) == {"error": "parent failed"}
    assert native_t3_lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]["acked"] is False


def test_abort_t3_can_run_twice(tmp_path, monkeypatch, capsys):
    from brigade import native_t3_dispatch, native_t3_lifecycle
    from tests.test_native_t3_claim import THREAD, _claim, _enroll, _terminal_reply

    workspace, run_id = _enroll(tmp_path, ("done",))
    _claim(workspace, run_id, "done")
    native_t3_dispatch.accepted(
        workspace=workspace,
        run_id=run_id,
        task_id="done",
        parent_thread=THREAD,
        payload=_terminal_reply(workspace, run_id, "done"),
    )
    folder = _abort_folder(tmp_path, workspace, run_id)

    _abort(monkeypatch, folder)
    capsys.readouterr()
    assert native_t3_lifecycle.status(workspace=workspace, run_id=run_id)["tasks"][0]["acked"] is True

    _abort(monkeypatch, folder)

    assert capsys.readouterr().out.splitlines() == _operator_lines(workspace, run_id)
    assert json.loads((folder / "failure.json").read_text(encoding="utf-8")) == {"error": "parent failed"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("missing", "five complete pairs"),
        ("overlap", "overlap"),
        ("order", "alternation"),
        ("envelope", "envelope"),
        ("input", "shared batch"),
        ("unverified", "verified"),
        ("read-only", "envelope"),
        ("effort", "envelope"),
    ],
)
def test_rejects_invalid_pairs(matched_records, change, message):
    rows = deepcopy(matched_records)
    if change == "missing":
        rows.pop()
    elif change == "overlap":
        rows[1]["start_ns"] -= 1
    elif change == "order":
        rows[2]["record"]["arm"], rows[3]["record"]["arm"] = rows[3]["record"]["arm"], rows[2]["record"]["arm"]
    elif change == "envelope":
        rows[1]["envelope"]["service_tier"] = "fast"
    elif change == "input":
        rows[1]["batch_fingerprint"] = "f" * 64
    elif change == "effort":
        rows[1]["record"]["effort"] = "medium"
    elif change == "read-only":
        rows[0]["record"]["read_only_enforcement"] = "brigade-read-only-policy"
    else:
        rows[0]["record"]["verified_completion_seconds"] = None
    with pytest.raises(ValueError, match=message):
        qualification.summarize(rows)


@pytest.mark.parametrize(
    ("arm", "case", "error"),
    [
        ("t3", "valid", None),
        ("run-batch", "valid", None),
        ("t3", "medium", None),
        ("run-batch", "medium", None),
        ("t3", "medium-low-child", "T3 status does not match"),
        ("t3", "medium-low-target", "retained T3 target"),
        ("run-batch", "medium-low-session", "Codex turn does not confirm"),
        ("run-batch", "medium-low-receipt", "worker receipt does not match"),
        ("t3", "restarted", None),
        ("run-batch", "missing-schema", "expected matched qualification measurements"),
        ("run-batch", "edited-errors", "does not match retained evidence|do not match retained evidence"),
        ("run-batch", "edited-timing", "do not match retained evidence"),
        ("t3", "edited-timing", "do not match retained evidence"),
        ("t3", "missing-capabilities-report", "capabilities-report.json"),
        ("t3", "missing-readiness", "readiness.json"),
        ("t3", "missing-delegate-start", "delegate-start.json"),
        ("t3", "missing-delegate-replies", "delegate-replies.json"),
        ("t3", "missing-delegate-reply-guide", "delegate-reply-guide.json"),
        ("t3", "missing-wake-guide", "wake-guide.json"),
        ("t3", "unordered-marks", "out of order"),
        ("t3", "wrong-parent-model", "parent must be"),
        ("t3", "wrong-parent-effort", "parent must be"),
        ("t3", "dirty-tree", "worktree or base revision mismatch"),
        ("run-batch", "dirty-tree", "worktree or base revision mismatch"),
        ("t3", "duplicate-tree", "worktree or base revision mismatch"),
        ("run-batch", "duplicate-tree", "worktree or base revision mismatch"),
    ],
)
def test_collects_paired_route_with_bound_results(tmp_path, monkeypatch, arm, case, error):
    import hashlib
    from types import SimpleNamespace

    from brigade import orca_qualification, run_batch
    from brigade import t3_qualification_evidence as evidence

    def save(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    base = "a" * 40
    workspace = tmp_path / "workspace"
    pair = tmp_path / "pair"
    folder = pair / arm
    folder.mkdir(parents=True)
    run_dir = tmp_path / f"{arm}-run"
    run_dir.mkdir()
    ids = ["guide", "package", "runner"]
    payload = {
        "schema": run_batch.SCHEMA,
        "request_id": "paired-request",
        "tasks": [
            {"task_id": name, "seat": "worker", "text": f"answer {name}", "isolation": "worktree", "read_only": True}
            for name in ids
        ],
    }
    save(pair / "batch.json", payload)
    fingerprint = run_batch.request_fingerprint(payload, base_sha=base)
    context = {
        "request_id": "paired-request",
        "workspace": str(workspace),
        "base_sha": base,
        "brigade_commit": "b" * 40,
        "batch_fingerprint": fingerprint,
        "answers": {name: {"head": base, "sha256": "c" * 64} for name in ids},
        "run_ids": {arm: run_dir.name},
        "provider_instance": "codex-example",
        "parent_thread": "thread-parent",
        "project": "project-example",
        "environment": "env-example",
        "seat": "worker",
    }
    worker_effort = "medium" if case.startswith("medium") else "low"
    if worker_effort == "medium":
        context["worker_effort"] = worker_effort
    save(folder / "expected.json", context)
    turns = {name: {"started_at": "2026-01-01T00:00:02Z", "ended_at": "2026-01-01T00:00:06Z"} for name in ids}
    if arm == "run-batch":
        for turn in turns.values():
            turn["provider_errors"] = 1
    opened = {"monotonic_ns": 10**9, "wall_ns": evidence.utc("2026-01-01T00:00:01Z")}
    measurements = {
        "schema": evidence.MEASUREMENTS_SCHEMA,
        "run_id": run_dir.name,
        "opened": opened,
        "verified": {"monotonic_ns": 9 * 10**9, "wall_ns": evidence.utc("2026-01-01T00:00:09Z")},
        "turns": turns,
    }
    save(folder / "opened.json", opened)
    save(folder / "verified.json", measurements["verified"])
    if arm == "t3":
        capabilities = {
            "parentThreadId": context["parent_thread"],
            "inheritedModel": "other-model" if case == "wrong-parent-model" else "gpt-6.2-sol",
            "inheritedOptions": [
                {"id": "reasoningEffort", "value": "high" if case == "wrong-parent-effort" else "medium"},
                {"id": "serviceTier", "value": "default"},
            ],
            "inheritedProviderInstanceId": "codex-parent",
            "providers": [{"providerInstanceId": "codex-parent", "driverKind": "codex"}],
        }
        reported = "2026-01-01T00:00:00Z"
        save(folder / "capabilities.json", capabilities)
        save(
            folder / "capabilities-report.json",
            {
                "schema": evidence.roster_readiness_t3.SCHEMA,
                "origin_environment": context["environment"],
                "origin_project": context["project"],
                "reported_at": reported,
                "capabilities": capabilities,
            },
        )
        save(
            folder / "readiness.json",
            {
                "schema": evidence.roster_readiness.SCHEMA,
                "status": "ready",
                "exit_code": 0,
                **evidence.roster_readiness.T3_QUALIFIERS,
                "t3_reported_at": evidence.utc(reported) / 1e9,
                "t3_received_at": evidence.utc(reported) / 1e9 + 0.5,
                "request": {"route": "t3", "isolation": "worktree", "concurrency": 3, "workers": ["worker"]},
            },
        )
        save(folder / "delegate-start.json", {"monotonic_ns": 1_100_000_000})
        save(folder / "delegate-replies.json", {"monotonic_ns": 2_000_000_000})
        for i, name in enumerate(ids):
            save(folder / f"delegate-reply-{name}.json", {"monotonic_ns": 1_200_000_000 + i})
            wake = 7_000_000_000 + i
            if case == "unordered-marks" and name == "guide":
                wake = 1_100_000_000
            save(folder / f"wake-{name}.json", {"monotonic_ns": wake})
        if case.startswith("missing-"):
            (folder / f"{case.removeprefix('missing-')}.json").unlink()
    (folder / "interventions.jsonl").write_text('"operator action"\n', encoding="utf-8")
    (folder / "errors.jsonl").touch()

    def worker_options(effort):
        return [{"id": "reasoningEffort", "value": effort}, {"id": "serviceTier", "value": "default"}]

    members = []

    def tree_for(name):
        return tmp_path / ("same-tree" if case == "duplicate-tree" else name)

    for name in ids:
        answer = json.dumps(context["answers"][name])
        receipt = run_dir / f"batch-result-{hashlib.sha256(name.encode()).hexdigest()}.json"
        receipt_effort = "low" if case == "medium-low-receipt" and name == "guide" else worker_effort
        save(receipt, {"ok": True, "requested_model": "gpt-6.2-sol", "reasoning": receipt_effort, "text": answer})
        task = SimpleNamespace(
            handle=SimpleNamespace(spec=SimpleNamespace(task_id=name), attempt_id=f"{name}-attempt"),
            workspace=SimpleNamespace(base_sha=base),
            status="completed",
            containment="quiescent",
            result_id=f"{name}-result",
            provider_task_id=f"{name}-native-task",
            original_run_id=f"{name}-child-run",
            provider_result_id=f"{name}-restarted-run" if case == "restarted" else f"{name}-child-run",
            artifact_kind="receipt",
            artifact_digest=hashlib.sha256(receipt.read_bytes()).hexdigest(),
            acknowledged=True,
        )
        members.append(task)
        if arm == "t3":
            if case == "restarted":
                turns[name].update(original_run_id=task.original_run_id, terminal_run_id=task.provider_result_id)
            native = {
                "taskId": task.provider_task_id,
                "childThreadId": f"{name}-thread",
                "childRunId": task.original_run_id,
                "providerInstanceId": context["provider_instance"],
                "options": worker_options("low" if case == "medium-low-child" and name == "guide" else worker_effort),
            }
            save(folder / f"delegate-{name}.json", native)
            save(
                folder / f"status-{name}.json",
                {
                    **native,
                    "status": "completed",
                    "executedModel": "gpt-6.2-sol",
                    "latestTerminalRunId": task.provider_result_id,
                    "resultContextTransferId": f"{name}-transfer",
                    "hasPendingChildRuns": False,
                    "hasPendingQuestion": False,
                    "pendingApproval": None,
                    "summary": answer,
                },
            )
            save(
                folder / f"activity-{name}.json",
                {
                    "hasMore": False,
                    "thread": {
                        "threadId": native["childThreadId"],
                        "worktreePath": str(tree_for(name)),
                        "parentThreadId": context["parent_thread"],
                        "projectId": context["project"],
                        "activeRunId": None,
                    },
                    "recentRuns": [
                        {
                            "runId": task.provider_result_id,
                            "startedAt": turns[name]["started_at"],
                            "completedAt": turns[name]["ended_at"],
                            "status": "completed",
                            "executedModel": "gpt-6.2-sol",
                            "providerInstanceId": context["provider_instance"],
                        }
                    ],
                },
            )
            join = {"result_id": task.result_id, "run_id": run_dir.name, "task_id": name}
            save(folder / f"ack-{name}.json", join)
            save(folder / f"observe-{name}.json", {**join, "attempt_id": task.handle.attempt_id, "status": "completed"})
        else:
            session_effort = "low" if case == "medium-low-session" and name == "guide" else worker_effort
            session = _session_rows(
                base, tree_for(name), turns[name]["started_at"], turns[name]["ended_at"], effort=session_effort
            )
            (folder / f"session-{name}.jsonl").write_text(
                "".join(f"{json.dumps(row)}\n" for row in session), encoding="utf-8"
            )

    monkeypatch.setattr(evidence, "clean_revision", lambda _: "d" * 40 if case == "dirty-tree" else base)
    monkeypatch.setattr(evidence.run_batch_workspace, "expected_path", lambda _workspace, _run, name: tree_for(name))

    monkeypatch.setattr(evidence.run_tasks, "read_task_batch", lambda *_a, **_kw: SimpleNamespace(members=members))
    monkeypatch.setattr(
        evidence.native_child_journal,
        "read_events",
        lambda _: [SimpleNamespace(event_type="task.native-held")],
    )
    if arm == "t3":
        save(
            run_dir / "t3-enrollment.json",
            {
                "schema": "brigade.t3_enrollment.v1",
                "request_id": "paired-request",
                "fingerprint": fingerprint,
                "source_revision": base,
            },
        )
        status = {
            "schema": "brigade.t3_status.v1",
            "run_id": run_dir.name,
            "status": "completed",
            "tasks": [{"task_id": name, "held_reason": None, "acked": True, "terminal": True} for name in ids],
        }
        monkeypatch.setattr(evidence.native_t3_lifecycle, "status", lambda **_kw: status)
        monkeypatch.setattr(
            evidence.native_t3_enrollment,
            "read_delegation_input",
            lambda _, task_id: SimpleNamespace(
                task_id=task_id,
                seat="worker",
                text=f"answer {task_id}",
                target=SimpleNamespace(
                    source_revision=base,
                    isolation="worktree",
                    launch_model="gpt-6.2-sol",
                    driver="codex",
                    provider_instance=context["provider_instance"],
                    options=[
                        (entry["id"], entry["value"])
                        for entry in worker_options(
                            "low" if case == "medium-low-target" and task_id == "guide" else worker_effort
                        )
                    ],
                ),
            ),
        )
    else:
        canonical = run_batch.validate_batch(payload)
        runner_marker = run_batch.plan_batch(payload, base_sha=base)
        save(run_dir / "batch-request.json", {**runner_marker, "tasks": canonical["tasks"]})
        save(run_dir / "run.json", {"status": "ok"})
        status = {
            "schema": "brigade.run_batch_result.v1",
            "run_id": run_dir.name,
            "run_status": "ok",
            "request_id": "paired-request",
            "synthesis": None,
            "tasks": {name: {"result_id": f"{name}-result"} for name in ids},
        }
    save(folder / "status.json", status)
    if case == "missing-schema":
        del measurements["schema"]
    elif case == "edited-errors":
        turns["guide"]["provider_errors"] = 0
    elif case == "edited-timing":
        turns["guide"]["started_at"] = "2026-01-01T00:00:04Z"
    save(folder / "measurements.json", measurements)

    def collect():
        return orca_qualification.collect(
            arm, run_dir, folder / "status.json", folder / "measurements.json", qualification="t3"
        )

    if error is not None:
        with pytest.raises((OSError, ValueError), match=error):
            collect()
        return
    record = collect()
    assert record["effort"] == worker_effort
    assert record["all_tasks_terminal"]
    assert record["verified_completion_seconds"] == 8
    assert record["timings_seconds"]["queue"] == 1
    assert record["timings_seconds"]["execution"] == 4
    assert record["timings_seconds"]["delivery"] == 3
    assert record["parent_interventions"] == 2

    assert record["errors"]["other"] == (3 if arm == "run-batch" else 0)


@pytest.mark.parametrize("tier", ["default", "fast", None])
def test_direct_tier_gate_reads_configuration_without_a_turn(monkeypatch, tmp_path, tier):
    calls = []

    class Server:
        def __init__(self, *, cwd):
            assert cwd == tmp_path

        def start(self, *, timeout):
            calls.append("start")

        def request(self, method, params, *, timeout):
            calls.append(method)
            assert params["cwd"] == str(tmp_path)
            return {"config": {"service_tier": tier}}

        def close(self):
            calls.append("close")

    monkeypatch.setattr(qualification, "AppServer", Server)
    if tier == "default":
        qualification._check_direct_tier(tmp_path)
    else:
        with pytest.raises(ValueError, match="effective service tier"):
            qualification._check_direct_tier(tmp_path)
    assert calls == ["start", "config/read", "close"]


def _session_rows(base, tree, turn_start, turn_end, *, effort="low"):
    return [
        {"type": "session_meta", "payload": {"id": "session", "cwd": str(tree), "git": {"commit_hash": base}}},
        {"type": "turn_context", "payload": {"model": "gpt-6.2-sol", "effort": effort}},
        {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_id": "session",
                "thread_settings": {
                    "cwd": str(tree),
                    "model": "gpt-6.2-sol",
                    "reasoning_effort": effort,
                    "service_tier": "default",
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "task_started"}, "timestamp": turn_start},
        {"type": "event_msg", "payload": {"type": "stream_error"}},
        {"type": "event_msg", "payload": {"type": "task_complete"}, "timestamp": turn_end},
    ]


def test_reads_direct_turn_and_applied_settings(tmp_path):
    from brigade import t3_qualification_evidence as evidence

    base, tree = "a" * 40, tmp_path / "worktree"
    turn_start, turn_end = "2026-01-01T00:00:02Z", "2026-01-01T00:00:06Z"
    records = _session_rows(base, tree, turn_start, turn_end)
    (tmp_path / "session.jsonl").write_text("".join(f"{json.dumps(row)}\n" for row in records), encoding="utf-8")
    assert evidence._provider_turn(tmp_path, tree, {"wall_ns": 0}, base, "low") == {
        "started_at": turn_start,
        "ended_at": turn_end,
        "provider_errors": 1,
    }


def test_report_refuses_rewritten_record(tmp_path, matched_records, monkeypatch):

    fast = next(row for row in matched_records if (row["pair"], row["record"]["arm"]) == (2, "run-batch"))
    fast["record"]["verified_completion_seconds"] -= 1
    fast["record"]["timings_seconds"]["delivery"] -= 1
    for original in matched_records:
        folder = tmp_path / f"pair-{original['pair']:02}" / original["record"]["arm"]
        folder.mkdir(parents=True)
        row = deepcopy(original)
        if (row["pair"], row["record"]["arm"]) == (4, "run-batch"):
            row["record"] = deepcopy(fast["record"])
        (folder / "record.json").write_text(json.dumps(row), encoding="utf-8")

    def derive(output, pair, arm):
        assert output == tmp_path
        return next(row for row in matched_records if (row["pair"], row["record"]["arm"]) == (pair, arm))

    monkeypatch.setattr(qualification, "_collect_row", derive, raising=False)
    monkeypatch.setattr(sys, "argv", ["t3_qualification", "report", str(tmp_path)])
    with pytest.raises(SystemExit) as failure:
        qualification.main()
    assert failure.value.code == 2
    assert not (tmp_path / "report.json").exists()


def test_timing_uses_one_clock_with_forward_wall_drift():
    from brigade import t3_qualification_evidence as evidence

    wall = evidence.utc("2026-01-01T00:00:00Z")
    measurement = {
        "opened": {"monotonic_ns": 100_000_000, "wall_ns": wall},
        "verified": {"monotonic_ns": 200_000_000, "wall_ns": wall + 140_000_000},
        "turns": {"guide": {"started_at": "2026-01-01T00:00:00.010Z", "ended_at": "2026-01-01T00:00:00.139Z"}},
    }
    total, phases = evidence.timing(measurement, {"guide"})
    assert total == pytest.approx(0.1)
    assert phases["delivery"] >= 0
    assert sum(duration for duration in phases.values() if duration is not None) == pytest.approx(total)


@pytest.fixture
def coordinator_brief(tmp_path):
    context = {
        "workspace": str(tmp_path / "workload"),
        "run_ids": {"t3": "t3-example"},
        "parent_thread": "thread-parent",
        "seat": "worker",
    }
    qualification._write_brief(tmp_path, context, tmp_path / "roster.toml")
    return (tmp_path / "coordinator.txt").read_text(encoding="utf-8")


def test_coordinator_renders_single_marks_outside_task_loop(coordinator_brief):
    brief = coordinator_brief
    commands = [line.split()[-1] for line in brief.splitlines() if "brigade.t3_qualification mark " in line]
    assert commands == [
        "opened",
        "delegate-start",
        "delegate-reply-TASK",
        "delegate-replies",
        "wake-TASK",
    ]
    assert brief.index(" delegate-start") < brief.index("For each task ID")
    assert brief.index(" delegate-replies") > brief.index("accepted-TASK.json")
    assert (
        brief.index(" > claim-TASK.json")
        < brief.index("b. Call delegate_task")
        < brief.index(" delegate-reply-TASK")
        < brief.index(" > accepted-TASK.json")
    )
    assert "Substitute the waking task ID for TASK" in brief


def test_coordinator_resumes_from_saved_outputs_and_marks_before_finalize(coordinator_brief):
    launch, wake = coordinator_brief.split("## Each completion wake", 1)
    launch_rule = " ".join(launch.split())
    wake_rule = " ".join(wake.split())

    assert "continue at the first unfinished sub-step for each task" in launch_rule
    assert "Never re-run a sub-step whose output file exists" in launch_rule
    for filename in (
        "claim-TASK.json",
        "delegate-TASK.json",
        "delegate-reply-TASK.json",
        "accepted-TASK.json",
        "opened.json",
        "delegate-start.json",
        "delegate-replies.json",
        "wake-TASK.json",
    ):
        assert filename in launch_rule
    assert "continue at the first unfinished sub-step for each task" in wake_rule
    assert "skip the wake mark if wake-TASK.json already exists" in wake_rule
    assert "Never finalize before delegate-replies.json exists" in wake_rule
    assert "Mark launch complete before finalizing" in launch_rule
    assert coordinator_brief.index(" delegate-replies\n") < coordinator_brief.index(" > finalize.json")


@pytest.mark.parametrize("task_text_version", [None, 1, 2, 3])
def test_collect_row_binds_directory_and_explicit_route(tmp_path, monkeypatch, task_text_version):
    from brigade import localio, run_batch

    campaign = {
        "schema": qualification.CAMPAIGN_SCHEMA,
        "campaign_id": "c" * 32,
        "workspace": str(tmp_path / "workspace"),
        "base_sha": "a" * 40,
        "brigade_commit": "b" * 40,
        "seat": "worker",
    }
    pair, arm = 4, "run-batch"
    pair_dir = tmp_path / "pair-04"
    folder = pair_dir / arm
    folder.mkdir(parents=True)
    request, ids = qualification._pair_identities(campaign, pair)
    if task_text_version is not None:
        campaign["task_text_version"] = task_text_version
    monkeypatch.setattr(qualification.subprocess, "check_output", lambda argv, **_kwargs: argv[-1].encode())
    payload, answers = qualification.prepare_batch(
        tmp_path, campaign["base_sha"], campaign["seat"], request, campaign.get("task_text_version", 1)
    )
    context = {
        **campaign,
        "qualification": "t3",
        "pair": pair,
        "arm": arm,
        "request_id": request,
        "run_ids": ids,
        "batch_fingerprint": run_batch.request_fingerprint(payload, base_sha=campaign["base_sha"]),
        "answers": answers,
    }
    localio.write_json(tmp_path / "campaign.json", campaign)
    localio.write_json(pair_dir / "batch.json", payload)
    localio.write_json(folder / "expected.json", context)
    for mark, value in zip(("offered", "opened", "verified"), (0, 1, 2), strict=True):
        localio.write_json(folder / f"{mark}.json", {"monotonic_ns": value})
    localio.write_json(folder / "closed.json", {"monotonic_ns": 3, "native_live": 0})

    def collect(selected, run_dir, status, measurements, *, qualification):
        assert (selected, qualification, run_dir.name) == (arm, "t3", ids[arm])
        assert status == folder / "status.json"
        assert measurements == folder / "measurements.json"
        return {"arm": selected, "effort": "low"}

    monkeypatch.setattr(qualification.orca_qualification, "collect", collect)
    row = qualification._collect_row(tmp_path, pair, arm)
    assert row["envelope"]["effort"] == row["record"]["effort"] == "low"
    assert (row["pair"], row["run_id"], row["request_id"], row["batch_fingerprint"]) == (
        pair,
        ids[arm],
        request,
        context["batch_fingerprint"],
    )
    for key, changed in (("pair", 2), ("request_id", "other-request"), ("run_ids", {arm: "other-run"})):
        localio.write_json(folder / "expected.json", {**context, key: changed})
        with pytest.raises(ValueError, match="pair directory"):
            qualification._collect_row(tmp_path, pair, arm)
    localio.write_json(folder / "expected.json", context)
    localio.write_json(tmp_path / "campaign.json", {**campaign, "worker_effort": "medium"})
    with pytest.raises(ValueError, match="campaign worker effort differs"):
        qualification._collect_row(tmp_path, pair, arm)


@pytest.mark.parametrize("early_wake", [False, True])
def test_t3_marks_allow_wakes_during_launch_after_own_reply(tmp_path, monkeypatch, coordinator_brief, early_wake):
    from brigade import t3_qualification_evidence as evidence

    wake_commands = [
        shlex.split(line)[3:]
        for line in coordinator_brief.splitlines()
        if "brigade.t3_qualification mark " in line and line.split()[-1].startswith("wake-")
    ]

    def mark_wake(task, stamp):
        monkeypatch.setattr(evidence, "clock", lambda: {"monotonic_ns": stamp})
        for command in wake_commands:
            monkeypatch.setattr(sys, "argv", ["qualification", *command[:-1], command[-1].replace("TASK", task)])
            qualification.main()

    # Tenths of a second after opened, following the pair 1 wake ordering.
    def mark_launch(name, stamp):
        (tmp_path / f"{name}.json").write_text(json.dumps({"monotonic_ns": stamp}), encoding="utf-8")

    mark_launch("delegate-start", 84)
    mark_launch("delegate-reply-guide", 350)
    mark_wake("guide", 340 if early_wake else 464)
    assert sorted(path.name for path in tmp_path.glob("wake-*.json")) == ["wake-guide.json"]
    mark_launch("delegate-reply-package", 660)
    mark_wake("package", 720)
    mark_launch("delegate-reply-runner", 890)
    mark_launch("delegate-replies", 909)
    mark_wake("runner", 966)

    args = (tmp_path, {"monotonic_ns": 0}, {"monotonic_ns": 1000}, {"guide", "package", "runner"})
    if early_wake:
        with pytest.raises(ValueError, match="out of order for guide"):
            evidence._validate_t3_marks(*args)
    else:
        evidence._validate_t3_marks(*args)


@pytest.mark.parametrize(("option", "expected"), [([], "low"), (["--worker-effort", "medium"], "medium")])
def test_run_cli_selects_worker_effort(monkeypatch, tmp_path, option, expected):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualification",
            "run",
            "--workspace",
            str(tmp_path),
            "--output",
            str(tmp_path / "output"),
            "--roster",
            "roster.toml",
            "--codex-sessions",
            "sessions",
            "--seat",
            "worker",
            "--brigade-commit",
            "a" * 40,
            *option,
        ],
    )
    selections = []
    monkeypatch.setattr(qualification, "run", lambda args: selections.append(args.worker_effort))
    qualification.main()
    assert selections == [expected]


@pytest.mark.parametrize(("seat_effort", "t3_effort"), [("medium", "medium"), ("low", "medium"), ("medium", "low")])
def test_preflight_matches_worker_effort(tmp_path, monkeypatch, seat_effort, t3_effort):
    from types import SimpleNamespace

    base = "a" * 40
    agent = SimpleNamespace(
        cli="codex",
        transport="direct",
        command=None,
        env=None,
        fallback=(),
        model="gpt-6.2-sol",
        reasoning=seat_effort,
    )
    loaded = SimpleNamespace(
        agents={"worker": agent},
        t3_targets={"worker": {}},
        max_workers=3,
        orchestrator="parent",
        sandbox="read-only",
    )
    target = SimpleNamespace(
        options=(("reasoningEffort", t3_effort), ("serviceTier", "default")),
        launch_model="gpt-6.2-sol",
        driver="codex",
        target_environment="env-example",
        origin_environment="env-example",
    )
    roster_path = tmp_path / "roster.toml"
    monkeypatch.setattr(qualification.roster, "resolve_roster_path", lambda _: roster_path)
    monkeypatch.setattr(qualification.roster, "load_roster", lambda _: loaded)
    monkeypatch.setattr(qualification.native_t3_enrollment, "target_for_seat", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(qualification.evidence, "clean_revision", lambda _: base)
    monkeypatch.setattr(qualification.evidence, "check_native_idle", lambda _: None)

    args = (tmp_path, roster_path, "worker", base, "medium")
    if seat_effort == t3_effort == "medium":
        assert qualification._preflight(*args) == target
    else:
        with pytest.raises(ValueError, match="must both use medium effort"):
            qualification._preflight(*args)


def test_campaign_shares_plain_task_text_between_arms(tmp_path, monkeypatch):
    from types import SimpleNamespace

    base = "a" * 40
    args = SimpleNamespace(brigade_commit="b" * 40, seat="worker", worker_effort="medium", parent_timeout=1)
    target = SimpleNamespace(
        origin_thread="thread-example",
        origin_environment="env-example",
        target_project="project-example",
        provider_instance="codex-example",
    )
    batch_bytes = {}
    expected_answers = {}

    def capture(arm, folder):
        batch_bytes[arm] = (folder.parent / "batch.json").read_bytes()
        expected_answers[arm] = json.loads((folder / "expected.json").read_bytes())["answers"]

    monkeypatch.setattr(qualification, "schedule", lambda: [(1, "t3"), (1, "run-batch")])
    monkeypatch.setattr(qualification, "_preflight", lambda *_args: target)
    monkeypatch.setattr(qualification.subprocess, "check_output", lambda argv, **_kwargs: argv[-1].encode())
    monkeypatch.setattr(qualification, "_write_brief", lambda folder, *_args: capture("t3", folder))
    monkeypatch.setattr(qualification, "_wait_for_parent", lambda *_args: None)
    monkeypatch.setattr(
        qualification, "_direct", lambda _workspace, _roster, folder, *_args: capture("run-batch", folder)
    )
    monkeypatch.setattr(qualification, "_collect_row", lambda *_args, **_kwargs: {"start_ns": 1, "closed_ns": 1})
    monkeypatch.setattr(qualification, "summarize", lambda _rows: {})

    qualification._run_campaign(args, tmp_path, tmp_path, tmp_path / "roster.toml", tmp_path, base, target)

    assert json.loads((tmp_path / "campaign.json").read_bytes())["task_text_version"] == 3
    assert batch_bytes["t3"] == batch_bytes["run-batch"]
    batch = json.loads(batch_bytes["t3"])
    assert {task["task_id"] for task in batch["tasks"]} == qualification.WORKLOAD.keys()
    preamble = (
        "This is an operator-assigned, read-only benchmark task under Brigade governance. "
        "The operator authorized the two commands below. Complete the task without asking for another assignment.\n\n"
    )
    legacy, legacy_answers = qualification.prepare_batch(tmp_path, base, args.seat, batch["request_id"], 1)
    assert batch == legacy
    version_two, version_two_answers = qualification.prepare_batch(tmp_path, base, args.seat, batch["request_id"], 2)
    assert [task["text"] for task in version_two["tasks"]] == [preamble + task["text"] for task in legacy["tasks"]]
    assert expected_answers["t3"] == expected_answers["run-batch"] == legacy_answers == version_two_answers
