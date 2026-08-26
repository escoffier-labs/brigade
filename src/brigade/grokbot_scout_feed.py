"""Private policy validation and issue-number discovery for Grok Bot scouts."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from . import grokbot_feed, grokbot_jobs

POLICY_SCHEMA = "brigade.grokbot.scout-feed.v1"
POLICY_KEYS = frozenset(
    {
        "schema",
        "approved",
        "repository",
        "approval_label",
        "base_ref",
        "ownership_paths",
        "verification_commands",
        "timeout_seconds",
        "daily_limit",
    }
)


class ScoutFeedError(ValueError):
    """A rejected scout policy or discovery request with a stable reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def preflight(target: Path, policy_path: Path, *, now: datetime) -> dict[str, object]:
    """Validate a private policy and discover the first approved issue number."""
    del target, now
    policy = load_policy(policy_path)
    numbers = _discover_issue_numbers(policy)
    if not numbers:
        return {
            "valid": True,
            "created": 0,
            "reason": "no-approved-issues",
            "repository": policy["repository"],
            "issue_number": None,
            "daily_limit": policy["daily_limit"],
            "created_today": 0,
        }
    return {
        "valid": True,
        "created": 0,
        "reason": "ready",
        "repository": policy["repository"],
        "issue_number": numbers[0],
        "daily_limit": policy["daily_limit"],
        "created_today": 0,
    }


def load_policy(path: Path) -> dict[str, object]:
    """Load and validate an owner-only scout policy."""
    try:
        payload = json.loads(grokbot_feed._read_manifest_snapshot(path))
    except grokbot_feed.FeedError as exc:
        raise ScoutFeedError("unsafe-policy") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoutFeedError("malformed-policy") from exc
    if not isinstance(payload, dict) or set(payload) != POLICY_KEYS:
        raise ScoutFeedError("malformed-policy")
    if payload["schema"] != POLICY_SCHEMA:
        raise ScoutFeedError("invalid-schema")
    if payload["approved"] is not True:
        raise ScoutFeedError("not-approved")
    return _validate_policy(payload)


def _validate_policy(payload: dict[str, Any]) -> dict[str, object]:
    _validate_approval_label(payload["approval_label"])
    daily_limit = payload["daily_limit"]
    if type(daily_limit) is not int or not 1 <= daily_limit <= 10:
        raise ScoutFeedError("invalid-daily-limit")
    spec = {
        "label": "Validate Grok Bot scout policy",
        "role": "repository-scout",
        "repository": payload["repository"],
        "base_ref": payload["base_ref"],
        "ownership_paths": payload["ownership_paths"],
        "instructions": "Validate the approved Grok Bot scout policy.",
        "verification_commands": payload["verification_commands"],
        "artifact": {"kind": "report"},
        "timeout_seconds": payload["timeout_seconds"],
    }
    try:
        grokbot_jobs._validate_spec(spec)
    except grokbot_jobs.GrokbotJobError as exc:
        raise ScoutFeedError(exc.reason) from exc
    return json.loads(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _validate_approval_label(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not 1 <= len(value) <= 100
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ScoutFeedError("invalid-approval-label")
    return value


def _discover_issue_numbers(policy: dict[str, object]) -> list[int]:
    if shutil.which("gh") is None:
        raise ScoutFeedError("gh-unavailable")
    repository = policy["repository"]
    approval_label = policy["approval_label"]
    assert isinstance(repository, str)
    assert isinstance(approval_label, str)
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--label",
                approval_label,
                "--limit",
                "100",
                "--json",
                "number",
            ],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
    except OSError as exc:
        raise ScoutFeedError("gh-failed") from exc
    if result.returncode != 0:
        raise ScoutFeedError("gh-failed")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ScoutFeedError("malformed-gh-output") from exc
    if not isinstance(payload, list):
        raise ScoutFeedError("malformed-gh-output")
    numbers: set[int] = set()
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != {"number"}:
            raise ScoutFeedError("malformed-gh-output")
        number = entry["number"]
        if type(number) is not int or number <= 0:
            raise ScoutFeedError("malformed-gh-output")
        numbers.add(number)
    return sorted(numbers)
