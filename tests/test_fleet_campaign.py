"""Offline membership, conflict and authority boundaries for campaign previews."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli


def binding(repo="repo-a", **updates):
    return {
        "repo_id": repo,
        "authority_kind": "task",
        "authority_id": "task-a",
        "authority_fingerprint": "a" * 64,
        "spec_fingerprint": "b" * 64,
        **updates,
    }


def fixture(target: Path, bindings=None, *, disabled=False):
    (target / ".brigade").mkdir(exist_ok=True)
    (target / ".brigade/repos.toml").write_text(
        '[[repo]]\nid="repo-a"\npath="private-repo"\n'
        '[[repo]]\nid="repo-b"\npath="other/private-repo"\n'
        f"enabled={str(not disabled).lower()}\n"
    )
    path = target / "bindings.json"
    path.write_text(json.dumps(bindings if bindings is not None else [binding(), binding("repo-b")]))
    return path


def invoke(target, bindings, *extra):
    return cli.main(
        [
            "fleet",
            "campaign",
            "preview",
            "--target",
            str(target),
            "--campaign-id",
            "campaign-a",
            "--bindings",
            str(bindings),
            "--json",
            *extra,
        ]
    )


def test_cli_offline_selection_and_no_effects(tmp_path, capsys, monkeypatch):
    from brigade import fleet_client, grokbot_jobs
    from brigade.repos_cmd import actions_dispatch

    def forbidden(*args, **kwargs):
        pytest.fail("preview crossed a write or live authority boundary")

    monkeypatch.setattr(fleet_client, "load_fleet_config", forbidden)
    monkeypatch.setattr(fleet_client, "repo_claim", forbidden)
    monkeypatch.setattr(grokbot_jobs, "enqueue", forbidden)
    monkeypatch.setattr(actions_dispatch, "actions_dispatch_apply", forbidden)
    monkeypatch.setattr(actions_dispatch, "_write_actions", forbidden)
    path = fixture(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert invoke(tmp_path, path, "--repos", "repo-b,repo-a,repo-a") == 0
    result = json.loads(capsys.readouterr().out)
    assert [m["repo_id"] for m in result["members"]] == ["repo-a", "repo-b"]
    assert result["summary"] == {"total": 2, "unknown": 2, "terminal": 0, "done": False}
    assert result["resume_candidates"] == ["repo-a", "repo-b"]
    assert result["prior_comparison"] == "not_checked"
    assert all(m["launch_safety"] == "unknown" for m in result["members"])
    assert "private-repo" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_empty_health_command_refuses_without_effects_or_private_echo(tmp_path, capsys):
    path = fixture(tmp_path)
    config = tmp_path / ".brigade/repos.toml"
    config.write_text('[[repo]]\nid="repo-a"\npath="private-repo"\n[[repo.health_command]]\nargv=[]\n')
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert invoke(tmp_path, path) == 2
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "kind": "fleet-campaign-preview",
        "read_only": True,
        "error": "input_refused",
    }
    assert "private" not in output.out + output.err
    assert str(tmp_path) not in output.out + output.err
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_path_resolution_failure_refuses_without_private_echo(tmp_path, capsys, monkeypatch):
    path = fixture(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def refuse_resolution(self, *args, **kwargs):
        raise RuntimeError("Symlink loop from private-repo")

    monkeypatch.setattr(Path, "resolve", refuse_resolution)
    assert invoke(tmp_path, path) == 2
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "kind": "fleet-campaign-preview",
        "read_only": True,
        "error": "input_refused",
    }
    assert "private" not in output.out + output.err
    assert str(tmp_path) not in output.out + output.err
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("ids", ["missing", "repo-b"])
def test_missing_or_disabled_ids_refuse(tmp_path, capsys, ids):
    path = fixture(tmp_path, [binding("repo-b")], disabled=True)
    assert invoke(tmp_path, path, "--repos", ids) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == "repository_selection_refused"
    assert str(tmp_path) not in output.out + output.err


def test_deterministic_membership_and_explicit_prior_conflicts(tmp_path, capsys):
    path = fixture(tmp_path)
    assert invoke(tmp_path, path) == 0
    first = json.loads(capsys.readouterr().out)
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps(first))
    path.write_text(json.dumps([binding("repo-b"), binding()]))
    assert invoke(tmp_path, path, "--prior", str(prior)) == 0
    reordered = json.loads(capsys.readouterr().out)
    assert first["membership_fingerprint"] == reordered["membership_fingerprint"]
    assert reordered["prior_comparison"] == "matched"
    for changed in (
        [binding()],
        [binding(authority_id="task-b"), binding("repo-b")],
        [binding(spec_fingerprint="c" * 64), binding("repo-b")],
        [binding(authority_fingerprint="d" * 64), binding("repo-b")],
    ):
        path.write_text(json.dumps(changed))
        extra = ["--repos", "repo-a"] if len(changed) == 1 else []
        assert invoke(tmp_path, path, "--prior", str(prior), *extra) == 2
        assert json.loads(capsys.readouterr().out)["error"] == "prior_preview_conflict"


@pytest.mark.parametrize("change", ["campaign", "fingerprint", "binding"])
def test_prior_cannot_rebind_or_hide_tampering(tmp_path, capsys, change):
    path = fixture(tmp_path)
    assert invoke(tmp_path, path) == 0
    prior = json.loads(capsys.readouterr().out)
    if change == "campaign":
        prior["campaign_id"] = "campaign-b"
    elif change == "fingerprint":
        prior["membership_fingerprint"] = "f" * 64
    else:
        prior["bindings"][0]["authority_id"] = "task-other"
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(json.dumps(prior))
    assert invoke(tmp_path, path, "--prior", str(prior_path)) == 2
    assert "prior_preview" in json.loads(capsys.readouterr().out)["error"]


def test_explicit_null_prior_cannot_skip_comparison(tmp_path, capsys):
    path = fixture(tmp_path)
    prior = tmp_path / "prior.json"
    prior.write_text("null")
    assert invoke(tmp_path, path, "--prior", str(prior)) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "prior_preview_refused"


@pytest.mark.parametrize(
    "bad",
    [
        [binding(), binding()],
        [binding(), binding(authority_id="task-b")],
        [binding(authority_id="/private/task")],
        [binding(prompt="private prompt")],
        [binding(spec_fingerprint="not-a-fingerprint")],
        [binding(authority_kind="session")],
        [binding(f"repo-{i}") for i in range(257)],
    ],
)
def test_invalid_bindings_are_bounded_and_redacted(tmp_path, capsys, bad):
    path = fixture(tmp_path, bad)
    assert invoke(tmp_path, path) == 2
    output = capsys.readouterr()
    assert "private" not in output.out + output.err
    assert "error" in json.loads(output.out)


@pytest.mark.parametrize("status", ["done", "cancelled", "dismissed", "pending", "unknown"])
@pytest.mark.parametrize("condition", ["complete", "stale", "incomplete", "mismatch", "conflict"])
def test_supplied_observations_never_complete_or_exclude(tmp_path, capsys, status, condition):
    path = fixture(tmp_path, [binding()])
    observation = {
        "repo_id": "repo-a",
        "authority_id": "task-a",
        "authority_fingerprint": "a" * 64,
        "task_status": status,
        "execution_status": "succeeded",
        "attempt_id": "attempt-a",
        "fresh": condition != "stale",
        "complete": condition != "incomplete",
        "job_key": "job-a",
        "artifact_key": "artifact-a",
    }
    if condition == "mismatch":
        observation["authority_id"] = "task-other"
    records = [observation, dict(observation)]
    if condition == "conflict":
        records.append({**observation, "task_status": "blocked", "execution_status": "failed"})
    observations = tmp_path / "observations.json"
    observations.write_text(json.dumps(records))
    assert invoke(tmp_path, path, "--repos", "repo-a", "--observations", str(observations)) == 0
    result = json.loads(capsys.readouterr().out)
    member = result["members"][0]
    assert member["task_state"] == "unknown"
    assert member["observation_provenance"] == "supplied_unverified"
    assert member["observation_count"] == (2 if condition == "conflict" else 1)
    assert result["resume_candidates"] == ["repo-a"]
    assert result["summary"] == {"total": 1, "unknown": 1, "terminal": 0, "done": False}
    assert member["evidence_condition"] == ("unverified" if condition == "complete" else condition)


def test_execution_failure_multiple_attempts_stay_evidence(tmp_path, capsys):
    path = fixture(tmp_path, [binding()])
    records = [
        {
            "repo_id": "repo-a",
            "authority_id": "task-a",
            "authority_fingerprint": "a" * 64,
            "task_status": "unknown",
            "execution_status": status,
            "attempt_id": f"attempt-{i}",
            "fresh": True,
            "complete": True,
            "job_key": "job-a",
            "artifact_key": None,
        }
        for i, status in enumerate(["failed", "succeeded"])
    ]
    observations = tmp_path / "observations.json"
    observations.write_text(json.dumps(records))
    assert invoke(tmp_path, path, "--repos", "repo-a", "--observations", str(observations)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["total"] == 1
    assert result["summary"]["done"] is False
    assert result["members"][0]["evidence_condition"] == "unverified"
    assert result["members"][0]["observation_count"] == 2


def test_private_or_oversized_inputs_refuse_without_echo(tmp_path, capsys):
    path = fixture(tmp_path)
    observations = tmp_path / "observations.json"
    for data in ([{"prompt": "private secret"}], [binding()] * 1025):
        observations.write_text(json.dumps(data))
        assert invoke(tmp_path, path, "--observations", str(observations)) == 2
        output = capsys.readouterr()
        assert "private" not in output.out + output.err
    path.write_text(" " * (256 * 1024 + 1))
    assert invoke(tmp_path, path) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "input_refused"


def test_preview_help_is_explicitly_read_only(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["fleet", "campaign", "preview", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out.lower()
    assert "read-only" in help_text
    assert "json" in help_text
    assert "dispatch" in help_text
