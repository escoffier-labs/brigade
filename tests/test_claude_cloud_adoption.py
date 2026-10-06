"""Offline explicit adoption, local replay, and unavailable lifecycle contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest

from brigade import claude_cloud, cli, cloud_tracker, fleet_client
from brigade.claude_cloud_identity import normalize_claude_cloud_identity

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
REPO = "fixture-owner/fixture-repo"
SHA = "a" * 40
OBSERVE_GITHUB = cloud_tracker.observe_github


@pytest.mark.parametrize("flag", ["--task-id", "--session-id"])
def test_default_cli_adoption_label_discards_url_tracking(tmp_path, capsys, flag):
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "adopt",
                "--target",
                str(tmp_path),
                "--provider",
                "claude-cloud",
                flag,
                "https://claude.ai/code/cse_fixture?token=private-fixture#tracking",
                "--repo",
                REPO,
                "--json",
            ]
        )
        == 0
    )
    entry = json.loads(capsys.readouterr().out)
    assert entry["label"] == "cse_fixture"
    assert "private-fixture" not in cloud_tracker.registry_path(tmp_path).read_text()
    assert "tracking" not in repr(cloud_tracker.status_payload(tmp_path))


@pytest.mark.parametrize("task_id", ["legacy-task", None, "cse_other"])
def test_bound_adoption_refuses_legacy_session_binding(tmp_path, task_id):
    cloud_tracker.register(
        tmp_path,
        provider="claude-cloud",
        task_id="legacy-task" if task_id == "cse_other" else task_id,
        session_id="cse_fixture",
        branch="claude/fixture",
        label="fixture",
        source="adopt-branch",
    )
    if task_id == "cse_other":
        registry = cloud_tracker.load_registry(tmp_path)
        registry["entries"][0]["task_id"] = task_id
        cloud_tracker.save_registry(tmp_path, registry)
    before = cloud_tracker.registry_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="binding conflict|identity conflict"):
        cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id="cse_fixture", repo=REPO)
    assert cloud_tracker.registry_path(tmp_path).read_bytes() == before


def test_legacy_registration_cannot_duplicate_bound_session(tmp_path):
    cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id="cse_fixture", repo=REPO)
    before = cloud_tracker.registry_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="binding conflict"):
        cloud_tracker.register(tmp_path, provider="claude-cloud", task_id="cse_fixture", label="fixture")
    assert cloud_tracker.registry_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("observed_repo", [REPO, "fixture-owner/other", None])
@pytest.mark.parametrize("branch", ["feature/fix", "claude/fixture"])
def test_observer_retains_explicit_refs_and_scopes_branch_evidence(tmp_path, monkeypatch, observed_repo, branch):
    cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id="cse_fixture", repo=REPO, branch=branch, pr="12")

    def read(argv, **kwargs):
        if argv[:3] == ["gh", "repo", "view"]:
            return (0, json.dumps({"nameWithOwner": observed_repo}), "")
        if argv[:2] == ["gh", "api"]:
            return (0, json.dumps([{"name": branch}]), "")
        if argv[:3] == ["gh", "pr", "list"]:
            return (
                0,
                json.dumps(
                    [
                        {
                            "headRefName": branch,
                            "state": "MERGED",
                            "number": 12,
                            "url": f"https://github.com/{observed_repo or 'fixture-owner/other'}/pull/12",
                        }
                    ]
                ),
                "",
            )
        return (1, "", "")

    monkeypatch.setattr(cloud_tracker, "_run_text", read)
    snapshot = OBSERVE_GITHUB(tmp_path)
    row = cloud_tracker.status_payload(tmp_path, github=snapshot)["entries"][0]
    if observed_repo == REPO:
        assert row["artifact_state"] == "landed"
        assert row["evidence"]["github"]["branch_exists"] is True
    else:
        assert row["artifact_state"] == "unobserved"
        assert row["evidence"]["github"]["branch_exists"] is None


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("adoption/status/doctor invoked a provider or holder operation")

    monkeypatch.setattr(cloud_tracker.subprocess, "run", forbidden)
    monkeypatch.setattr(claude_cloud, "list_agents", forbidden)
    for name in ("admit_cloud", "bind_cloud", "renew_cloud", "release_cloud", "fetch_cloud"):
        monkeypatch.setattr(fleet_client, name, forbidden)
    monkeypatch.setattr(cloud_tracker, "observe_github", lambda target: {"branches": [], "prs": []})
    monkeypatch.setattr(cloud_tracker, "jules_cloud_wired", lambda: False)
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


@pytest.mark.parametrize("session", ["session_fixture_A-1", "cse_fixture_B-2"])
def test_schemeless_identity_matches_https(session):
    assert normalize_claude_cloud_identity(f"claude.ai/code/{session}?from=cli#fixture") == (
        normalize_claude_cloud_identity(session)
    )


@pytest.mark.parametrize("session", ["session_fixture_A-1", "cse_fixture_B-2"])
def test_two_local_callers_replay_one_record_and_reject_repo_conflict(tmp_path, session):
    def caller(index):
        if index % 2:
            return cloud_tracker.register(
                tmp_path, provider="claude-cloud", task_id=session, repo=REPO.upper(), label="fixture"
            )
        return cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id=f"claude.ai/code/{session}", repo=REPO)

    with ThreadPoolExecutor(max_workers=2) as pool:
        entries = list(pool.map(caller, range(8)))
    assert len({entry["id"] for entry in entries}) == 1
    assert len(cloud_tracker.load_registry(tmp_path)["entries"]) == 1
    assert entries[0]["session_id"] == session
    assert entries[0]["task_id"] == session
    assert entries[0]["repo"] == REPO
    before = cloud_tracker.registry_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="repository binding conflict"):
        cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id=session, repo="fixture-owner/other")
    assert cloud_tracker.registry_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("command", ["register", "adopt"])
def test_cli_binds_refs_and_replays_without_overwriting_immutable_refs(tmp_path, capsys, command):
    argv = [
        "run",
        "cloud",
        command,
        "--target",
        str(tmp_path),
        "--provider",
        "claude-cloud",
        "--task-id",
        "https://claude.ai/code/cse_fixture?from=cli",
        "--label",
        "fixture",
        "--repo",
        "https://github.com/Fixture-Owner/Fixture-Repo",
        "--branch",
        "claude/fixture",
        "--commit",
        SHA.upper(),
        "--pr",
        f"https://github.com/{REPO}/pull/12",
        "--json",
    ]
    assert cli.main(argv) == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry["repo"] == REPO
    assert entry["commit"] == SHA
    assert entry["pr_url"] == f"https://github.com/{REPO}/pull/12"
    assert entry["session_url"] == "https://claude.ai/code/cse_fixture"
    assert "lease_holder" not in entry
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["id"] == entry["id"]
    argv[argv.index("--commit") + 1] = "b" * 40
    assert cli.main(argv) == 2
    assert "reference conflict" in capsys.readouterr().err
    assert cloud_tracker.load_registry(tmp_path)["entries"][0]["commit"] == SHA


@pytest.mark.parametrize(
    "changes",
    [
        {"repo": "../fixture"},
        {"repo": "fixture-owner/.."},
        {"repo": "https://github.com.evil.invalid/fixture/repo"},
        {"repo": "fixture-owner/repo.git"},
        {"repo": "fixture-owner/repo\n"},
        {"session_id": "agent-local-fixture"},
        {"session_id": "cse_"},
        {"session_id": "claude.ai/code/cse_fixture/extra"},
        {"session_id": "https://claude.ai:443/code/cse_fixture"},
        {"task_id": "session_other"},
        {"commit": "main"},
        {"commit": "a" * 7},
        {"pr": "https://github.com/fixture-owner/other/pull/12"},
        {"pr": "0"},
        {"branch": "claude/../fixture"},
        {"branch": "-unsafe"},
        {"branch": "/claude/fixture"},
        {"branch": "claude/fixture.lock"},
        {"lease_holder": "fixture-holder"},
    ],
)
def test_invalid_binding_is_safe_and_does_not_write(tmp_path, changes):
    kwargs = {"provider": "claude-cloud", "session_id": "cse_fixture", "repo": REPO, "label": "fixture"}
    kwargs.update(changes)
    with pytest.raises(ValueError):
        cloud_tracker.register(tmp_path, **kwargs)
    assert not cloud_tracker.registry_path(tmp_path).exists()


def test_refs_require_explicit_repo_and_session(tmp_path):
    with pytest.raises(ValueError):
        cloud_tracker.adopt(tmp_path, provider="claude-cloud", branch="claude/fixture", repo=REPO)
    with pytest.raises(ValueError):
        cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id="cse_fixture", commit=SHA)


@pytest.mark.parametrize("artifact", ["merged", "missing", "deleted", "branch"])
def test_artifact_never_establishes_provider_lifecycle(tmp_path, artifact):
    entry = cloud_tracker.adopt(
        tmp_path, provider="claude-cloud", session_id="cse_fixture", repo=REPO, branch="claude/fixture", pr="12"
    )
    github = {"branches": ["claude/fixture"] if artifact == "branch" else [], "prs": []}
    if artifact == "merged":
        github["prs"] = [
            {"head": "claude/fixture", "state": "MERGED", "number": 12, "url": f"https://github.com/{REPO}/pull/12"}
        ]
    payload = cloud_tracker.status_payload(
        tmp_path, now=NOW, github=github, provider_tasks={"cse_fixture": {"state": "completed"}}
    )
    row = payload["entries"][0]
    assert row["classification"] == "needs-investigation"
    assert row["provider_state"] is None
    assert row["provider_lifecycle"]["state"] == "unknown"
    assert row["provider_lifecycle"]["last_confirmed_at"] is None
    assert row["provider_lifecycle"]["source"] == "unsupported-provider-status"
    assert row["repo"] == REPO
    assert row["id"] == entry["id"]
    assert row["artifact_state"] == ("landed" if artifact == "merged" else "unobserved")
    assert payload["lifecycle_counts"]["claude-cloud"]["active"] is None
    records = cloud_tracker.center_activity_records(
        tmp_path, now=NOW, github=github, provider_tasks={}, cursor_wired=False
    )
    assert records[0]["state"] == "unknown"
    assert records[0]["provider_lifecycle"]["last_confirmed_at"] is None


def test_poll_does_not_refresh_old_provider_fact_and_preserves_other_evidence(tmp_path):
    entry = cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id="cse_fixture", repo=REPO)
    registry = cloud_tracker.load_registry(tmp_path)
    confirmed = "2026-10-01T12:00:00Z"
    registry["entries"][0].update(
        {
            "provider_lifecycle": {"state": "running", "last_confirmed_at": confirmed},
            "local_continuation": {"state": "observed", "source": "fixture"},
            "lease_evidence": {"state": "expired", "source": "fixture"},
        }
    )
    cloud_tracker.save_registry(tmp_path, registry)
    before = cloud_tracker.registry_path(tmp_path).read_bytes()
    for now in (NOW, NOW + timedelta(hours=24)):
        row = cloud_tracker.status_payload(tmp_path, now=now)["entries"][0]
        lifecycle = row["provider_lifecycle"]
        assert lifecycle["state"] == "unknown"
        assert lifecycle["freshness"] == "stale"
        assert lifecycle["last_confirmed_at"] == confirmed
        assert lifecycle["observed_at"] == now.isoformat().replace("+00:00", "Z")
        assert row["local_continuation"]["state"] == "observed"
        assert row["lease_evidence"]["state"] == "expired"
        assert row["id"] == entry["id"]
    assert cloud_tracker.registry_path(tmp_path).read_bytes() == before


def test_doctor_and_status_are_offline_and_branch_discovery_is_unbound(tmp_path, capsys):
    assert cli.main(["run", "cloud", "doctor", "--target", str(tmp_path), "--provider", "claude-cloud", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "disabled-by-policy"
    assert cli.main(["run", "cloud", "status", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"]["claude-cloud"]["wired"] is False
    payload = cloud_tracker.status_payload(tmp_path, github={"branches": ["claude/fixture"], "prs": []})
    row = payload["entries"][0]
    assert row["task_id"] is None
    assert row.get("session_id") is None
    assert row.get("repo") is None
    assert (
        cloud_tracker.center_activity_records(
            tmp_path, github={"branches": ["claude/fixture"], "prs": []}, provider_tasks={}, cursor_wired=False
        )[0]["state"]
        == "unknown"
    )
    assert not claude_cloud.launch_agent(REPO, "fixture").ok


def test_branch_match_in_another_repository_cannot_establish_artifact_landing(tmp_path):
    cloud_tracker.adopt(tmp_path, provider="claude-cloud", session_id="cse_fixture", repo=REPO, branch="claude/fixture")
    payload = cloud_tracker.status_payload(
        tmp_path,
        github={
            "branches": [],
            "prs": [
                {"head": "claude/fixture", "state": "MERGED", "url": "https://github.com/fixture-owner/other/pull/12"}
            ],
        },
    )
    assert payload["entries"][0]["artifact_state"] == "unobserved"
    assert payload["entries"][0]["pr"] is None


def test_replay_can_add_missing_refs_without_changing_observation_time(tmp_path):
    first = cloud_tracker.register(
        tmp_path, provider="claude-cloud", session_id="cse_fixture", repo=REPO, label="fixture"
    )
    assert first["source"] == "adopt-session"
    assert first["adopted_at"] is not None
    replay = cloud_tracker.adopt(
        tmp_path,
        provider="claude-cloud",
        session_id="cse_fixture",
        repo=REPO,
        branch="claude/fixture",
        commit=SHA,
        pr="12",
    )
    assert replay["id"] == first["id"]
    assert replay["adopted_at"] == first["adopted_at"]
    assert replay["commit"] == SHA
    assert replay["branch"] == "claude/fixture"
    assert replay["pr_url"] == f"https://github.com/{REPO}/pull/12"
