"""Codex observation budgets keep registered tasks visible in work briefs."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import cloud_codex_probes, cloud_tracker, codex_cloud


NOW = datetime(2026, 8, 12, 20, tzinfo=timezone.utc)


@pytest.fixture
def isolated_providers(monkeypatch):
    empty = cloud_tracker.ProviderObservation(False, False, "unconfigured", {})
    monkeypatch.setattr(cloud_tracker, "_cursor_cloud_observation", lambda target: empty)
    monkeypatch.setattr(cloud_tracker, "_jules_cloud_observation", lambda target: empty)
    monkeypatch.setattr(cloud_tracker, "_grokbot_cloud_observation", lambda: empty)
    monkeypatch.setattr(cloud_tracker, "observe_github", lambda target: {"branches": [], "prs": []})


def _register(target: Path, task_id: str, *, provider: str = "codex-cloud", hours_ago: float = 12) -> None:
    cloud_tracker.register(
        target,
        provider=provider,
        task_id=task_id,
        label="registered task",
        prompt_hash="sha256:" + "a" * 64,
        dispatched_at=(NOW - timedelta(hours=hours_ago)).isoformat(),
    )


def test_brief_limits_inventory_and_checks_only_missing_registered_tasks(monkeypatch, tmp_path, isolated_providers):
    for task_id in ("task_present", "task_missing"):
        _register(tmp_path, task_id)
    _register(tmp_path, "agent_other", provider="cursor-cloud")
    limits = []
    status_ids = []

    def list_tasks(
        *, cwd, max_items=codex_cloud.DEFAULT_MAX_ITEMS, max_pages=codex_cloud.MAX_LIST_PAGES, command_timeout=None
    ):
        assert cwd == tmp_path
        limits.append((max_items, max_pages, command_timeout))
        return codex_cloud.ListTasksResult([{"id": "task_present", "state": "running"}])

    def run_text(command, *, cwd, timeout=30.0):
        assert command[:3] == ["codex", "cloud", "status"]
        assert cwd == tmp_path
        status_ids.append(command[3])
        return 0, "[RUNNING] registered task", ""

    monkeypatch.setattr(codex_cloud, "list_tasks", list_tasks)
    monkeypatch.setattr(cloud_tracker, "_run_text", run_text)

    result = cloud_tracker.stale_entries_for_brief(tmp_path, now=NOW)

    assert "error" not in result
    assert limits == [(codex_cloud.MAX_LIST_LIMIT, 1, cloud_codex_probes.BRIEF_CODEX_BUDGET_SECONDS)]
    assert status_ids == ["task_missing"]


def test_brief_reports_stale_ready_task_outside_first_page(monkeypatch, tmp_path, isolated_providers):
    _register(tmp_path, "task_old_ready")
    cloud_tracker.set_stale_ready_hours(tmp_path, 6)
    monkeypatch.setattr(
        codex_cloud,
        "list_tasks",
        lambda **kwargs: codex_cloud.ListTasksResult([{"id": "task_newer", "state": "running"}]),
    )
    monkeypatch.setattr(cloud_tracker, "_run_text", lambda *a, **k: (0, "[READY] registered task", ""))

    result = cloud_tracker.stale_entries_for_brief(tmp_path, now=NOW)

    assert result["stale_count"] == 1
    row = result["stale_entries"][0]
    assert row["task_id"] == "task_old_ready"
    assert row["provider_state"] == "ready"
    assert row["classification"] == "stale"


@pytest.mark.parametrize("bounded", [False, True])
def test_nonzero_codex_status_with_output_still_reports_state(monkeypatch, tmp_path, bounded):
    # `codex cloud status` exits 1 for tasks in an error state but still prints `[ERROR] <title>`.
    _register(tmp_path, "task_errored")
    monkeypatch.setattr(codex_cloud, "list_tasks", lambda **kwargs: codex_cloud.ListTasksResult([]))
    monkeypatch.setattr(cloud_tracker, "_run_text", lambda *a, **k: (1, "[ERROR] Add a feature", ""))
    kwargs = {"codex_max_items": codex_cloud.MAX_LIST_LIMIT} if bounded else {}

    observation = cloud_tracker.observe_provider("codex-cloud", tmp_path, **kwargs)

    assert observation.tasks == {"task_errored": {"state": "error", "ready_at": None}}


@pytest.mark.parametrize("bounded", [False, True])
def test_failed_codex_status_without_output_supplies_no_evidence(monkeypatch, tmp_path, bounded):
    _register(tmp_path, "task_unobserved")
    monkeypatch.setattr(codex_cloud, "list_tasks", lambda **kwargs: codex_cloud.ListTasksResult([]))
    monkeypatch.setattr(cloud_tracker, "_run_text", lambda *a, **k: (1, "", ""))
    kwargs = {"codex_max_items": codex_cloud.MAX_LIST_LIMIT} if bounded else {}

    observation = cloud_tracker.observe_provider("codex-cloud", tmp_path, **kwargs)

    assert observation.tasks == {}
    assert (observation.configured, observation.reachable, observation.reason) == (True, True, None)


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize(
    "diagnostic",
    [
        "Error: http error: get_task_details failed: GET https://example.invalid/tasks/task_x failed: 404 Not Found",
        "Error: authentication failed",
    ],
)
def test_failed_codex_status_diagnostic_is_not_a_task_state(monkeypatch, tmp_path, bounded, diagnostic):
    _register(tmp_path, "task_unobserved")
    monkeypatch.setattr(codex_cloud, "list_tasks", lambda **kwargs: codex_cloud.ListTasksResult([]))
    monkeypatch.setattr(cloud_tracker, "_run_text", lambda *a, **k: (1, "", diagnostic))
    kwargs = {"codex_max_items": codex_cloud.MAX_LIST_LIMIT} if bounded else {}

    observation = cloud_tracker.observe_provider("codex-cloud", tmp_path, **kwargs)

    assert observation.tasks == {}


def test_brief_probes_only_the_newest_registered_tasks(monkeypatch, tmp_path):
    for index in range(cloud_codex_probes.BRIEF_STATUS_PROBE_LIMIT + 3):
        _register(tmp_path, f"task_{index:02d}", hours_ago=index + 1)
    monkeypatch.setattr(codex_cloud, "list_tasks", lambda **kwargs: codex_cloud.ListTasksResult([]))
    probed: list[str] = []

    def fake_run(command, *, cwd=None, timeout=30.0):
        probed.append(command[-1])
        return 0, "[READY] task", ""

    monkeypatch.setattr(cloud_tracker, "_run_text", fake_run)

    observation = cloud_tracker.observe_provider("codex-cloud", tmp_path, codex_max_items=codex_cloud.MAX_LIST_LIMIT)

    newest = [f"task_{index:02d}" for index in range(cloud_codex_probes.BRIEF_STATUS_PROBE_LIMIT)]
    assert probed == newest
    assert sorted(observation.tasks) == newest


def test_brief_probe_cap_counts_each_task_once(monkeypatch, tmp_path):
    for _ in range(cloud_codex_probes.BRIEF_STATUS_PROBE_LIMIT):
        _register(tmp_path, "task_duplicated", hours_ago=1)
    _register(tmp_path, "task_other_ready", hours_ago=48)
    monkeypatch.setattr(codex_cloud, "list_tasks", lambda **kwargs: codex_cloud.ListTasksResult([]))
    probed: list[str] = []

    def fake_run(command, *, cwd=None, timeout=30.0):
        probed.append(command[-1])
        return 0, "[READY] task", ""

    monkeypatch.setattr(cloud_tracker, "_run_text", fake_run)

    observation = cloud_tracker.observe_provider("codex-cloud", tmp_path, codex_max_items=codex_cloud.MAX_LIST_LIMIT)

    assert probed == ["task_duplicated", "task_other_ready"]
    assert sorted(observation.tasks) == ["task_duplicated", "task_other_ready"]


def test_brief_inventory_and_probes_share_one_deadline(monkeypatch, tmp_path):
    for index in range(3):
        _register(tmp_path, f"task_{index:02d}", hours_ago=index + 1)
    budget = cloud_codex_probes.BRIEF_CODEX_BUDGET_SECONDS
    clock = [1000.0]
    monkeypatch.setattr(cloud_codex_probes.time, "monotonic", lambda: clock[0])
    list_timeouts: list[float] = []

    def slow_list(**kwargs):
        list_timeouts.append(kwargs["command_timeout"])
        clock[0] += budget / 3
        return codex_cloud.ListTasksResult([])

    probe_timeouts: list[float] = []

    def slow_run(command, *, cwd=None, timeout=30.0):
        probe_timeouts.append(timeout)
        clock[0] += budget / 2
        return 0, "[READY] task", ""

    monkeypatch.setattr(codex_cloud, "list_tasks", slow_list)
    monkeypatch.setattr(cloud_tracker, "_run_text", slow_run)

    observation = cloud_tracker.observe_provider("codex-cloud", tmp_path, codex_max_items=codex_cloud.MAX_LIST_LIMIT)

    # The page used a third of the budget, so the probes split the rest and stop at the deadline.
    assert list_timeouts == [budget]
    assert probe_timeouts == pytest.approx([budget * 2 / 3, budget / 6])
    assert sorted(observation.tasks) == ["task_00", "task_01"]


def test_default_status_fallback_probes_every_registered_task(monkeypatch, tmp_path):
    for index in range(cloud_codex_probes.BRIEF_STATUS_PROBE_LIMIT + 2):
        _register(tmp_path, f"task_{index:02d}", hours_ago=index + 1)
    monkeypatch.setattr(codex_cloud, "list_tasks", lambda **kwargs: codex_cloud.ListTasksResult([]))
    monkeypatch.setattr(cloud_tracker, "_run_text", lambda *a, **k: (0, "[READY] task", ""))

    observation = cloud_tracker.observe_provider("codex-cloud", tmp_path)

    assert len(observation.tasks) == cloud_codex_probes.BRIEF_STATUS_PROBE_LIMIT + 2


@pytest.mark.parametrize("entrypoint", ["observe_provider", "observe_provider_details", "observe_providers"])
def test_default_observation_keeps_full_inventory(monkeypatch, tmp_path, isolated_providers, entrypoint):
    _register(tmp_path, "task_outside_inventory")
    limits = []

    def list_tasks(
        *, cwd, max_items=codex_cloud.DEFAULT_MAX_ITEMS, max_pages=codex_cloud.MAX_LIST_PAGES, command_timeout=None
    ):
        limits.append((max_items, max_pages, command_timeout))
        return codex_cloud.ListTasksResult([{"id": "task_inventory", "state": "running"}])

    def unexpected_status(*a, **k):
        pytest.fail("nonempty default inventory must not trigger registered-task status calls")

    monkeypatch.setattr(codex_cloud, "list_tasks", list_tasks)
    monkeypatch.setattr(cloud_tracker, "_run_text", unexpected_status)
    if entrypoint == "observe_provider":
        tasks = cloud_tracker.observe_provider("codex-cloud", tmp_path).tasks
    elif entrypoint == "observe_provider_details":
        observations, _ = cloud_tracker.observe_provider_details(tmp_path, ignored_legacy_option=True)
        tasks = observations["codex-cloud"].tasks
    else:
        tasks, _, _ = cloud_tracker.observe_providers(tmp_path, ignored_legacy_option=True)

    assert limits == [(codex_cloud.DEFAULT_MAX_ITEMS, codex_cloud.MAX_LIST_PAGES, codex_cloud.DEFAULT_LIST_TIMEOUT)]
    assert tasks == {"task_inventory": {"state": "running", "ready_at": None}}
