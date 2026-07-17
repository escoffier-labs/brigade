import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brigade import cli, friction_cmd, repos_cmd


def _write_config(workspace: Path, repos: list[tuple[str, str, Path]]) -> None:
    lines: list[str] = []
    for repo_id, label, path in repos:
        lines.extend(
            [
                "[[repo]]",
                f'id = "{repo_id}"',
                f'label = "{label}"',
                f'path = "{path.relative_to(workspace)}"',
                "enabled = true",
                "",
            ]
        )
    config = workspace / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines))


def _write_note(repo: Path, name: str, text: str) -> Path:
    path = repo / "notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_repos_friction_scan_aggregates_recurrence_and_survives_repo_failure(tmp_path, capsys):
    alpha = tmp_path / "private-alpha"
    beta = tmp_path / "private-beta"
    missing = tmp_path / "private-missing"
    alpha.mkdir()
    beta.mkdir()
    _write_config(
        tmp_path,
        [
            ("alpha", "service alpha", alpha),
            ("beta", "service beta", beta),
            ("missing", "service missing", missing),
        ],
    )
    for repo in (alpha, beta):
        _write_note(
            repo,
            "network.log",
            "connection refused by the MCP transport\nMCP transport timed out during the same doctor command\n",
        )
        _write_note(repo, "other-network.log", "request timed out while contacting the compiler cache\n")
        _write_note(repo, "passing.log", "test result: ok. 27 passed; 0 failed\n")

    rc = cli.main(
        [
            "repos",
            "friction",
            "scan",
            "--target",
            str(tmp_path),
            "--json",
        ]
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "partial"
    assert payload["repo_count"] == 3
    assert payload["completed_repo_count"] == 2
    assert payload["failed_repo_count"] == 1
    assert payload["signature_count"] == 2
    signature = next(item for item in payload["signatures"] if item["occurrence_count"] == 4)
    assert signature["friction_type"] == "network_timeout"
    assert signature["repo_count"] == 2
    assert signature["occurrence_count"] == 4
    assert signature["trend"] == "new"
    assert [repo["repo_id"] for repo in signature["repos"]] == ["alpha", "beta"]
    assert [repo["occurrence_count"] for repo in signature["repos"]] == [2, 2]
    assert payload["repos"][0]["source_families"]["regex"]["accepted"] == 2
    assert payload["repos"][0]["source_families"]["regex"]["skipped"] == 1
    assert payload["repos"][2]["status"] == "failed"
    rendered = json.dumps(payload)
    assert str(tmp_path) not in rendered
    assert "private-alpha" not in rendered
    assert "private-beta" not in rendered

    root = tmp_path / ".brigade" / "repos" / "friction"
    assert payload["report_id"].endswith("-repos-friction")
    assert (root / f"{payload['report_id']}.json").is_file()
    assert (root / f"{payload['report_id']}.md").is_file()
    assert (root / "latest.json").is_file()
    assert (root / "latest.md").is_file()
    assert cli.main(["repos", "friction", "show", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["report_id"] == payload["report_id"]


def test_repos_friction_second_run_classifies_new_recurring_cleared_and_unknown(tmp_path, capsys):
    alpha = tmp_path / "repo-alpha"
    beta = tmp_path / "repo-beta"
    alpha.mkdir()
    beta.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha), ("beta", "service beta", beta)])
    auth = _write_note(alpha, "auth.log", "permission denied while refreshing the session\n")
    _write_note(alpha, "network.log", "connection refused by the MCP transport\n")
    slow = _write_note(alpha, "slow.log", "request timed out while contacting the compiler cache\n")
    _write_note(beta, "network.log", "connection refused by the MCP transport\n")
    _write_note(beta, "quota.log", "rate limit reached for this request\n")

    assert repos_cmd.friction_scan(target=tmp_path, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)

    auth.unlink()
    slow.unlink()
    _write_note(alpha, "tool.log", "command not found while running the checker\n")
    shutil.rmtree(beta)

    assert repos_cmd.friction_scan(target=tmp_path, json_output=True) == 1
    second = json.loads(capsys.readouterr().out)

    current = {item["friction_type"]: item["trend"] for item in second["signatures"]}
    assert current["network_timeout"] == "recurring"
    assert current["tool_failure"] == "new"
    assert {item["friction_type"] for item in second["comparison"]["cleared"]} == {"auth", "network_timeout"}
    assert {item["friction_type"] for item in second["comparison"]["unknown"]} == {"quota"}
    assert second["comparison"]["previous_report_id"] == first["report_id"]


def test_repos_friction_scans_global_agent_logs_once(tmp_path, monkeypatch, capsys):
    alpha = tmp_path / "repo-alpha"
    beta = tmp_path / "repo-beta"
    alpha.mkdir()
    beta.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha), ("beta", "service beta", beta)])
    calls: list[bool] = []

    def fake_scan_payload(**kwargs):
        calls.append(bool(kwargs.get("include_agent_logs")))
        families = {
            family: {"accepted": 0, "grouped": 0, "rejected": 0, "truncated": 0}
            for family in friction_cmd.SOURCE_FAMILIES
        }
        candidates = []
        if kwargs.get("include_agent_logs"):
            families["regex"] = {"accepted": 2, "grouped": 1, "rejected": 3, "truncated": 4}
            candidates.extend(
                [
                    {
                        "id": "agent-tool-failure",
                        "friction_type": "tool_failure",
                        "source_family": "regex",
                        "evidence": {"path": "agent-log", "line": 1, "snippet": f"command failed in {alpha}"},
                    },
                    {
                        "id": "agent-auth-failure",
                        "friction_type": "auth",
                        "source_family": "regex",
                        "evidence": {"path": "agent-log", "line": 2, "snippet": "permission denied"},
                    },
                ]
            )
        return (
            {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "files_scanned": 1 if candidates else 0,
                "files_skipped": 0,
                "candidate_count": len(candidates),
                "truncated": bool(candidates),
                "rejected_noise": 3 if candidates else 0,
                "counts": {"by_source_family": families},
                "candidates": candidates,
            },
            0,
        )

    monkeypatch.setattr(friction_cmd, "scan_payload", fake_scan_payload)

    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(calls) == 3
    assert calls.count(True) == 1
    assert calls.count(False) == 2
    signature = next(item for item in payload["signatures"] if item["friction_type"] == "tool_failure")
    assert signature["repos"] == [{"repo_id": "alpha", "label": "service alpha", "occurrence_count": 1}]
    assert signature["latest_evidence_at"] == "2026-07-16T12:00:00+00:00"
    unassociated = next(item for item in payload["signatures"] if item["friction_type"] == "auth")
    assert unassociated["repo_count"] == 0
    assert unassociated["occurrence_count"] == 1
    assert payload["unassociated_occurrence_count"] == 1
    assert payload["agent_logs"]["status"] == "completed"
    assert payload["agent_logs"]["candidate_count"] == 2
    assert payload["agent_logs"]["truncated"] is True
    assert payload["agent_logs"]["rejected_noise"] == 3
    assert payload["agent_logs"]["source_families"]["regex"] == {
        "accepted": 2,
        "grouped": 1,
        "rejected": 3,
        "skipped": 7,
        "truncated": 4,
    }


def test_repos_friction_agent_association_requires_repo_path_boundary(tmp_path, monkeypatch, capsys):
    service = tmp_path / "private-svc"
    service_api = tmp_path / "private-svc-api"
    service.mkdir()
    service_api.mkdir()
    _write_config(
        tmp_path,
        [("root", "service", service), ("api", "service api", service_api)],
    )

    def fake_scan_payload(**kwargs):
        families = {
            family: {"accepted": 0, "grouped": 0, "rejected": 0, "truncated": 0}
            for family in friction_cmd.SOURCE_FAMILIES
        }
        candidates = []
        if kwargs.get("include_agent_logs"):
            candidates.append(
                {
                    "id": "agent-prefix-failure",
                    "friction_type": "tool_failure",
                    "source_family": "regex",
                    "evidence": {
                        "path": "agent-log",
                        "line": 1,
                        "snippet": f"command failed in {service_api / 'src' / 'worker.py'}",
                    },
                }
            )
        return (
            {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "files_scanned": 1 if candidates else 0,
                "files_skipped": 0,
                "candidate_count": len(candidates),
                "counts": {"by_source_family": families},
                "candidates": candidates,
            },
            0,
        )

    monkeypatch.setattr(friction_cmd, "scan_payload", fake_scan_payload)

    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    signature = next(item for item in payload["signatures"] if item["id"] == "agent-prefix-failure")
    assert [repo["repo_id"] for repo in signature["repos"]] == ["api"]
    assert "api/src/worker.py" in signature["safe_summary"]
    assert "root-api" not in signature["safe_summary"]


def test_repos_friction_sanitizes_global_agent_log_roots(tmp_path, monkeypatch, capsys):
    alpha = tmp_path / "repo-alpha"
    alpha.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha)])
    private_agent_root = Path(friction_cmd.DEFAULT_AGENT_LOG_DIRS[0]).expanduser().resolve()

    def fake_scan_payload(**kwargs):
        families = {
            family: {"accepted": 0, "grouped": 0, "rejected": 0, "truncated": 0}
            for family in friction_cmd.SOURCE_FAMILIES
        }
        candidates = []
        if kwargs.get("include_agent_logs"):
            candidates.append(
                {
                    "id": "agent-private-path",
                    "friction_type": "auth",
                    "source_family": "regex",
                    "evidence": {
                        "path": str(private_agent_root / "sessions" / "run.jsonl"),
                        "line": 1,
                        "snippet": f"permission denied in {private_agent_root / 'sessions'}",
                    },
                }
            )
        return (
            {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "files_scanned": 1 if candidates else 0,
                "files_skipped": 0,
                "candidate_count": len(candidates),
                "counts": {"by_source_family": families},
                "candidates": candidates,
            },
            0,
        )

    monkeypatch.setattr(friction_cmd, "scan_payload", fake_scan_payload)

    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert str(private_agent_root) not in json.dumps(payload)


def test_repos_friction_sanitizes_unconfigured_home_paths(tmp_path, monkeypatch, capsys):
    alpha = tmp_path / "repo-alpha"
    alpha.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha)])
    private_path = Path.home() / "private-not-in-fleet" / "failure.log"

    def fake_scan_payload(**kwargs):
        families = {
            family: {"accepted": 0, "grouped": 0, "rejected": 0, "truncated": 0}
            for family in friction_cmd.SOURCE_FAMILIES
        }
        candidates = []
        if kwargs.get("include_agent_logs"):
            candidates.append(
                {
                    "id": "unconfigured-home-path",
                    "friction_type": "blocked_workflow",
                    "source_family": "regex",
                    "evidence": {
                        "path": str(private_path),
                        "line": 1,
                        "snippet": f"command failed in {private_path}",
                    },
                }
            )
        return (
            {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "files_scanned": 1 if candidates else 0,
                "files_skipped": 0,
                "candidate_count": len(candidates),
                "counts": {"by_source_family": families},
                "candidates": candidates,
            },
            0,
        )

    monkeypatch.setattr(friction_cmd, "scan_payload", fake_scan_payload)

    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=True, json_output=True) == 0
    rendered = capsys.readouterr().out

    assert str(Path.home()) not in rendered
    assert "~/private-not-in-fleet/failure.log" in rendered


def test_repos_friction_catches_one_repo_scanner_exception(tmp_path, monkeypatch, capsys):
    alpha = tmp_path / "repo-alpha"
    beta = tmp_path / "repo-beta"
    alpha.mkdir()
    beta.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha), ("beta", "service beta", beta)])

    def fake_scan_payload(**kwargs):
        if kwargs["target"] == beta:
            raise RuntimeError("private scanner detail")
        families = {
            family: {"accepted": 0, "grouped": 0, "rejected": 0, "truncated": 0}
            for family in friction_cmd.SOURCE_FAMILIES
        }
        return (
            {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "files_scanned": 0,
                "files_skipped": 0,
                "candidate_count": 0,
                "counts": {"by_source_family": families},
                "candidates": [],
            },
            0,
        )

    monkeypatch.setattr(friction_cmd, "scan_payload", fake_scan_payload)

    assert repos_cmd.friction_scan(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert [repo["status"] for repo in payload["repos"]] == ["completed", "failed"]
    assert "private scanner detail" not in json.dumps(payload)


def test_repos_friction_agent_log_failure_is_partial_without_losing_repo_results(tmp_path, monkeypatch, capsys):
    alpha = tmp_path / "repo-alpha"
    alpha.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha)])

    def fake_scan_payload(**kwargs):
        if kwargs.get("include_agent_logs"):
            raise RuntimeError("private global log failure")
        families = {
            family: {"accepted": 0, "grouped": 0, "rejected": 0, "truncated": 0}
            for family in friction_cmd.SOURCE_FAMILIES
        }
        return (
            {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "files_scanned": 0,
                "files_skipped": 0,
                "candidate_count": 0,
                "counts": {"by_source_family": families},
                "candidates": [],
            },
            0,
        )

    monkeypatch.setattr(friction_cmd, "scan_payload", fake_scan_payload)

    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["repos"][0]["status"] == "completed"
    assert payload["agent_logs"]["status"] == "failed"
    assert "private global log failure" not in json.dumps(payload)


def test_repos_friction_keeps_distinct_dated_reports_within_one_second(tmp_path, monkeypatch, capsys):
    alpha = tmp_path / "repo-alpha"
    alpha.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha)])
    first_at = datetime(2026, 7, 16, 12, 0, 0, 100000, tzinfo=timezone.utc)
    times = iter((first_at, first_at + timedelta(milliseconds=500)))
    monkeypatch.setattr(repos_cmd.friction_fleet, "_now", lambda: next(times))

    assert repos_cmd.friction_scan(target=tmp_path, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert repos_cmd.friction_scan(target=tmp_path, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["report_id"] != second["report_id"]
    root = tmp_path / ".brigade" / "repos" / "friction"
    assert (root / f"{first['report_id']}.json").is_file()
    assert (root / f"{second['report_id']}.json").is_file()


def test_repos_friction_marks_unassociated_history_unknown_when_agent_scan_fails(tmp_path, monkeypatch, capsys):
    alpha = tmp_path / "repo-alpha"
    alpha.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha)])
    agent_calls = 0

    def fake_scan_payload(**kwargs):
        nonlocal agent_calls
        families = {
            family: {"accepted": 0, "grouped": 0, "rejected": 0, "truncated": 0}
            for family in friction_cmd.SOURCE_FAMILIES
        }
        if kwargs.get("include_agent_logs"):
            agent_calls += 1
            if agent_calls == 2:
                raise RuntimeError("agent logs unavailable")
            candidates = [
                {
                    "id": "agent-auth-failure",
                    "friction_type": "auth",
                    "source_family": "regex",
                    "evidence": {"path": "agent-log", "line": 1, "snippet": "permission denied"},
                }
            ]
        else:
            candidates = []
        return (
            {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "files_scanned": 1 if candidates else 0,
                "files_skipped": 0,
                "candidate_count": len(candidates),
                "counts": {"by_source_family": families},
                "candidates": candidates,
            },
            0,
        )

    monkeypatch.setattr(friction_cmd, "scan_payload", fake_scan_payload)

    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=True, json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=True, json_output=True) == 1
    second = json.loads(capsys.readouterr().out)

    assert {item["friction_type"] for item in second["comparison"]["unknown"]} == {"auth"}
    assert second["comparison"]["cleared"] == []


def test_repos_friction_keeps_agent_associated_history_unknown_when_next_scan_omits_logs(tmp_path, monkeypatch, capsys):
    alpha = tmp_path / "repo-alpha"
    beta = tmp_path / "repo-beta"
    alpha.mkdir()
    beta.mkdir()
    _write_config(tmp_path, [("alpha", "service alpha", alpha), ("beta", "service beta", beta)])

    def fake_scan_payload(**kwargs):
        families = {
            family: {"accepted": 0, "grouped": 0, "rejected": 0, "truncated": 0}
            for family in friction_cmd.SOURCE_FAMILIES
        }
        candidates = []
        if kwargs.get("include_agent_logs"):
            candidates.append(
                {
                    "id": "shared-agent-failure",
                    "friction_type": "tool_failure",
                    "source_family": "regex",
                    "evidence": {
                        "path": "agent-log",
                        "line": 1,
                        "snippet": f"one command failed in {alpha} and {beta}",
                    },
                }
            )
        return (
            {
                "generated_at": "2026-07-16T12:00:00+00:00",
                "files_scanned": 1 if candidates else 0,
                "files_skipped": 0,
                "candidate_count": len(candidates),
                "counts": {"by_source_family": families},
                "candidates": candidates,
            },
            0,
        )

    monkeypatch.setattr(friction_cmd, "scan_payload", fake_scan_payload)

    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=True, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    signature = next(item for item in first["signatures"] if item["id"] == "shared-agent-failure")
    assert signature["occurrence_count"] == 1
    assert [repo["occurrence_count"] for repo in signature["repos"]] == [1, 1]
    assert signature["agent_evidence"] is True

    assert repos_cmd.friction_scan(target=tmp_path, include_agent_logs=False, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert {item["friction_type"] for item in second["comparison"]["unknown"]} == {"tool_failure"}
    assert second["comparison"]["cleared"] == []
