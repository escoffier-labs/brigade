"""Conductor-launched Claude cloud routine sessions: register, adopt, status and launch refusal."""

from datetime import datetime, timedelta, timezone
from functools import partial
import json

import pytest

from brigade import claude_cloud_routine, cli, cloud_tracker, fleet_client

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
REPO = "fixture-owner/fixture-repo"
ROUTINE = "trig_fixture01"
SESSION = "cse_fixture01"
SESSION_URL = "https://claude.ai/code/session_fixture01"
BRANCH = "claude/fixture-issue-7"
PR_URL = f"https://github.com/{REPO}/pull/12"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Tracking must never launch, admit a hub lane slot or reach a provider."""

    def forbidden(operation, *args, **kwargs):
        pytest.fail(f"routine tracking invoked {operation}")

    monkeypatch.setattr(cloud_tracker.subprocess, "run", partial(forbidden, "subprocess.run"))
    for name in ("admit_cloud", "bind_cloud", "renew_cloud", "release_cloud", "fetch_cloud"):
        monkeypatch.setattr(fleet_client, name, partial(forbidden, f"fleet_client.{name}"))
    monkeypatch.setattr(cloud_tracker, "observe_github", lambda target: {"branches": [], "prs": []})
    from brigade import grokbot_jobs

    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: False)
    original = cloud_tracker.observe_provider
    monkeypatch.setattr(
        cloud_tracker,
        "observe_provider",
        lambda provider, target: (
            original(provider, target)
            if provider == "claude-cloud"
            else cloud_tracker.ProviderObservation(False, False, "unconfigured", {})
        ),
    )


def _github(state=None):
    prs = [{"head": BRANCH, "state": state, "number": 12, "url": PR_URL}] if state else []
    return {"repo": REPO, "branches": [BRANCH] if state else [], "branches_complete": True, "prs": prs}


def _register(tmp_path, **overrides):
    fields = {
        "provider": "claude-cloud",
        "label": "issue-7",
        "session_id": SESSION,
        "repo": REPO,
        "routine_id": ROUTINE,
        "issue": "7",
        "session_url": SESSION_URL,
        "dispatched_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    fields.update(overrides)
    return cloud_tracker.register(tmp_path, **fields)


def _row(tmp_path, *, state=None, now=NOW):
    payload = cloud_tracker.status_payload(tmp_path, now=now, github=_github(state))
    return payload["entries"][0], payload


def _set_report(tmp_path, word, at):
    registry = cloud_tracker.load_registry(tmp_path)
    registry["entries"][0]["provider_lifecycle"] = {
        "state": word,
        "source": claude_cloud_routine.LIFECYCLE_SOURCE,
        "last_confirmed_at": at.isoformat().replace("+00:00", "Z"),
    }
    cloud_tracker.save_registry(tmp_path, registry)


def test_cli_register_records_routine_session_without_a_lane_slot(tmp_path, capsys):
    argv = ["run", "cloud", "register", "--target", str(tmp_path), "--provider", "claude-cloud"]
    argv += ["--label", "issue-7", "--session-id", SESSION, "--repo", REPO, "--routine-id", ROUTINE]
    argv += ["--issue", f"https://github.com/{REPO}/issues/7", "--session-url", SESSION_URL + "?utm=x", "--json"]

    assert cli.main(argv) == 0
    entry = json.loads(capsys.readouterr().out)

    assert entry["provider"] == "claude-cloud"
    assert entry["task_id"] == entry["session_id"] == SESSION
    assert entry["session_url"] == SESSION_URL
    assert entry["routine_id"] == ROUTINE
    assert entry["issue"] == 7
    assert entry["repo"] == REPO
    assert entry["source"] == "adopt-session"
    assert "lease_holder" not in entry
    assert "utm" not in cloud_tracker.registry_path(tmp_path).read_text()

    row, payload = _row(tmp_path)
    assert row["routine_id"] == ROUTINE and row["issue"] == 7 and row["session_url"] == SESSION_URL
    assert payload["sources"]["claude-cloud"]["authority"] == "conductor-routed"
    assert "disabled-by-policy" not in json.dumps(payload)


def test_cli_adopt_adds_branch_pr_and_observed_status_to_the_same_row(tmp_path, capsys):
    first = _register(tmp_path)
    argv = ["run", "cloud", "adopt", "--target", str(tmp_path), "--provider", "claude-cloud"]
    argv += ["--session-id", SESSION, "--repo", REPO, "--routine-id", ROUTINE, "--branch", BRANCH]
    argv += ["--pr", "12", "--observed-status", "Completed", "--json"]

    assert cli.main(argv) == 0
    entry = json.loads(capsys.readouterr().out)

    assert entry["id"] == first["id"]
    assert entry["branch"] == BRANCH and entry["pr_url"] == PR_URL
    assert entry["session_url"] == SESSION_URL
    assert entry["provider_lifecycle"]["state"] == "completed"
    assert entry["provider_lifecycle"]["source"] == "conductor-reported"
    assert len(cloud_tracker.load_registry(tmp_path)["entries"]) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"routine_id": "trig_other"},
        {"issue": "8"},
        {"session_url": "https://claude.ai/code/session_other"},
    ],
)
def test_replay_refuses_a_different_routine_issue_or_url(tmp_path, fields):
    _register(tmp_path)
    before = cloud_tracker.registry_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="Claude reference conflict"):
        cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id=SESSION, repo=REPO, **fields)
    assert cloud_tracker.registry_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("routine_id", "trig_"),
        ("routine_id", "cse_fixture01"),
        ("routine_id", "trig_bad id"),
        ("routine_id", "trig_" + "a" * 129),
        ("routine_id", "https://claude.ai/code/routines/trig_fixture01"),
        ("session_id", "trig_fixture01"),
        ("session_id", "sess_fixture01"),
        ("session_id", "cse_"),
        ("session_url", "https://claude.example/code/session_fixture01"),
        ("session_url", "https://claude.ai/chat/session_fixture01"),
        ("issue", "0"),
        ("issue", "seven"),
        ("issue", "https://github.com/other-owner/other-repo/issues/7"),
        ("observed_status", "done now"),
        ("observed_status", "x" * 33),
        ("observed_status", ""),
    ],
)
def test_malformed_ids_are_refused_without_echo_or_write(tmp_path, field, value):
    with pytest.raises(ValueError) as refused:
        _register(tmp_path, **{field: value})
    assert value == "" or value not in str(refused.value)
    assert not cloud_tracker.registry_path(tmp_path).exists()


@pytest.mark.parametrize("session_id", [SESSION, "session_fixture01", SESSION_URL])
def test_documented_session_id_forms_are_accepted(tmp_path, session_id):
    entry = _register(tmp_path, session_id=session_id, session_url=None)
    assert entry["session_id"] in {SESSION, "session_fixture01"}
    assert entry["session_url"] == f"https://claude.ai/code/{entry['session_id']}"


def test_routine_fields_need_a_claude_repository_binding(tmp_path):
    with pytest.raises(ValueError, match="require --provider claude-cloud and --repo"):
        cloud_tracker.register(tmp_path, provider="cursor-cloud", task_id="bc-1", label="x", routine_id=ROUTINE)
    with pytest.raises(ValueError, match="require --provider claude-cloud and --repo"):
        _register(tmp_path, repo=None)
    with pytest.raises(ValueError, match="requires --routine-id"):
        _register(tmp_path, routine_id=None, observed_status="running")
    assert not cloud_tracker.registry_path(tmp_path).exists()


@pytest.mark.parametrize(
    ("pr_state", "reported", "hours", "classification", "provider_state", "artifact"),
    [
        (None, None, 1, "pending", None, "unobserved"),
        (None, None, 7, "needs-investigation", None, "unobserved"),
        (None, "running", 1, "pending", "running", "unobserved"),
        ("OPEN", "running", 1, "pending", "running", "pr-open"),
        ("OPEN", None, 1, "ready-to-land", None, "pr-open"),
        ("OPEN", "completed", 1, "ready-to-land", "completed", "pr-open"),
        ("OPEN", None, 7, "stale", None, "pr-open"),
        ("MERGED", None, 1, "landed", None, "landed"),
        ("MERGED", "failed", 1, "landed", "failed", "landed"),
        (None, "completed", 1, "needs-investigation", "completed", "unobserved"),
        (None, "failed", 1, "needs-investigation", "failed", "unobserved"),
    ],
)
def test_status_comes_from_github_and_the_conductor_report(
    tmp_path, pr_state, reported, hours, classification, provider_state, artifact
):
    _register(tmp_path, branch=BRANCH, pr="12")
    now = NOW + timedelta(hours=hours)
    if reported:
        _set_report(tmp_path, reported, now - timedelta(minutes=5))

    row, payload = _row(tmp_path, state=pr_state, now=now)

    assert row["classification"] == classification
    assert row["provider_state"] == provider_state
    assert row["artifact_state"] == artifact
    assert row["evidence"]["provider"]["wired"] is False
    lifecycle = row["provider_lifecycle"]
    if reported:
        assert lifecycle["state"] == reported and lifecycle["freshness"] == "fresh"
        assert lifecycle["source"] == "conductor-reported"
        assert payload["lifecycle_counts"]["claude-cloud"] == {
            "active": None,
            "unknown": 0,
            "reported": 1,
            "coverage": "unavailable",
        }
    else:
        assert lifecycle["state"] == "unknown" and lifecycle["source"] == "unsupported-provider-status"
        assert payload["lifecycle_counts"]["claude-cloud"]["unknown"] == 1


def test_a_conductor_report_ages_out_and_polling_never_refreshes_it(tmp_path):
    _register(tmp_path)
    _set_report(tmp_path, "running", NOW)
    before = cloud_tracker.registry_path(tmp_path).read_bytes()

    fresh, _ = _row(tmp_path, now=NOW + timedelta(hours=1))
    aged, _ = _row(tmp_path, now=NOW + timedelta(hours=7))

    assert fresh["provider_state"] == "running" and fresh["classification"] == "pending"
    assert aged["provider_state"] is None
    assert aged["provider_lifecycle"]["state"] == "unknown"
    assert aged["provider_lifecycle"]["reported_state"] == "running"
    assert aged["provider_lifecycle"]["reason"] == "report-stale"
    assert aged["classification"] == "needs-investigation"
    assert cloud_tracker.registry_path(tmp_path).read_bytes() == before


def test_rows_without_a_routine_keep_the_unknown_lifecycle_contract(tmp_path):
    cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id=SESSION, repo=REPO, branch=BRANCH, pr="12")
    _set_report(tmp_path, "completed", NOW)

    row, _ = _row(tmp_path, state="MERGED")

    assert row["classification"] == "needs-investigation"
    assert row["provider_state"] is None
    assert row["provider_lifecycle"]["state"] == "unknown"
    assert row["artifact_state"] == "landed"


def test_sync_and_center_read_a_routine_row_without_hub_calls(tmp_path):
    _register(tmp_path, branch=BRANCH, pr="12", observed_status="running")
    registry = cloud_tracker.load_registry(tmp_path)
    confirmed = datetime.fromisoformat(
        registry["entries"][0]["provider_lifecycle"]["last_confirmed_at"][:-1] + "+00:00"
    )
    now = confirmed + timedelta(minutes=1)

    sync = cloud_tracker.sync_payload(tmp_path, now=now, github=_github("OPEN"), hub_leases=[])
    records = cloud_tracker.center_activity_records(
        tmp_path, now=now, github=_github("MERGED"), provider_tasks={}, cursor_wired=False
    )

    assert sync["counts"] == {"active": 1, "renewed": 0, "released": 0, "needs_you": 0}
    assert sync["active"][0]["routine_id"] == ROUTINE
    assert sync["sources"]["claude-cloud"]["authority"] == "conductor-routed"
    assert records[0]["state"] == "succeeded"
    assert records[0]["routine_id"] == ROUTINE and records[0]["issue"] == 7


@pytest.mark.parametrize("as_json", [True, False])
def test_launch_refusal_names_the_conductor_route_and_reads_nothing(tmp_path, capsys, as_json):
    prompt = tmp_path / "missing-prompt.md"
    argv = ["run", "cloud", "launch", "--target", str(tmp_path), "--provider", "claude-cloud"]
    argv += ["--repo", REPO, "--label", "issue-7", "--prompt-file", str(prompt)]

    assert cli.main(argv + (["--json"] if as_json else [])) == 2
    captured = capsys.readouterr()

    if as_json:
        payload = json.loads(captured.out)
        assert payload["ok"] is False
        assert payload["reason"] == "conductor-routed-launch"
        assert payload["runbook"] == "docs/claude-cloud-capabilities.md#conductor-runbook"
        text = payload["detail"]
    else:
        text = captured.err
        assert "error: conductor-routed-launch" in text
    assert "conductor launches them through one-time Claude Code routines" in text
    assert "docs/claude-cloud-capabilities.md#conductor-runbook" in text
    assert "brigade run cloud register --provider claude-cloud" in text
    assert not cloud_tracker.registry_path(tmp_path).exists()


@pytest.mark.parametrize("as_json", [True, False])
def test_launch_refusal_spawns_nothing_when_the_windows_platform_probe_falls_back(
    tmp_path, capsys, monkeypatch, as_json
):
    """On Windows ``platform.system()`` runs the ``ver`` command when its WMI query fails."""
    import platform
    import subprocess

    from brigade import component_paths

    def windows_probe():
        subprocess.run("ver", shell=True, check=False)  # the offline fixture forbids this
        return "Windows"

    monkeypatch.setattr(platform, "system", windows_probe)
    monkeypatch.setattr(component_paths, "_HOST_IS_WINDOWS", True, raising=False)
    monkeypatch.chdir(tmp_path)  # a Windows-form data root is a relative name on this host
    argv = ["run", "cloud", "launch", "--target", str(tmp_path), "--provider", "claude-cloud"]
    argv += ["--repo", REPO, "--label", "issue-7", "--prompt-file", str(tmp_path / "missing-prompt.md")]

    assert cli.main(argv + (["--json"] if as_json else [])) == 2
    captured = capsys.readouterr()

    assert "conductor-routed-launch" in (captured.out if as_json else captured.err)
    assert not cloud_tracker.registry_path(tmp_path).exists()


def test_doctor_passes_offline_and_canary_reports_no_inventory(tmp_path, capsys):
    assert cli.main(["run", "cloud", "doctor", "--target", str(tmp_path), "--provider", "claude-cloud", "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert cli.main(["run", "cloud", "canary", "--target", str(tmp_path), "--provider", "claude-cloud", "--json"]) == 1
    canary = json.loads(capsys.readouterr().out)

    assert doctor["ok"] is True and doctor["reason"] == "conductor-routed"
    assert doctor["launch"] == "conductor-routine"
    assert doctor["runbook"] == claude_cloud_routine.RUNBOOK
    assert canary["ok"] is False and canary["task_count"] == 0 and canary["apply"] is False
