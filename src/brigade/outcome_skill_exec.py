"""Physical side effects of an outcome reconcile decision for a skill artifact.

Reconcile runs unattended, so it never rolls back, uninstalls or promotes over a
hand-edited installed copy: that copy is the operator's, and only the explicit
``--discard-local-edits`` commands may replace it. The functions return the
execution strings that ``outcome reconcile`` records in decision receipts.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
from pathlib import Path
from typing import Any

SKIPPED_LOCAL_EDITS = "rollback-skipped: local edits"


def _run(fn: Any, **kwargs: Any) -> tuple[int, str]:
    """Call a noisy command function, returning its exit code and captured stdout."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        return fn(**kwargs), out.getvalue()


def _install_result(stdout: str) -> str:
    """Judge an ``install(harness="all")`` JSON payload, not just its exit code.

    Exit 0 also covers every target being skipped (hand-edited copies, other
    skips), which installs nothing and must not count as a promotion.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return "install-failed: unreadable install result"
    if not isinstance(payload, dict):
        return "install-failed: unreadable install result"
    if payload.get("installed") is True:
        return "installed"
    receipt = payload.get("receipt")
    skipped = receipt.get("skipped") if isinstance(receipt, dict) else None
    rows = skipped if isinstance(skipped, list) else []
    edited = sum(1 for row in rows if isinstance(row, dict) and row.get("local_edit"))
    return f"install-failed: nothing installed ({len(rows)} targets skipped, {edited} with local edits)"


def execute(target: Path, artifact_id: str, action: str) -> str:
    """Perform a decision's physical side effect for a skill artifact.

    install -> install across all harnesses. rollback -> restore the last good
    snapshot per harness, or uninstall when a first install has no prior
    snapshot (the first-install-safe rule); a copy with local edits is skipped
    for both. Defensive: a skills failure is recorded, never raised, so one
    artifact cannot abort the autonomous run.
    """
    from . import skills_cmd

    install_mod = importlib.import_module("brigade.skills_cmd.install")
    try:
        if action == "install":
            if not skills_cmd._registry_entry_present(target, artifact_id):
                # The ledger named an artifact that was never accepted into the
                # registry, so there is nothing to install. Report it distinctly
                # instead of a generic rc failure; reconcile keeps it a candidate.
                return "install-skipped: not in registry"
            rc, stdout = _run(
                skills_cmd.install, workspace=target, skill=artifact_id, harness="all", force=True, json_output=True
            )
            return _install_result(stdout) if rc == 0 else f"install-failed(rc={rc})"
        if action == "rollback":
            outcomes: list[str] = []
            for harness in skills_cmd._install_targets(target):
                if install_mod.installed_local_edit(target, artifact_id, harness) == "changed":
                    outcomes.append(f"{harness}:{SKIPPED_LOCAL_EDITS}")
                    continue
                rc, _ = _run(
                    skills_cmd.rollback, workspace=target, skill=artifact_id, harness=harness, json_output=True
                )
                if rc == 0:
                    outcomes.append(f"{harness}:rollback")
                    continue
                # Checked again: the uninstall fallback must never remove an edited copy.
                if install_mod.installed_local_edit(target, artifact_id, harness) == "changed":
                    outcomes.append(f"{harness}:{SKIPPED_LOCAL_EDITS}")
                    continue
                rc, _ = _run(
                    skills_cmd.uninstall, workspace=target, skill=artifact_id, harness=harness, json_output=True
                )
                outcomes.append(f"{harness}:uninstall" if rc == 0 else f"{harness}:noop")
            return "reverted:" + ",".join(outcomes)
        return "noop"
    except Exception as exc:  # noqa: BLE001 - autonomy must survive any skills failure
        return f"error:{type(exc).__name__}"
