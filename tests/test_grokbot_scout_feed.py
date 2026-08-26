"""Policy and issue discovery tests for the Grok Bot scout feed."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import grokbot_scout_feed


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _policy(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "brigade.grokbot.scout-feed.v1",
        "approved": True,
        "repository": "example/brigade",
        "approval_label": "grokbot-scout-approved",
        "base_ref": "main",
        "ownership_paths": ["src/brigade", "tests"],
        "verification_commands": ["pytest -q tests -k issue"],
        "timeout_seconds": 7200,
        "daily_limit": 3,
    }
    value.update(overrides)
    return value


def _write_policy(path: Path, payload: dict[str, object], *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def _gh_numbers(monkeypatch: pytest.MonkeyPatch, numbers: list[int]) -> None:
    class Result:
        returncode = 0
        stdout = json.dumps([{"number": number} for number in numbers])
        stderr = ""

    monkeypatch.setattr(grokbot_scout_feed.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(grokbot_scout_feed.subprocess, "run", lambda *args, **kwargs: Result())


def _assert_no_queue_state(target: Path) -> None:
    assert not (target / ".brigade" / "cloud" / "grokbot").exists()


def test_preflight_discovers_sorted_unique_numbers_without_creating_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    policy = _write_policy(tmp_path / "policy.json", _policy())
    _gh_numbers(monkeypatch, [42, 7, 42])

    result = grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)

    assert result == {
        "valid": True,
        "created": 0,
        "reason": "ready",
        "repository": "example/brigade",
        "issue_number": 7,
        "daily_limit": 3,
        "created_today": 0,
    }
    _assert_no_queue_state(tmp_path)


def test_preflight_invokes_gh_with_exact_number_only_argv_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class Result:
        returncode = 0
        stdout = json.dumps([{"number": 7}])
        stderr = ""

    calls: list[tuple[object, dict[str, object]]] = []
    policy = _write_policy(tmp_path / "policy.json", _policy())
    monkeypatch.setattr(grokbot_scout_feed.shutil, "which", lambda name: "/usr/bin/gh")

    def run(argv: object, **kwargs: object) -> Result:
        calls.append((argv, kwargs))
        return Result()

    monkeypatch.setattr(grokbot_scout_feed.subprocess, "run", run)

    grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)

    assert calls == [
        (
            [
                "gh",
                "issue",
                "list",
                "--repo",
                "example/brigade",
                "--state",
                "open",
                "--label",
                "grokbot-scout-approved",
                "--limit",
                "100",
                "--json",
                "number",
            ],
            {"capture_output": True, "check": False, "shell": False, "text": True},
        )
    ]
    _assert_no_queue_state(tmp_path)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (_policy(schema="brigade.grokbot.scout-feed.v0"), "invalid-schema"),
        (_policy(approved=False), "not-approved"),
        ({key: value for key, value in _policy().items() if key != "approved"}, "malformed-policy"),
        (_policy(repository="bad/repo/path"), "invalid-repository"),
        (_policy(base_ref="../main"), "invalid-base-ref"),
        (_policy(ownership_paths=["../private"]), "invalid-ownership-paths"),
        (_policy(verification_commands=[]), "invalid-verification-commands"),
        (_policy(timeout_seconds=30), "invalid-timeout"),
        (_policy(approval_label=""), "invalid-approval-label"),
        (_policy(approval_label="has\nnewline"), "invalid-approval-label"),
        (_policy(approval_label="has\x00nul"), "invalid-approval-label"),
        (_policy(approval_label="x" * 101), "invalid-approval-label"),
        (_policy(daily_limit=0), "invalid-daily-limit"),
        (_policy(daily_limit=True), "invalid-daily-limit"),
        (_policy(daily_limit=11), "invalid-daily-limit"),
    ],
)
def test_preflight_rejects_every_invalid_policy_field_without_queue_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], reason: str
):
    policy = _write_policy(tmp_path / "policy.json", payload)
    _gh_numbers(monkeypatch, [7])

    with pytest.raises(grokbot_scout_feed.ScoutFeedError, match=f"^{reason}$"):
        grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)

    _assert_no_queue_state(tmp_path)


@pytest.mark.parametrize("mode", [0o660, 0o602])
def test_preflight_rejects_writable_policy_without_queue_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
):
    policy = _write_policy(tmp_path / "policy.json", _policy(), mode=mode)
    _gh_numbers(monkeypatch, [7])

    with pytest.raises(grokbot_scout_feed.ScoutFeedError, match="^unsafe-policy$"):
        grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)

    _assert_no_queue_state(tmp_path)


def test_preflight_rejects_symlinked_policy_without_queue_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real = _write_policy(tmp_path / "real-policy.json", _policy())
    policy = tmp_path / "policy.json"
    policy.symlink_to(real)
    _gh_numbers(monkeypatch, [7])

    with pytest.raises(grokbot_scout_feed.ScoutFeedError, match="^unsafe-policy$"):
        grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)

    _assert_no_queue_state(tmp_path)


def test_preflight_rejects_missing_gh_without_queue_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy = _write_policy(tmp_path / "policy.json", _policy())
    monkeypatch.setattr(grokbot_scout_feed.shutil, "which", lambda name: None)

    with pytest.raises(grokbot_scout_feed.ScoutFeedError, match="^gh-unavailable$"):
        grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)

    _assert_no_queue_state(tmp_path)


def test_preflight_redacts_gh_failure_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class Result:
        returncode = 1
        stdout = "PRIVATE_GH_STDOUT"
        stderr = "PRIVATE_GH_STDERR"

    policy = _write_policy(tmp_path / "policy.json", _policy())
    monkeypatch.setattr(grokbot_scout_feed.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(grokbot_scout_feed.subprocess, "run", lambda *args, **kwargs: Result())

    with pytest.raises(grokbot_scout_feed.ScoutFeedError) as exc:
        grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)

    assert exc.value.reason == "gh-failed"
    assert "PRIVATE_GH" not in str(exc.value)
    _assert_no_queue_state(tmp_path)


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        json.dumps({"number": 7}),
        json.dumps([{"number": 7, "title": "private"}]),
        json.dumps([{"number": True}]),
        json.dumps([{"number": 0}]),
        json.dumps([{"number": -1}]),
        json.dumps([{"number": "7"}]),
    ],
)
def test_preflight_rejects_malformed_gh_output_without_queue_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
):
    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, output: str):
            self.stdout = output

    policy = _write_policy(tmp_path / "policy.json", _policy())
    monkeypatch.setattr(grokbot_scout_feed.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(grokbot_scout_feed.subprocess, "run", lambda *args, **kwargs: Result(stdout))

    with pytest.raises(grokbot_scout_feed.ScoutFeedError, match="^malformed-gh-output$"):
        grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)

    _assert_no_queue_state(tmp_path)
