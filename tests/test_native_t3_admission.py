"""Seat admission and workspace capacity gates for governed native T3 claims."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import (
    cli,
    fleet_client,
    fleet_client_policy,
    fleet_model_admission,
    fleet_session_presence,
    native_t3_admission as admission,
    native_t3_capacity as capacity,
    native_t3_dispatch as dispatch,
    native_t3_enrollment as enrollment,
    native_t3_lifecycle as lifecycle,
    proc,
    roster as roster_mod,
    run_dirfd,
    run_journal,
    run_tasks,
    runguard,
)
from tests.test_native_t3_claim import (
    POSIX_ONLY,
    TASK_TEXT,
    THREAD,
    _claim,
    _native,
    _run_dir,
    _terminal,
)


def _git(repo: Path, *args: str) -> None:
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr


def _roster_text(
    *,
    orchestrator: str = "lead",
    seats: tuple[str, ...] = ("coder",),
    targets: tuple[str, ...] | None = None,
    max_workers: int = 4,
    model: str = "launch-model",
    driver: str = "codex",
    agent_cli: str = "codex",
    max_live: dict[str, int] | int | None = None,
    allow_models: tuple[str, ...] | None = None,
) -> str:
    declared = seats if targets is None else targets
    lines = [f'orchestrator = "{orchestrator}"', "", "[limits]", f"max_workers = {max_workers}"]
    if allow_models is not None:
        lines.append("allow_models = [" + ", ".join(json.dumps(item) for item in allow_models) + "]")
    lines.append("")
    for name in sorted({orchestrator, *seats, *declared}):
        lines += [f"[agents.{name}]", f'cli = "{agent_cli}"', f'role = "{name}"', ""]
    for name in sorted(declared):
        limit = max_live.get(name) if isinstance(max_live, dict) else max_live
        lines += [
            f"[t3_targets.{name}]",
            'origin_environment = "env-target"',
            'origin_thread = "thread-parent"',
            'target_environment = "env-target"',
            'target_project = "project-test"',
            'provider_instance = "codex-test"',
            f'driver = "{driver}"',
            f'model = "{model}"',
            'isolation = "worktree"',
        ]
        if limit is not None:
            lines.append(f"max_live = {limit}")
        lines += ["", f"[t3_targets.{name}.options]", 'reasoningEffort = "high"', ""]
    return "\n".join(lines)


def _workspace(tmp_path: Path, **roster_kwargs: object) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "roster.toml").write_text(_roster_text(**roster_kwargs), encoding="utf-8")  # type: ignore[arg-type]
    return repo


def _batch(tmp_path: Path, *, request_id: str, seat: str = "coder", ids: tuple[str, ...] = ("one",)) -> Path:
    payload = {
        "schema": "brigade.run_batch.v1",
        "request_id": request_id,
        "tasks": [
            {"task_id": task_id, "text": f"{TASK_TEXT} {task_id}", "seat": seat, "isolation": "worktree"}
            for task_id in ids
        ],
        "synthesis": None,
    }
    path = tmp_path / f"batch-{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _enroll_run(
    workspace: Path, tmp_path: Path, *, request_id: str, seat: str = "coder", ids: tuple[str, ...] = ("one",)
) -> str:
    report = enrollment.enroll(batch_path=_batch(tmp_path, request_id=request_id, seat=seat, ids=ids), target=workspace)
    return report["run_id"]


def _enroll(tmp_path: Path, *, ids: tuple[str, ...] = ("one",), **roster_kwargs: object) -> tuple[Path, str]:
    workspace = _workspace(tmp_path, **roster_kwargs)
    return workspace, _enroll_run(workspace, tmp_path, request_id="request-1", ids=ids)


def _events(workspace: Path, run_id: str):
    path = _run(workspace, run_id) / "events" / "lifecycle.jsonl"
    return list(run_journal.read_journal_bounded(path).events)


def _claim_code(workspace: Path, run_id: str, task_id: str = "one") -> str | None:
    try:
        _claim(workspace, run_id, task_id)
    except (dispatch.DispatchError, enrollment.EnrollmentError) as exc:
        return exc.code
    return None


def _run(workspace: Path, run_id: str) -> Path:
    return _run_dir(workspace, run_id)


def _write_hub_config() -> Path:
    path = fleet_client.brigade_home() / "fleet.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[fleet]\nhub_url = "https://hub.example.invalid"\n', encoding="utf-8")
    return path


def _policy_document(*, enabled: bool = True) -> dict:
    return {
        "schema": "brigade.fleet_policy.v1",
        "defaults": {},
        "machines": {},
        "seats": {"coder": {"provider": "openai", "model": "launch-model", "enabled": enabled}},
        "consumers": {"brigade-run": {}},
        "repositories": {},
        "routing": {"enabled": False},
    }


def _unreadable_policy() -> dict:
    raise fleet_client_policy.FleetPolicyClientError("network", "unreadable")


@POSIX_ONLY
def test_orchestrator_seat_refuses_a_claim(tmp_path):
    workspace = _workspace(tmp_path, seats=("lead", "coder"), targets=("lead", "coder"))
    run_id = _enroll_run(workspace, tmp_path, request_id="request-lead", seat="lead")
    assert _claim_code(workspace, run_id) == "admission-denied"
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert "seat-orchestrator" in str(refused.value)
    assert not [event for event in _events(workspace, run_id) if event.event_type == dispatch.CLAIM_EVENT]


@POSIX_ONLY
def test_changed_target_declaration_refuses_a_claim(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    roster = workspace / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text(encoding="utf-8").replace('model = "launch-model"', 'model = "other-model"'))
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-denied"
    assert "target-changed" in str(refused.value)


@POSIX_ONLY
def test_retired_model_refuses_a_claim(tmp_path):
    workspace, run_id = _enroll(tmp_path, model="gpt-5.4")
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-denied"
    assert "model-retired" in str(refused.value)


@POSIX_ONLY
def test_uppercase_retired_launch_model_refuses_a_claim(tmp_path):
    workspace, run_id = _enroll(tmp_path, model="GPT-5.4")
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-denied"
    assert "model-retired" in str(refused.value)


@POSIX_ONLY
def test_target_driver_adapter_must_be_allowed(tmp_path):
    workspace, run_id = _enroll(tmp_path, agent_cli="claude", allow_models=("claude",), driver="codex")
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-denied"
    assert "adapter-not-allowed" in str(refused.value)


@POSIX_ONLY
def test_target_driver_must_match_the_seat_provider(tmp_path):
    workspace, run_id = _enroll(tmp_path, agent_cli="claude", allow_models=("claude", "codex"), driver="codex")
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-denied"
    assert "driver-mismatch" in str(refused.value)


@POSIX_ONLY
def test_unknown_target_driver_refuses_a_claim(tmp_path):
    workspace, run_id = _enroll(tmp_path, driver="cursor")
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-denied"
    assert "driver-unknown" in str(refused.value)


def test_last_known_good_authority_refuses_in_the_pure_predicate():
    text = _roster_text()
    roster = roster_mod.load_roster(Path("/nonexistent/roster.toml"), text=text)
    target = enrollment.target_for_seat(roster, "coder", source_revision="a" * 40)
    verdict = admission.evaluate(roster, "coder", target, hub=admission.HubAuthority(state="lkg-only"))
    assert verdict.code == "admission-unavailable" and verdict.reason == "policy-unavailable"


def test_disallowed_adapter_refuses_in_the_pure_predicate():
    text = _roster_text()
    allowed = roster_mod.load_roster(Path("/nonexistent/roster.toml"), text=text)
    target = enrollment.target_for_seat(allowed, "coder", source_revision="a" * 40)
    blocked = replace(allowed, allow_models=("claude",))
    verdict = admission.evaluate(blocked, "coder", target, hub=admission.HubAuthority(state="unenrolled"))
    assert verdict.code == "admission-denied" and verdict.reason == "adapter-not-allowed"
    verdict = admission.evaluate(allowed, "coder", target, hub=admission.HubAuthority(state="unenrolled"))
    assert verdict.admitted and verdict.reason == "local-roster"
    assert verdict.roster_digest.startswith("sha256:") and verdict.policy_digest is None


@POSIX_ONLY
def test_enrolled_node_with_unreadable_hub_policy_refuses(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)
    monkeypatch.setattr(fleet_model_admission, "enrollment_activation_observed", lambda **_: True)
    monkeypatch.setattr(fleet_client_policy, "show_policy", _unreadable_policy)
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-unavailable"
    assert "policy-unavailable" in str(refused.value)


@POSIX_ONLY
def test_configured_hub_without_enrollment_high_water_requires_live_policy(tmp_path, monkeypatch):
    _write_hub_config()
    workspace, run_id = _enroll(tmp_path)
    monkeypatch.setattr(fleet_client_policy, "show_policy", _unreadable_policy)
    assert admission.read_hub_authority(workspace).state == "unavailable"
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-unavailable"
    assert "policy-unavailable" in str(refused.value)


@POSIX_ONLY
def test_hub_url_environment_without_high_water_requires_live_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example.invalid")
    workspace, run_id = _enroll(tmp_path)
    monkeypatch.setattr(fleet_client_policy, "show_policy", _unreadable_policy)
    assert admission.read_hub_authority(workspace).state == "unavailable"
    assert _claim_code(workspace, run_id) == "admission-unavailable"


@POSIX_ONLY
def test_repository_identity_timeout_refuses_with_fixed_tokens(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example.invalid")
    workspace, run_id = _enroll(tmp_path)
    monkeypatch.setattr(fleet_client_policy, "show_policy", lambda: {"document": _policy_document()})

    def timeout(_target: Path) -> None:
        raise subprocess.TimeoutExpired(cmd=["git", "remote"], timeout=5)

    monkeypatch.setattr(fleet_session_presence, "repository_identity", timeout)
    capsys.readouterr()
    assert cli.main(["t3", "claim", run_id, "one", "--parent-thread", THREAD, "--target", str(workspace)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[admission-unavailable]" in captured.err and TASK_TEXT not in captured.err


@POSIX_ONLY
def test_enrolled_node_requires_the_live_hub_seat_verdict(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)
    document = _policy_document(enabled=False)
    monkeypatch.setattr(fleet_model_admission, "enrollment_activation_observed", lambda **_: True)
    monkeypatch.setattr(fleet_client_policy, "show_policy", lambda: {"document": document})
    with pytest.raises(dispatch.DispatchError) as refused:
        _claim(workspace, run_id, "one")
    assert refused.value.code == "admission-denied" and "policy-denied" in str(refused.value)

    document["seats"]["coder"]["enabled"] = True
    granted = _claim(workspace, run_id, "one")
    assert granted["state"] == "claimed"
    claims = [event for event in _events(workspace, run_id) if event.event_type == dispatch.CLAIM_EVENT]
    assert claims[-1].payload["admission"] == "admitted"
    assert claims[-1].payload["roster_digest"].startswith("sha256:")
    assert claims[-1].payload["policy_digest"].startswith("sha256:")


@POSIX_ONLY
def test_two_runs_racing_for_the_last_global_slot_get_one_grant(tmp_path):
    workspace = _workspace(tmp_path, max_workers=1)
    first = _enroll_run(workspace, tmp_path, request_id="request-a")
    second = _enroll_run(workspace, tmp_path, request_id="request-b")

    def attempt(run_id: str) -> str:
        return _claim_code(workspace, run_id) or "granted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (first, second)))
    assert sorted(outcomes) == ["capacity-denied", "granted"]
    assert capacity.governed_occupancy(workspace).live == 1


@POSIX_ONLY
def test_per_seat_max_live_caps_one_seat_only(tmp_path):
    workspace = _workspace(
        tmp_path,
        seats=("coder", "reviewer"),
        max_workers=4,
        max_live={"coder": 1},
    )
    coder_a = _enroll_run(workspace, tmp_path, request_id="request-a", seat="coder")
    coder_b = _enroll_run(workspace, tmp_path, request_id="request-b", seat="coder")
    reviewer = _enroll_run(workspace, tmp_path, request_id="request-c", seat="reviewer")
    assert _claim_code(workspace, coder_a) is None
    assert _claim_code(workspace, coder_b) == "capacity-denied"
    assert _claim_code(workspace, reviewer) is None
    occupancy = capacity.governed_occupancy(workspace)
    assert occupancy.seats == (("coder", 1), ("reviewer", 1))


@POSIX_ONLY
def test_held_and_post_launch_abandoned_tasks_keep_occupying(tmp_path):
    workspace, run_id = _enroll(tmp_path, ids=("one", "two"))
    _claim(workspace, run_id, "one")
    dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_native("one"))
    with pytest.raises(dispatch.DispatchError) as conflict:
        dispatch.accepted(
            workspace=workspace,
            run_id=run_id,
            task_id="one",
            parent_thread=THREAD,
            payload=_native("one", childThreadId="thread-other"),
        )
    assert conflict.value.code == "accept-conflict"
    _claim(workspace, run_id, "two")
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="two", payload=None, abandon=True)
    assert capacity.governed_occupancy(workspace) == capacity.Occupancy(2, (("coder", 2),), True)


@POSIX_ONLY
def test_pre_launch_abandoned_binding_does_not_occupy(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)
    original = run_tasks.claim_task_launch

    def boom(*_args: object, **_kwargs: object):
        raise run_tasks.TaskJournalError("injected failure before the launch claim")

    # The claim binding is durable but the launch claim never was. Abandoning
    # that binding charged and launched nothing, so it occupies nothing.
    monkeypatch.setattr(run_tasks, "claim_task_launch", boom)
    with pytest.raises(dispatch.DispatchError):
        _claim(workspace, run_id, "one")
    monkeypatch.setattr(run_tasks, "claim_task_launch", original)
    dispatch.hold_resolve(workspace=workspace, run_id=run_id, task_id="one", payload=None, abandon=True)
    occupancy = capacity.governed_occupancy(workspace)
    assert occupancy == capacity.Occupancy(0, (), True)


@POSIX_ONLY
def test_terminal_quiescent_result_frees_only_after_ack(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    report = dispatch.accepted(
        workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_terminal("one")
    )
    assert report["result_id"] is not None
    assert capacity.governed_occupancy(workspace).live == 1
    lifecycle.ack(workspace=workspace, run_id=run_id, task_id="one", result_id=report["result_id"])
    assert capacity.governed_occupancy(workspace) == capacity.Occupancy(0, (), True)


@POSIX_ONLY
def test_nonterminal_correlated_task_keeps_occupying(tmp_path):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    dispatch.accepted(workspace=workspace, run_id=run_id, task_id="one", parent_thread=THREAD, payload=_native("one"))
    assert capacity.governed_occupancy(workspace).live == 1


@pytest.mark.parametrize("kind", ["corrupt", "symlink", "journal-deleted", "events-symlink", "kind-rewritten"])
@POSIX_ONLY
def test_unverifiable_run_makes_occupancy_non_authoritative(tmp_path, kind):
    workspace, run_id = _enroll(tmp_path)
    runs = runguard.lock_path(workspace).parent / "runs"
    run_dir = _run(workspace, run_id)
    if kind == "corrupt":
        broken = runs / ("t3-" + "f" * 64)
        broken.mkdir()
        (broken / "run.json").write_text("{not json", encoding="utf-8")
    elif kind == "symlink":
        target = tmp_path / "outside-run"
        target.mkdir()
        try:
            os.symlink(target, runs / ("t3-" + "e" * 64))
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is unavailable on this platform")
    elif kind == "journal-deleted":
        (run_dir / "events" / "lifecycle.jsonl").unlink()
    elif kind == "events-symlink":
        (run_dir / "events").rename(tmp_path / "events-real")
        empty = tmp_path / "empty-events"
        empty.mkdir()
        try:
            os.symlink(empty, run_dir / "events")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is unavailable on this platform")
    else:
        receipt = run_dir / "run.json"
        meta = json.loads(receipt.read_text(encoding="utf-8"))
        meta["kind"] = "ordinary-run"
        receipt.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    occupancy = capacity.governed_occupancy(workspace)
    assert occupancy.authoritative is False and occupancy.live == 0
    assert _claim_code(workspace, run_id) == "capacity-unknown"


@POSIX_ONLY
def test_run_dir_replaced_during_scan_makes_occupancy_non_authoritative(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")
    assert capacity.governed_occupancy(workspace).live == 1
    monkeypatch.setattr(run_dirfd.BoundRunDir, "still_bound", lambda self: False)
    occupancy = capacity.governed_occupancy(workspace)
    assert occupancy.authoritative is False and occupancy.live == 0


@pytest.mark.parametrize("value", ["0", "-1", "true", '"2"'])
def test_max_live_must_be_a_positive_integer(value):
    text = _roster_text().replace('isolation = "worktree"', f'isolation = "worktree"\nmax_live = {value}')
    with pytest.raises(ValueError, match="max_live"):
        roster_mod.load_roster(Path("/nonexistent/roster.toml"), text=text)


@POSIX_ONLY
def test_refusals_have_empty_stdout_and_no_task_text(tmp_path, capsys):
    workspace, run_id = _enroll(tmp_path)
    roster = workspace / ".brigade" / "roster.toml"
    original = roster.read_text(encoding="utf-8")
    roster.write_text(original.replace('model = "launch-model"', 'model = "other-model"'))
    capsys.readouterr()
    assert cli.main(["t3", "claim", run_id, "one", "--parent-thread", THREAD, "--target", str(workspace)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[admission-denied]" in captured.err and TASK_TEXT not in captured.err
    roster.write_text(original, encoding="utf-8")


@POSIX_ONLY
def test_capacity_lock_is_acquired_before_the_run_lock(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)
    order: list[str] = []
    original_capacity = capacity.capacity_lock
    original_owned = enrollment.owned_run
    original_roster = admission.load_roster

    @contextmanager
    def tracked_capacity(*args: object, **kwargs: object):
        order.append("capacity")
        with original_capacity(*args, **kwargs):  # type: ignore[arg-type]
            yield

    @contextmanager
    def tracked_owned(*args: object, **kwargs: object):
        order.append("run")
        with original_owned(*args, **kwargs):  # type: ignore[arg-type]
            yield

    def tracked_roster(*args: object, **kwargs: object):
        order.append("roster")
        return original_roster(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(capacity, "capacity_lock", tracked_capacity)
    monkeypatch.setattr(enrollment, "owned_run", tracked_owned)
    monkeypatch.setattr(admission, "load_roster", tracked_roster)
    _claim(workspace, run_id, "one")
    assert order == ["capacity", "run", "roster"]


@POSIX_ONLY
def test_occupancy_scan_takes_no_run_lock(tmp_path, monkeypatch):
    workspace, run_id = _enroll(tmp_path)
    _claim(workspace, run_id, "one")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the occupancy scan must not take a run lock")

    monkeypatch.setattr(runguard, "run_lock", refuse)
    assert capacity.governed_occupancy(workspace).live == 1


def test_capacity_never_treats_max_workers_as_a_per_seat_allowance(tmp_path):
    text = _roster_text(seats=("coder", "reviewer"), max_workers=2)
    roster = roster_mod.load_roster(Path("/nonexistent/roster.toml"), text=text)
    occupancy = capacity.Occupancy(1, (("coder", 1),), True)
    assert capacity.assess(roster, "coder", occupancy).code == "capacity-available"
    full = capacity.Occupancy(2, (("coder", 2),), True)
    assert capacity.assess(roster, "reviewer", full).code == "capacity-denied"


@POSIX_ONLY
def test_capacity_lock_path_is_workspace_scoped(tmp_path):
    workspace = _workspace(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init")
    _git(other, "config", "user.email", "test@example.invalid")
    _git(other, "config", "user.name", "Test User")
    (other / "tracked.txt").write_text("base\n")
    _git(other, "add", "tracked.txt")
    _git(other, "commit", "-m", "initial")
    assert capacity.capacity_lock_path(workspace) != capacity.capacity_lock_path(other)
    assert capacity.capacity_lock_path(workspace) == runguard.lock_path(workspace).with_name(
        capacity.CAPACITY_LOCK_NAME
    )
