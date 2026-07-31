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
