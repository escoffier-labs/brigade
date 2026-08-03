from pathlib import Path
import subprocess
from typing import Any

from brigade import aboyeur, runguard


def _ignore_brigade_runtime(workspace: Path) -> None:
    """Keep harness artifacts out of git ground-truth comparisons in test repos."""
    if not runguard.is_git_worktree(workspace):
        return
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    exclude_path = Path(result.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = workspace / exclude_path
    try:
        existing = exclude_path.read_text()
    except FileNotFoundError:
        existing = ""
    pattern = "/.brigade/"
    if pattern in existing.splitlines():
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not existing or existing.endswith("\n") else "\n"
    exclude_path.write_text(f"{existing}{separator}{pattern}\n")


# Pin seat health healthy inside a child script that must reach a later phase.
#
# A child process inherits none of tests/conftest.py, so the autouse
# _seat_health_probe_reports_healthy fixture cannot reach it. What the child
# does inherit is the bare-host condition: no seat CLI resolves on a clean
# runner, so every declared seat probes executable-unavailable and #578 slice B
# aborts the run before planning. A subprocess test that waits on a phase past
# admission never sees it. Tests that only need the run to start, and care about
# how fast it gets there, stub _write_run_seat_health_receipt instead.
HEALTHY_SEAT_HEALTH_CHILD_SETUP = """
from brigade import seat_health as _seat_health

_real_seat_health_probe = _seat_health.SeatHealthProbe


class _HealthySeatAdapter:
    def check(self, name, *, seat, roster, workspace, timeout_seconds):
        return _seat_health.SeatHealthCheck(name, "passed", "healthy under test")


_seat_health.SeatHealthProbe = lambda **kwargs: _real_seat_health_probe(adapter=_HealthySeatAdapter())
"""


def run_aboyeur_guarded(*args: Any, **kwargs: Any) -> int:
    """Call ``aboyeur.run`` under the same run guard used by the CLI."""
    output_dir = kwargs.get("output_dir")
    if output_dir is None:
        return aboyeur.run(*args, **kwargs)

    run_dir = Path(output_dir).expanduser().resolve()

    explicit_workspace = kwargs.get("lock_workspace")
    requested_workspace = explicit_workspace or kwargs.get("cwd")
    workspace_path = (
        Path(explicit_workspace or requested_workspace).expanduser().resolve()
        if explicit_workspace is not None or requested_workspace is not None
        else run_dir.parent / f".{run_dir.name}-lock-workspace"
    )
    workspace_path.mkdir(parents=True, exist_ok=True)
    kwargs["lock_workspace"] = workspace_path
    _ignore_brigade_runtime(workspace_path)
    if runguard.is_active_run_owner(workspace_path, run_dir):
        return aboyeur.run(*args, **kwargs)
    task = args[0] if args else kwargs.get("task")
    roster = args[1] if len(args) > 1 else kwargs.get("roster")
    raw_cwd = kwargs.get("cwd")
    cwd = Path(raw_cwd).expanduser().resolve() if raw_cwd is not None else None
    aboyeur.record_run_start(
        run_dir,
        task=task,
        cwd=cwd,
        roster=roster,
        read_only=bool(kwargs.get("read_only", False)),
        worker=kwargs.get("worker"),
        dry_run=bool(kwargs.get("dry_run", False)),
        lock_workspace=workspace_path,
        codex_transport=kwargs.get("codex_transport") or roster.codex_transport,
        scheduler=kwargs.get("scheduler", "waves"),
    )
    with runguard.run_lock(workspace_path, run_dir=run_dir):
        return aboyeur.run(*args, **kwargs)
