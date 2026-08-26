# Tiered Verification Gate Implementation Plan

**Goal:** Replace repeated 37 to 55 minute development gates with focused, receipt-backed checks while preserving one complete coverage gate for integration and CI.

**Architecture:** A new Bash entrypoint runs the existing fast repository checks and only caller-selected pytest targets. The existing full entrypoint keeps every check but takes a nonblocking POSIX lock in the shared Git directory through `scripts/verify_lock.py` (`fcntl.flock`), so linked worktrees cannot saturate the machine with duplicate full suites. `AGENTS.md` assigns each entrypoint to a distinct lifecycle stage. The documented full Brigade command pins `--timeout 3600` so the known-long integration gate is not killed by the 900-second verifier default. A verifier timeout (command exceeded `--timeout`) is distinct from lock contention (status 75).

**Key tech:** Bash, Python 3 `fcntl`, pytest, Brigade verification receipts.

Execute the checkboxes in order. Keep the test red before implementation, then commit the completed slice.

## File map

- `tests/test_verify_script.py`: executable contract tests for focused arguments, required selectors, lock contention, flock-shim independence, and agent instructions.
- `scripts/verify-focused`: focused development gate.
- `scripts/verify_lock.py`: portable POSIX lock helper used by the full gate.
- `scripts/verify`: full integration gate; invokes the helper when `BRIGADE_VERIFY_LOCK_HELD` is absent.
- `AGENTS.md`: lifecycle rules for focused and full verification.

### Task 1: Pin and implement the tiered gate

**Files:**
- Modify: `tests/test_verify_script.py`
- Create: `scripts/verify-focused`
- Modify: `scripts/verify`
- Modify: `AGENTS.md`

- [x] Add imports and a fake-tool helper to `tests/test_verify_script.py`:

```python
import os
import shutil
import subprocess

import pytest


def _fake_tool_dir(tmp_path: Path) -> tuple[Path, Path]:
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    log_path = tmp_path / "tools.log"
    stub = """#!/bin/sh
printf '%s' "$(basename "$0")" >> "$VERIFY_LOG"
for arg in "$@"; do printf '\t%s' "$arg" >> "$VERIFY_LOG"; done
printf '\n' >> "$VERIFY_LOG"
"""
    for name in ("ruff", "mypy", "python", "pytest"):
        path = tool_dir / name
        path.write_text(stub)
        path.chmod(0o755)
    return tool_dir, log_path
```

- [x] Add the focused-gate tests:

```python
@pytest.mark.skipif(os.name == "nt", reason="Bash verification entrypoint")
def test_verify_focused_requires_a_pytest_selector():
    completed = subprocess.run(
        [str(ROOT / "scripts/verify-focused")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "usage: ./scripts/verify-focused <pytest-selector>..." in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="Bash verification entrypoint")
def test_verify_focused_forwards_only_selected_tests(tmp_path, monkeypatch):
    tool_dir, log_path = _fake_tool_dir(tmp_path)
    monkeypatch.setenv("PY", str(tool_dir))
    monkeypatch.setenv("VERIFY_LOG", str(log_path))
    selectors = ["tests/test_verify_script.py", "tests/test_verify_script.py::test_root_ruff_configuration_excludes_non_python_trees"]
    completed = subprocess.run(
        [str(ROOT / "scripts/verify-focused"), *selectors],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0
    assert log_path.read_text().splitlines()[-1] == f"pytest\t-q\t{selectors[0]}\t{selectors[1]}"
```

- [x] Add the full-gate contention test:

```python
@pytest.mark.skipif(os.name != "posix", reason="fcntl is POSIX")
def test_verify_full_gate_fails_fast_when_another_gate_holds_the_lock(tmp_path, monkeypatch):
    import fcntl

    lock_path = tmp_path / "full.lock"
    lock_path.touch()
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PYTHON", sys.executable)
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = subprocess.run(
            [str(ROOT / "scripts/verify")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
    assert completed.returncode == 75
    assert "full verification already running" in completed.stderr
    assert "./scripts/verify-focused" in completed.stderr
```

- [x] Add an `AGENTS.md` contract test that requires both entrypoints, Brigade wrapping, full-gate lifecycle wording, the warning not to use `--no-reuse`, and `--timeout 3600` on the documented full Brigade command.

- [x] Run RED through Brigade:

```bash
brigade work verify run --target . --argv-json '[".venv/bin/pytest","-q","tests/test_verify_script.py"]' --capture brigade-work --no-reuse
```

Expected: failures because `scripts/verify-focused` does not exist, `scripts/verify` does not lock, and `AGENTS.md` still requires the full gate for every completed change.

- [x] Create executable `scripts/verify-focused`:

```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

if (( $# == 0 )); then
  echo "usage: ./scripts/verify-focused <pytest-selector>..." >&2
  exit 2
fi

PY=${PY:-.venv/bin}
"$PY/ruff" check .
"$PY/ruff" format --check .
"$PY/mypy"
"$PY/python" scripts/version_sync.py --check
"$PY/python" scripts/managed_snapshot.py --check
"$PY/python" scripts/template_profile_snapshot.py --check
"$PY/pytest" -q "$@"
```

- [x] Add the lock before tool execution in `scripts/verify` by execing `scripts/verify_lock.py` when `BRIGADE_VERIFY_LOCK_HELD` is absent. The helper opens the lock file 0600, takes `fcntl.flock(LOCK_EX|LOCK_NB)`, exits 75 on contention, marks the descriptor inheritable, sets `BRIGADE_VERIFY_LOCK_HELD=1`, and `os.execvpe`s `scripts/verify`:

```bash
if [ -z "${BRIGADE_VERIFY_LOCK_HELD:-}" ]; then
  if [ -n "${BRIGADE_VERIFY_LOCK_PYTHON:-}" ]; then
    lock_python=$BRIGADE_VERIFY_LOCK_PYTHON
  else
    lock_python=${PY:-.venv/bin}/python
  fi
  exec "$lock_python" scripts/verify_lock.py "$@"
fi
```

- [x] Revise `AGENTS.md` so development slices use `verify-focused` through `brigade work verify run`, while pull requests, merges, releases, and verification-infrastructure changes use the full gate once with `--timeout 3600`. State that 3600 bounds the known-long integration gate and is not permission to retry status 75. State that agents must not pass `--no-reuse` for the full gate and must not retry status 75 with a larger timeout. A verifier timeout is a command-duration failure; status 75 is lock contention.

- [x] Run GREEN through Brigade:

```bash
brigade work verify run --target . --argv-json '[".venv/bin/pytest","-q","tests/test_verify_script.py"]' --capture brigade-work --no-reuse
```

Expected: all tests in `tests/test_verify_script.py` pass.

- [x] Run the user-facing focused entrypoint through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_verify_script.py"]' --capture brigade-work --no-reuse
```

Expected: lint, format, mypy, sync checks, and `tests/test_verify_script.py` pass without launching the full suite.

- [x] Inspect `git diff`, run `git diff --check`, and commit:

```bash
git add AGENTS.md scripts/verify scripts/verify-focused tests/test_verify_script.py docs/superpowers/plans/2026-08-25-verification-gate-runtime.md
git commit -m "fix: split development and integration verification gates"
```

### Task 2: Pin the PR full-gate verifier timeout

**Files:**
- Modify: `tests/test_verify_script.py`
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-08-25-verification-gate-runtime-design.md`
- Modify: `docs/superpowers/plans/2026-08-25-verification-gate-runtime.md`

Do not edit scripts, CI, `proc.py`, or unrelated files. Do not commit. Do not change development focused commands.

- [x] Strengthen `test_agents_md_requires_focused_development_and_reserved_full_gate` so the documented full Brigade command must include `--timeout 3600`, while still forbidding `--no-reuse` and retaining the status-75 no-retry rule.
- [x] Run RED through Brigade:

```bash
brigade work verify run --target . --argv-json '[".venv/bin/pytest","-q","tests/test_verify_script.py"]' --capture brigade-work
```

Expected: `test_agents_md_requires_focused_development_and_reserved_full_gate` fails because `AGENTS.md` still documents the full command without `--timeout 3600`.

- [x] Update the full-gate command in `AGENTS.md` to `brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work`. Add one adjacent sentence that 3600 bounds the known-long integration gate and is not permission to retry status 75.
- [x] Update the design and this plan so `--timeout 3600` is pinned and a verifier timeout (command exceeded `--timeout`) is distinct from lock contention (status 75).
- [x] Run GREEN through Brigade:

```bash
brigade work verify run --target . --argv-json '[".venv/bin/pytest","-q","tests/test_verify_script.py"]' --capture brigade-work
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_verify_script.py"]' --capture brigade-work
```

Expected: `tests/test_verify_script.py` and the focused gate pass. Do not commit.
