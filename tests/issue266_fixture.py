"""Synthetic fixtures for issue 266 work-status memory regression tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

FLEET_REPO_COUNT = 53
FLEET_SWEEP_HISTORY_COUNT = 30
OPERATOR_REPORT_HISTORY_COUNT = 30
SHARED_SWEEP_ID = "20260716-120000-repo-fleet-sweep-fake01"
HEALTH_COMMAND_LABEL = "fake-health-check"
DEFAULT_ARTIFACT_PADDING_BYTES = 0


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def repo_ids(count: int = FLEET_REPO_COUNT) -> list[str]:
    return [f"fake-repo-{index:03d}" for index in range(1, count + 1)]


def init_workspace_git(target: Path) -> None:
    subprocess.run(["git", "init"], cwd=target, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture Dev"], cwd=target, check=True)
    (target / "README.md").write_text("Synthetic issue-266 benchmark workspace.\n")
    (target / "CHANGELOG.md").write_text("## [Unreleased]\n\n- Fixture only.\n")
    (target / "ROADMAP.md").write_text("# Roadmap\n\n- Synthetic fleet.\n")
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, stdout=subprocess.DEVNULL)


def seed_fleet_repo_dirs(target: Path, ids: list[str] | None = None) -> list[str]:
    ids = ids or repo_ids()
    for repo_id in ids:
        repo_dir = target / "fixtures" / "repos" / repo_id
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / "README.md").write_text(f"Synthetic repo {repo_id}\n")
    return ids


def seed_repos_toml(target: Path, ids: list[str]) -> None:
    lines: list[str] = []
    for repo_id in ids:
        rel = (target / "fixtures" / "repos" / repo_id).relative_to(target)
        lines.extend(
            [
                "[[repo]]",
                f'id = "{repo_id}"',
                f'label = "Synthetic {repo_id}"',
                f'path = "{rel.as_posix()}"',
                "enabled = true",
                "expect_brigade = false",
                "",
                "[[repo.health_command]]",
                f'label = "{HEALTH_COMMAND_LABEL}"',
                'argv = ["python3", "-c", "print(0)"]',
                "timeout = 30",
                "",
            ]
        )
    config = target / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines))


def _artifact_padding(padding_bytes: int) -> dict[str, str]:
    if padding_bytes <= 0:
        return {}
    return {"_fixture_padding": "x" * padding_bytes}


def _sweep_payload(ids: list[str], sweep_id: str, *, padding_bytes: int = 0) -> dict:
    payload = {
        "sweep_id": sweep_id,
        "status": "completed",
        "started_at": "2026-07-16T12:00:00+00:00",
        "completed_at": "2026-07-16T12:00:10+00:00",
        "repos": [
            {
                "repo_id": repo_id,
                "repo_label": f"Synthetic {repo_id}",
                "status": "completed",
                "commands": [
                    {
                        "label": HEALTH_COMMAND_LABEL,
                        "status": "completed",
                        "exit_code": 0,
                        "timed_out": False,
                        "started_at": "2026-07-16T12:00:01+00:00",
                        "completed_at": "2026-07-16T12:00:05+00:00",
                    }
                ],
            }
            for repo_id in ids
        ],
    }
    payload.update(_artifact_padding(padding_bytes))
    return payload


def seed_shared_fleet_sweep(
    target: Path,
    ids: list[str],
    *,
    sweep_id: str = SHARED_SWEEP_ID,
    padding_bytes: int = 0,
) -> None:
    sweep_dir = target / ".brigade" / "repos" / "sweeps" / sweep_id
    write_json(sweep_dir / "sweep.json", _sweep_payload(ids, sweep_id, padding_bytes=padding_bytes))


def seed_fleet_sweep_history(
    target: Path,
    ids: list[str],
    *,
    count: int = 1,
    padding_bytes: int = 0,
) -> None:
    for index in range(count):
        minute = index % 60
        hour = 12 + index // 60
        sweep_id = f"20260716-{hour:02d}{minute:02d}00-repo-fleet-sweep-{index:03d}"
        seed_shared_fleet_sweep(target, ids, sweep_id=sweep_id, padding_bytes=padding_bytes)


def _operator_report_payload(report_id: str, created_at: str, *, padding_bytes: int = 0) -> dict:
    payload = {
        "report_id": report_id,
        "status": "ready",
        "created_at": created_at,
        "generated_at": created_at,
        "closeout": {"status": "reviewed", "reviewed_at": created_at},
        "git": {"head": "0000000000000000000000000000000000000001"},
        "activity": [],
        "receipt_references": [],
    }
    payload.update(_artifact_padding(padding_bytes))
    return payload


def seed_operator_report_history(
    target: Path,
    count: int = OPERATOR_REPORT_HISTORY_COUNT,
    *,
    padding_bytes: int = 0,
) -> None:
    for index in range(count):
        minute = index % 60
        hour = 12 + index // 60
        report_id = f"20260716-{hour:02d}{minute:02d}00-operator-report-{index:03d}"
        created_at = f"2026-07-16T{hour:02d}:{minute:02d}:00+00:00"
        write_json(
            target / ".brigade" / "center" / "reports" / report_id / "CENTER_EVIDENCE.json",
            _operator_report_payload(report_id, created_at, padding_bytes=padding_bytes),
        )


def build_fleet_workspace(
    target: Path,
    *,
    repo_count: int = FLEET_REPO_COUNT,
    sweep_history_count: int = 1,
    artifact_padding_bytes: int = DEFAULT_ARTIFACT_PADDING_BYTES,
) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    init_workspace_git(target)
    ids = seed_fleet_repo_dirs(target, repo_ids(repo_count))
    seed_repos_toml(target, ids)
    seed_fleet_sweep_history(
        target,
        ids,
        count=sweep_history_count,
        padding_bytes=artifact_padding_bytes,
    )
    return target


def build_daily_status_workspace(
    target: Path,
    *,
    repo_count: int = FLEET_REPO_COUNT,
    report_count: int = OPERATOR_REPORT_HISTORY_COUNT,
    sweep_history_count: int = 1,
    artifact_padding_bytes: int = DEFAULT_ARTIFACT_PADDING_BYTES,
) -> Path:
    build_fleet_workspace(
        target,
        repo_count=repo_count,
        sweep_history_count=sweep_history_count,
        artifact_padding_bytes=artifact_padding_bytes,
    )
    seed_operator_report_history(target, report_count, padding_bytes=artifact_padding_bytes)
    return target
