"""Synthetic evidence for the #2046 matched experiment."""

import json
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


def test_complete_matched_pairs(matched_records):
    report = qualification.summarize(matched_records)
    assert report["completed_pairs"] == 5
    assert report["arms"]["t3"]["median_verified_completion_seconds"] == 6
    assert report["arms"]["run-batch"]["median_verified_completion_seconds"] == 7
    assert report["arms"]["t3"]["usage"] == "not recorded"
    assert report["arms"]["run-batch"]["median_timings_seconds"]["queue_and_startup"] == 1
    assert report["arms"]["run-batch"]["errors"]["other"] == 0


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
        ("t3", "restarted", None),
        ("run-batch", "missing-schema", "expected matched qualification measurements"),
        ("run-batch", "edited-errors", "does not match retained evidence|do not match retained evidence"),
        ("run-batch", "edited-timing", "do not match retained evidence"),
        ("t3", "edited-timing", "do not match retained evidence"),
        ("t3", "missing-capabilities-report", "capabilities-report.json"),
        ("t3", "missing-readiness", "readiness.json"),
        ("t3", "missing-delegate-start", "delegate-start.json"),
        ("t3", "missing-delegate-replies", "delegate-replies.json"),
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
            wake = 7_000_000_000 + i
            if case == "unordered-marks" and name == "guide":
                wake = 1_100_000_000
            save(folder / f"wake-{name}.json", {"monotonic_ns": wake})
        if case.startswith("missing-"):
            (folder / f"{case.removeprefix('missing-')}.json").unlink()
    (folder / "interventions.jsonl").write_text('"operator action"\n', encoding="utf-8")
    (folder / "errors.jsonl").touch()

    options = [{"id": "reasoningEffort", "value": "low"}, {"id": "serviceTier", "value": "default"}]
    members = []

    def tree_for(name):
        return tmp_path / ("same-tree" if case == "duplicate-tree" else name)

    for name in ids:
        answer = json.dumps(context["answers"][name])
        receipt = run_dir / f"batch-result-{hashlib.sha256(name.encode()).hexdigest()}.json"
        save(receipt, {"ok": True, "requested_model": "gpt-6.2-sol", "reasoning": "low", "text": answer})
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
                "options": options,
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
            session = _session_rows(base, tree_for(name), turns[name]["started_at"], turns[name]["ended_at"])
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
                    options=[(entry["id"], entry["value"]) for entry in options],
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


def _session_rows(base, tree, turn_start, turn_end):
    return [
        {"type": "session_meta", "payload": {"id": "session", "cwd": str(tree), "git": {"commit_hash": base}}},
        {"type": "turn_context", "payload": {"model": "gpt-6.2-sol", "effort": "low"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_id": "session",
                "thread_settings": {
                    "cwd": str(tree),
                    "model": "gpt-6.2-sol",
                    "reasoning_effort": "low",
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
    assert evidence._provider_turn(tmp_path, tree, {"wall_ns": 0}, base) == {
        "started_at": turn_start,
        "ended_at": turn_end,
        "provider_errors": 1,
    }


def test_report_refuses_rewritten_record(tmp_path, matched_records, monkeypatch):
    import sys

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


def test_coordinator_renders_single_marks_outside_task_loop(tmp_path):
    context = {
        "workspace": str(tmp_path / "workload"),
        "run_ids": {"t3": "t3-example"},
        "parent_thread": "thread-parent",
        "seat": "worker",
    }
    qualification._write_brief(tmp_path, context, tmp_path / "roster.toml")
    brief = (tmp_path / "coordinator.txt").read_text(encoding="utf-8")
    commands = [line.split()[-1] for line in brief.splitlines() if "brigade.t3_qualification mark " in line]
    assert commands == ["opened", "delegate-start", "delegate-replies", "wake-guide", "wake-package", "wake-runner"]
    assert brief.index(" delegate-start") < brief.index("For each task ID")
    assert brief.index(" delegate-replies") > brief.index("accepted-TASK.json")
    assert "Run only the command for that task" in brief


def test_collect_row_binds_directory_and_explicit_route(tmp_path, monkeypatch):
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
    payload = {
        "schema": run_batch.SCHEMA,
        "request_id": request,
        "tasks": [
            {"task_id": "runner", "seat": "worker", "text": "fixture", "read_only": True, "isolation": "worktree"}
        ],
    }
    answers = {"runner": {"head": campaign["base_sha"], "sha256": "d" * 64}}
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
    monkeypatch.setattr(qualification, "prepare_batch", lambda *_args: (payload, answers))

    def collect(selected, run_dir, status, measurements, *, qualification):
        assert (selected, qualification, run_dir.name) == (arm, "t3", ids[arm])
        assert status == folder / "status.json"
        assert measurements == folder / "measurements.json"
        return {"arm": selected}

    monkeypatch.setattr(qualification.orca_qualification, "collect", collect)
    row = qualification._collect_row(tmp_path, pair, arm)
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
