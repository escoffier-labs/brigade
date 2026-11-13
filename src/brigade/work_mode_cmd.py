"""``brigade work mode`` and ``brigade work rails`` (#2233)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import work_mode
from .config import load_config


def _emit(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"work mode: {payload['mode']}; safety rails: {payload['rails']} ({payload['target']})")


def _reconcile_claude_hooks(target: Path) -> str | None:
    """Re-merge an existing project-scope Claude install after a mode switch.

    Every mode carries the same events; this heals installs written by builds that
    dropped ``Stop`` and ``PostToolUseFailure`` in the off mode, then named yolo.
    """
    from .claude_hooks import install_cmd

    try:
        config = load_config(target)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if config is None or "claude" not in config.selection.harnesses:
        return None
    if not (target / install_cmd.SIDECAR_REL_PATH).is_file():
        if install_cmd.status_payload(target).get("managed_events"):
            return (
                f"managed Claude hooks exist without {install_cmd.SIDECAR_REL_PATH}; "
                f"run `brigade work hooks install --target {target}` so every managed event is installed"
            )
        return None
    if install_cmd.hooks_install(target=target, quiet=True) != 0:
        return f"run `brigade work hooks install --target {target}` after checking .claude/settings.json"
    return "reconciled"


def mode_command(*, target: Path, action: str, mode: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if action == "set":
        assert mode is not None
        error = work_mode.set_mode(target, mode)
        if error is not None:
            print(f"error: {error}", file=sys.stderr)
            return 2
        hooks = _reconcile_claude_hooks(target)
        if hooks is not None and hooks != "reconciled":
            print(f"warning: unable to reconcile Claude hooks; {hooks}", file=sys.stderr)
    from .codex_observe_hooks import codex_observe_hooks_installed, install_codex_observe_hooks

    try:
        cfg = load_config(target)
    except (OSError, ValueError, TypeError):
        cfg = None
    if action == "set" and work_mode.is_observe(target) and cfg is not None and "codex" in cfg.selection.harnesses:
        error = install_codex_observe_hooks(target)
        if error:
            print(f"warning: {error}", file=sys.stderr)
    payload: dict[str, object] = {"target": str(target), **work_mode.summary(target)}
    payload["observe_hooks"] = {"codex": codex_observe_hooks_installed(target)}
    _emit(payload, json_output=json_output)
    return 0


def rails_command(*, target: Path, action: str, json_output: bool = False) -> int:
    """Read or write the per-user rails record for ``target``; nothing in the repository is consulted."""
    target = target.expanduser().resolve()
    if action == "check":
        return 0 if work_mode.rails_enabled(target) else work_mode.RAILS_OFF_EXIT
    ignored = work_mode.ignored_repo_rails_note(target)
    if ignored is not None:
        print(f"warning: {ignored}", file=sys.stderr)
    if action in work_mode.RAILS_STATES:
        was_on = work_mode.rails_enabled(target)
        error = work_mode.set_rails(target, action)
        if error is not None:
            print(f"error: {error}", file=sys.stderr)
            return 2
        if action == "off" and was_on:
            print(work_mode.RAILS_OFF_WARNING.format(target=target), file=sys.stderr)
            if work_mode.pre_push_hook_gated(target) is False:
                print(
                    "warning: hooks/pre-push predates the rails switch and keeps scanning until it is "
                    "refreshed from the current Brigade template",
                    file=sys.stderr,
                )
        top = work_mode.hook_root_mismatch(target) if action == "off" else None
        if top is not None:
            print(
                f"warning: rails are off only for {target}; the pre-push hook checks the repository root {top}, "
                f"so run `brigade work rails off --target {top}` to skip its scans",
                file=sys.stderr,
            )
    payload: dict[str, object] = {"target": str(target), **work_mode.summary(target)}
    record = work_mode.rails_record_path()
    if record is not None:
        payload["rails_record"] = str(record)
    _emit(payload, json_output=json_output)
    return 0
