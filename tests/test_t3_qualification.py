"""Synthetic evidence for the #2046 matched experiment."""

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
    else:
        rows[0]["record"]["verified_completion_seconds"] = None
    with pytest.raises(ValueError, match=message):
        qualification.summarize(rows)


@pytest.mark.parametrize("arm", ["t3", "run-batch"])
def test_collects_paired_route_with_bound_results(tmp_path, monkeypatch, arm):
    import hashlib
    import json
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
    }
    save(folder / "expected.json", context)
    turns = {name: {"started_at": "2026-01-01T00:00:02Z", "ended_at": "2026-01-01T00:00:06Z"} for name in ids}
    opened = {"monotonic_ns": 10**9, "wall_ns": evidence.utc("2026-01-01T00:00:01Z")}
    measurements = {
        "schema": evidence.MEASUREMENTS_SCHEMA,
        "run_id": run_dir.name,
        "opened": opened,
        "verified": {"monotonic_ns": 9 * 10**9, "wall_ns": evidence.utc("2026-01-01T00:00:09Z")},
        "turns": turns,
    }
    save(folder / "opened.json", opened)
    save(folder / "measurements.json", measurements)
    (folder / "interventions.jsonl").write_text('"operator action"\n', encoding="utf-8")
    (folder / "errors.jsonl").touch()

    options = [{"id": "reasoningEffort", "value": "low"}, {"id": "serviceTier", "value": "default"}]
    members = []
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
            provider_result_id=f"{name}-child-run",
            artifact_kind="receipt",
            artifact_digest=hashlib.sha256(receipt.read_bytes()).hexdigest(),
            acknowledged=True,
        )
        members.append(task)
        if arm == "t3":
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
                    "latestTerminalRunId": task.original_run_id,
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
                        "worktreePath": str(tmp_path / name),
                        "parentThreadId": context["parent_thread"],
                        "projectId": context["project"],
                        "activeRunId": None,
                    },
                    "recentRuns": [
                        {
                            "runId": task.original_run_id,
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

    monkeypatch.setattr(evidence.run_tasks, "read_task_batch", lambda *_a, **_kw: SimpleNamespace(members=members))
    monkeypatch.setattr(
        evidence.native_child_journal,
        "read_events",
        lambda _: [SimpleNamespace(event_type="task.native-held")],
    )
    monkeypatch.setattr(evidence, "check_native_idle", lambda _: None)
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
    record = orca_qualification.collect(arm, run_dir, folder / "status.json", folder / "measurements.json")
    assert record["all_tasks_terminal"]
    assert record["verified_completion_seconds"] == 8
    assert record["timings_seconds"]["queue"] == 1
    assert record["timings_seconds"]["execution"] == 4
    assert record["timings_seconds"]["delivery"] == 3
    assert record["parent_interventions"] == 2

    if arm == "t3":
        turns["guide"]["started_at"] = "2026-01-01T00:00:04Z"
        save(folder / "measurements.json", measurements)
        with pytest.raises(ValueError, match="retained evidence"):
            orca_qualification.collect(arm, run_dir, folder / "status.json", folder / "measurements.json")


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


def test_reads_direct_turn_and_applied_settings(tmp_path):
    import json

    from brigade import t3_qualification_evidence as evidence

    base, tree = "a" * 40, tmp_path / "worktree"
    turn_start, turn_end = "2026-01-01T00:00:02Z", "2026-01-01T00:00:06Z"
    records = [
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
    (tmp_path / "session.jsonl").write_text("".join(f"{json.dumps(row)}\n" for row in records), encoding="utf-8")
    assert evidence._provider_turn(tmp_path, tree, {"wall_ns": 0}, base) == {
        "started_at": turn_start,
        "ended_at": turn_end,
        "provider_errors": 1,
    }
