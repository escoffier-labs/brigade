# Grok Bot approved issue selector implementation plan

**Goal:** Keep the existing Grok Bot Repository Scout supplied with explicitly labeled GitHub issues while preserving local queue authority, one-job execution, and a fixed UTC daily cap.

**Architecture:** Add one `grokbot_scout_feed` module beside the existing manifest feed. It validates an owner-only policy, asks `gh issue list` for numbers only, checks the private queue without creating it during preview, and builds one fixed read-only envelope. `run_cloud` exposes the operation as `scout-feed`. A queue admission helper performs the active-work, UTC daily-cap, idempotency, and creation checks under one lock. The MCP adapter, Fleet Hub publication, and Implementation Worker stay unchanged.

**Key technology:** Python standard library, `gh` CLI, pytest, the existing Grok Bot JSON queue, Brigade verification receipts.

Execute tasks in order and keep the checkboxes current. Every production behavior begins with a failing test captured through Brigade.

## File map

- `src/brigade/grokbot_scout_feed.py`: private policy validation, issue discovery, queue and daily-cap checks, deterministic envelope, preview, and apply.
- `tests/test_grokbot_scout_feed.py`: policy safety, discovery, selection, caps, idempotency, redaction, and CLI tests.
- `src/brigade/cli/run_cloud.py`: `scout-feed` parser, dispatch, and safe text output.
- `docs/grokbot-mcp.md`: policy example and hourly systemd invocation.
- `docs/phase-grokbot-approved-issue-selector.md`: approved contract and implementation status.

## demi

Actual ask: replenish useful Grok Bot work automatically without overlapping Fleet Hub active-work reporting.

Smallest useful slice: one labeled GitHub issue source that can create one Repository Scout report job per invocation.

Highest rung that holds: the existing Grok Bot queue, feed permission reader, job validator, `gh` CLI pattern, and systemd timer.

Existing pattern to follow: `grokbot_feed.py`, `_read_github_issue()` in `work_cmd/ledger/`, and `run_cloud._dispatch_grokbot_feed()`.

Cut from scope: Implementation Worker selection, quota-percentage inference, issue comments or labels, Fleet Hub APIs, campaigns, retries, merge, release, deploy, and new dependencies.

Growth trigger: add another source behind the same selection result only after Fleet Hub campaigns have a stable approved-work contract.

Verification: focused Grok Bot tests, Ruff, Mypy through `scripts/verify`, docs checks, and one preview plus one supervised live Scout job.

### Task 1: Validate the private policy and discover issue numbers

**Files:**

- Create: `tests/test_grokbot_scout_feed.py`
- Create: `src/brigade/grokbot_scout_feed.py`

- [x] Add these fixtures and the first failing tests:

```python
from __future__ import annotations

import json
import os
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


def _write_policy(path: Path, payload: dict[str, object], mode: int = 0o600) -> Path:
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


def test_preflight_discovers_numbers_without_creating_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy = _write_policy(tmp_path / "policy.json", _policy())
    _gh_numbers(monkeypatch, [42, 7])

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
    assert not (tmp_path / ".brigade" / "cloud" / "grokbot").exists()


@pytest.mark.parametrize("mode", [0o660, 0o602])
def test_preflight_rejects_writable_policy_without_queue_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
):
    policy = _write_policy(tmp_path / "policy.json", _policy(), mode=mode)
    _gh_numbers(monkeypatch, [7])

    with pytest.raises(grokbot_scout_feed.ScoutFeedError, match="^unsafe-policy$"):
        grokbot_scout_feed.preflight(tmp_path, policy, now=NOW)
    assert not (tmp_path / ".brigade" / "cloud" / "grokbot").exists()
```

- [x] Run RED:

```bash
brigade work verify run --target . --command ".venv/bin/pytest -q tests/test_grokbot_scout_feed.py -k 'preflight'" --capture brigade-work
```

Expect import failure because `grokbot_scout_feed` does not exist.

- [x] Implement the policy loader and discovery shell. Reuse `grokbot_feed._read_manifest_snapshot()` for the established owner and mode check, translate its error to `unsafe-policy`, and validate every value through `grokbot_jobs` validators:

```python
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
ACTIVE_STATES = frozenset({"queued", "claimed", "running", "cancel-requested"})


class ScoutFeedError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def load_policy(path: Path) -> dict[str, object]:
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
```

`_validate_policy()` constructs a temporary Repository Scout spec with fixed placeholder instructions and calls `grokbot_jobs._validate_spec()`. It separately requires a non-empty approval label of at most 100 characters, without NUL or a newline, and an integer `daily_limit` from 1 through 10. Return a deep JSON copy.

`_discover_issue_numbers()` requires `shutil.which("gh")`, invokes this exact argv with `shell=False`, and accepts a list of objects containing only a positive integer `number`:

```python
[
    "gh", "issue", "list",
    "--repo", policy["repository"],
    "--state", "open",
    "--label", policy["approval_label"],
    "--limit", "100",
    "--json", "number",
]
```

Return sorted unique numbers. Missing `gh`, nonzero exit, malformed JSON, unexpected fields, booleans, zero, or negative numbers raise stable reasons without including subprocess output.

- [x] Run GREEN with the Task 1 command. Add policy cases for wrong schema, missing approval, symlink, invalid repository, invalid paths, invalid commands, invalid timeout, invalid daily limit, missing `gh`, nonzero `gh`, and malformed output. Expect all to pass and leave queue state absent.

- [x] Commit:

```bash
git add src/brigade/grokbot_scout_feed.py tests/test_grokbot_scout_feed.py
git commit -m "feat: validate approved Grok Bot scout policy"
```

### Task 2: Enforce queue caps, daily caps, and stable issue idempotency

**Files:**

- Modify: `tests/test_grokbot_scout_feed.py`
- Modify: `src/brigade/grokbot_scout_feed.py`

- [x] Add a failing apply test:

```python
def test_apply_creates_one_fixed_read_only_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy = _write_policy(tmp_path / "policy.json", _policy())
    _gh_numbers(monkeypatch, [42, 7])

    result = grokbot_scout_feed.apply(tmp_path, policy, now=NOW)

    assert result["created"] == 1
    assert result["reason"] == "created"
    assert result["issue_number"] == 7
    assert set(result["job"]) == {"job_id", "state", "idempotent"}
    raw_path = next((tmp_path / ".brigade" / "cloud" / "grokbot" / "jobs").glob("grokbot-*.json"))
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw["spec"] == {
        "label": "Scout example/brigade issue #7",
        "role": "repository-scout",
        "repository": "example/brigade",
        "base_ref": "main",
        "ownership_paths": ["src/brigade", "tests"],
        "instructions": grokbot_scout_feed._instructions("example/brigade", "grokbot-scout-approved", 7),
        "verification_commands": ["pytest -q tests -k issue"],
        "artifact": {"kind": "report"},
        "timeout_seconds": 7200,
    }
    rendered = json.dumps(result)
    assert "pytest" not in rendered
    assert "src/brigade" not in rendered
```

- [x] Add failing no-work tests. Use `grokbot_jobs.enqueue()` to seed one active Repository Scout job and three same-day completed Scout jobs. Assert stable results for `active-job`, `daily-limit`, `all-known`, and `no-approved-issues`. Each result has `created: 0`, no `job`, and no task context.

- [x] Run RED:

```bash
brigade work verify run --target . --command ".venv/bin/pytest -q tests/test_grokbot_scout_feed.py -k 'apply or active or daily or known or no_approved'" --capture brigade-work
```

Expect failure because apply and cap logic are missing.

- [x] Implement `_instructions()` as a fixed template that names only repository, label, and issue number. It must include the read-only and untrusted-context rules from the approved phase spec.

- [x] Implement `_queue_snapshot()` without creating queue state during preview:

```python
def _queue_snapshot(target: Path) -> list[dict[str, object]]:
    root = Path(target) / ".brigade" / "cloud" / "grokbot"
    if not root.exists():
        return []
    payload = grokbot_jobs.status(target)
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise ScoutFeedError("unsafe-storage")
    return jobs
```

Count same-day jobs by parsing `created_at` as an aware datetime, converting to UTC, and comparing `.date()` to `now.astimezone(timezone.utc).date()`. Count all Repository Scout states, including expired and failed attempts.

- [x] For each sorted issue number, build a fixed 64-character SHA-256 idempotency key from the repository and the issue number's canonical positive-integer bytes, and use `grokbot_feed._existing_idempotency()` to skip known work. `preflight()` returns the selected issue without writing. `apply()` repeats admission through `grokbot_jobs.enqueue_repository_scout()`, which holds one queue lock across the active-work, UTC daily-cap, idempotency, and creation checks. Results return only safe counts, reason, issue number, and handle. Both public functions accept `now: datetime | None = None`. The default is an aware current UTC timestamp.

- [x] Run GREEN with the Task 2 command, then the whole new test file. Expect every test to pass.

- [x] Commit:

```bash
git add src/brigade/grokbot_scout_feed.py tests/test_grokbot_scout_feed.py
git commit -m "feat: select bounded Grok Bot scout work"
```

### Task 3: Add the CLI, docs, and full verification

**Files:**

- Modify: `tests/test_grokbot_scout_feed.py`
- Modify: `src/brigade/cli/run_cloud.py`
- Modify: `docs/grokbot-mcp.md`
- Modify: `docs/phase-grokbot-approved-issue-selector.md`

- [x] Add failing CLI tests for preview JSON, apply JSON, text no-work, malformed policy, and missing `gh`. Invoke `cli.main()` with:

```python
[
    "run", "cloud", "grokbot", "scout-feed",
    "--target", str(tmp_path),
    "--policy", str(policy),
    "--json",
]
```

Assert preview exits 0 and has no queue root. Add `--apply` and assert one queued job. Errors exit 2 and print only `error: <stable-reason>`.

- [x] Run RED:

```bash
brigade work verify run --target . --command ".venv/bin/pytest -q tests/test_grokbot_scout_feed.py -k cli" --capture brigade-work
```

Expect argparse failure because `scout-feed` is unknown.

- [x] Register `scout-feed` beside `feed`:

```python
p_scout_feed = grokbot_sub.add_parser(
    "scout-feed", help="Select one approved GitHub issue for Grok Bot Repository Scout."
)
add_target(p_scout_feed)
p_scout_feed.add_argument("--policy", type=Path, required=True, help="Path to the approved private scout policy.")
p_scout_feed.add_argument("--apply", action="store_true", help="Enqueue after selection. Default is preview only.")
```

Route it before the queue mutation switch. `_dispatch_grokbot_scout_feed()` catches `ScoutFeedError`, emits stable JSON or text, and never prints exception detail, subprocess output, commands, paths, or instructions.

- [x] Run GREEN with the CLI command and the complete new test file.

- [x] Add a sanitized policy example and systemd command to `docs/grokbot-mcp.md`. State that the approval label is the operator gate, the timer may run hourly, each pass creates at most one job, `daily_limit` counts failed and expired attempts, and the adapter cannot infer remaining quota percentage.

- [x] Update the phase spec status to implemented only after tests pass.

- [x] Run the focused contract through Brigade:

```bash
brigade work verify run --target . --command ".venv/bin/pytest -q tests/test_grokbot_scout_feed.py tests/test_grokbot_feed.py tests/test_grokbot_jobs.py tests/test_grokbot_mcp.py tests/test_grokbot_ops.py" --command ".venv/bin/ruff check src/brigade/grokbot_scout_feed.py src/brigade/cli/run_cloud.py tests/test_grokbot_scout_feed.py" --command ".venv/bin/ruff format --check src/brigade/grokbot_scout_feed.py src/brigade/cli/run_cloud.py tests/test_grokbot_scout_feed.py" --command "git diff --check" --capture brigade-work
```

- [x] Run the repository completion gate:

```bash
brigade work verify run --target . --command "./scripts/verify" --capture brigade-work --timeout 3600
```

- [x] Run Vale and detector checks on changed public prose through Brigade. Fix new findings without changing the approved security contract.

- [x] Write the required Memory Handoff in `.claude/memory-handoffs/` and lint it through Brigade.

- [x] Commit:

```bash
git add src/brigade/cli/run_cloud.py tests/test_grokbot_scout_feed.py docs/grokbot-mcp.md docs/phase-grokbot-approved-issue-selector.md .claude/memory-handoffs
git commit -m "docs: explain approved Grok Bot scout feed"
```

- [ ] Open a non-draft PR with `Refs` to the new selector issue. Do not claim it fixes the separate Fleet Hub active-work work. Do not merge until CI and independent review pass.
