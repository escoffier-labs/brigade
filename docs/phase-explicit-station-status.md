# Explicit Station Status Plan

Goal: replace top-level `empty` and `issues` states with the approved six-state vocabulary while keeping summary probes read-only and bounded.

Architecture: `status.py` projects existing station payloads into one public vocabulary. Built-in stations still use their existing doctor checks. Optional sidecar stations use their existing status payloads, except MiseLedger summary mode skips doctor and uses only bounded `status --json`. The detailed station commands keep their current payload vocabulary for compatibility.

Key tech: Python mappings, lazy imports to avoid cycles, pytest monkeypatch fixtures, and existing process-boundary helpers. Execute tasks in order and keep the checkboxes current.

## File Map

- `src/brigade/status.py`: canonical state mapping, optional-station dispatcher, and row construction.
- `src/brigade/evidence_cmd.py`: bounded status-only mode for top-level summaries.
- `src/brigade/tokens_cmd.py`: align the usage-tracker summary command with its declared read-only surface.
- `tests/test_status.py`: state precedence and payload-dispatch regressions.
- `tests/test_evidence_cmd.py`: prove summary mode never invokes MiseLedger doctor.
- `tests/test_tokens_cmd.py`: prove the summary command includes `--no-write` and `--since 30d`.

## Task 1: Canonical top-level health

**Files:**

- Modify: `src/brigade/status.py`
- Modify: `tests/test_status.py`

- [x] Add failing mapping tests:

```python
import pytest

from brigade import doctor
from brigade import registry


@pytest.mark.parametrize(
    ("raw", "installed", "expected"),
    [
        ("ok", True, "ok"),
        ("warn", True, "degraded"),
        ("fail", True, "failed"),
        ("timeout", True, "degraded"),
        ("incomplete", True, "degraded"),
        ("unwired", True, "not-configured"),
        ("missing", False, "not-installed"),
        ("manual", False, "not-installed"),
        ("unknown", True, "unchecked"),
    ],
)
def test_normalize_payload_health(raw, installed, expected):
    assert status_mod._normalize_payload_health(raw, installed=installed) == expected


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([], "unchecked"),
        ([(doctor.INFO, "x", "x")], "not-configured"),
        ([(doctor.OK, "x", "x")], "ok"),
        ([(doctor.OK, "x", "x"), (doctor.WARN, "y", "y")], "degraded"),
        ([(doctor.FAIL, "x", "x"), (doctor.WARN, "y", "y")], "failed"),
    ],
)
def test_health_from_doctor_checks(checks, expected):
    assert status_mod._health_from_checks(checks) == expected
```

- [x] Run the two tests and watch them fail because the helpers do not exist:

```bash
/home/clawdbot/repos/brigade/.venv/bin/pytest tests/test_status.py -q
```

- [x] Add the canonical state tuple and pure mapping helpers to `status.py`:

```python
STATUS_STATES = (
    "not-installed",
    "not-configured",
    "unchecked",
    "ok",
    "degraded",
    "failed",
)


def _normalize_payload_health(raw: object, *, installed: bool | None) -> str:
    value = str(raw or "").lower()
    if installed is False and value in {"", "manual", "missing"}:
        return "not-installed"
    return {
        "ok": "ok",
        "warn": "degraded",
        "fail": "failed",
        "timeout": "degraded",
        "incomplete": "degraded",
        "unwired": "not-configured",
        "missing": "not-installed",
    }.get(value, "unchecked")


def _health_from_checks(checks: list[_doctor.CheckResult]) -> str:
    levels = {status for status, _name, _detail in checks}
    if _doctor.FAIL in levels:
        return "failed"
    if _doctor.WARN in levels:
        return "degraded"
    if _doctor.OK in levels:
        return "ok"
    if _doctor.MANUAL in levels or _doctor.INFO in levels:
        return "not-configured"
    return "unchecked"
```

- [x] Add a lazy optional-station dispatcher:

```python
def _optional_station_payload(station: str, target: Path) -> dict[str, object] | None:
    if station == "tokens":
        from . import tokens_cmd
        return tokens_cmd.status_payload(target)
    if station == "search":
        from . import search_cmd
        return search_cmd.status_payload(target)
    if station == "pantry":
        from . import pantry_cmd
        return pantry_cmd.status_payload(target)
    if station == "notifications":
        from . import notifications_cmd
        return notifications_cmd._status_payload()
    if station == "evidence":
        from . import evidence_cmd
        return evidence_cmd.status_payload(target, include_doctor=False, timeout=5.0)
    return None
```

- [x] Add a failing dispatcher test that patches `_optional_station_payload`, limits `all_stations()` to `registry.SEARCH`, returns `{"installed": True, "health": "ok", "summary": "graph ok"}`, and asserts JSON health is `ok` with summary `graph ok`.

- [x] Rewrite the `status.run()` row loop. Optional payloads supply health and summary. Other stations retain doctor counts and use `_health_from_checks`. Set optional-row counts to `ok=1` only for `ok`, `warn=1` only for `degraded`, and `fail=1` only for `failed`.

- [x] Add a warning regression using one fake station doctor with one `WARN` result. Assert the row health is `degraded`, not `ok` or `empty`.

- [x] Run `tests/test_status.py` to green through Brigade.

## Task 2: Keep summary probes read-only and bounded

**Files:**

- Modify: `src/brigade/evidence_cmd.py`
- Modify: `src/brigade/tokens_cmd.py`
- Modify: `tests/test_evidence_cmd.py`
- Modify: `tests/test_tokens_cmd.py`

- [x] Add a failing MiseLedger test that patches `evidence_brief._miseledger_bin` and `_run_json`, calls `status_payload(tmp_path, include_doctor=False, timeout=5.0)`, and asserts the only command is `["miseledger", "status", "--json"]` with timeout `5.0`.

- [x] Change the signature to:

```python
def status_payload(
    target: Path,
    *,
    include_doctor: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
```

Use `timeout` for status and doctor calls. When `include_doctor` is false, do not call doctor. Derive `ok` from exit 0 with a JSON object, `unwired` from exit 2, `timeout` from exit 124, and `incomplete` for any other result. Preserve the existing detailed behavior when `include_doctor` is true.

- [x] Add a failing Token Glace test that captures the usage-tracker argv and expects:

```python
[tracker, "export", "--since", "30d", "--summary-json", "--no-write"]
```

- [x] Replace the current usage-tracker summary argv with that exact command.

- [x] Run the focused status suites through Brigade:

```bash
/home/clawdbot/repos/brigade/.venv/bin/brigade work verify run --target . --command "/home/clawdbot/repos/brigade/.venv/bin/pytest tests/test_status.py tests/test_evidence_cmd.py tests/test_tokens_cmd.py tests/test_search_cmd.py tests/test_pantry_cmd.py -q" --capture brigade-work
```

Expected: all selected tests pass.

- [x] Run the full gate through Brigade:

```bash
/home/clawdbot/repos/brigade/.venv/bin/brigade work verify run --target . --command "env PY=/home/clawdbot/repos/brigade/.venv/bin ./scripts/verify" --capture brigade-work
```

- [x] Commit:

```bash
git add src/brigade/status.py src/brigade/evidence_cmd.py src/brigade/tokens_cmd.py tests/test_status.py tests/test_evidence_cmd.py tests/test_tokens_cmd.py docs/phase-explicit-station-status.md
git commit -m "fix(status): report explicit station health"
```
