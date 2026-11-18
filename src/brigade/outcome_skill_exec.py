"""Physical side effects of an outcome reconcile decision for a skill artifact.

Reconcile runs unattended, so it never rolls back, uninstalls or promotes over a
hand-edited installed copy: that copy is the operator's, and only the explicit
``--discard-local-edits`` commands may replace it. A copy no valid receipt can
vouch for (receipt deleted, corrupt or missing its digests) is a veto too: the
receipt lives in the workspace the agent's own user can write, so it may stop
a write but never authorize one. The functions return the execution strings that
``outcome reconcile`` records in decision receipts.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
from pathlib import Path
from typing import Any

SKIPPED_LOCAL_EDITS = "rollback-skipped: local edits"
SKIPPED_UNVERIFIED = "skipped: unverified (no valid receipt)"


def _run(fn: Any, **kwargs: Any) -> tuple[int, str]:
    """Call a noisy command function, returning its exit code and captured stdout."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        return fn(**kwargs), out.getvalue()


def _install_result(stdout: str) -> str:
    """Judge an ``install(harness="all")`` JSON payload, not just its exit code.

    Exit 0 also covers targets being skipped (hand-edited or unverifiable
    copies, other skips). A promotion is global routing authority, so it needs
    every applicable target freshly installed: one skipped target, even beside
    a successful one, keeps the prior status. A target that cannot apply at all
    (no Hermes home) is not counted.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return "install-failed: unreadable install result"
    if not isinstance(payload, dict):
        return "install-failed: unreadable install result"
    receipt = payload.get("receipt")
    skipped = receipt.get("skipped") if isinstance(receipt, dict) else None
    rows = [row for row in skipped if isinstance(row, dict)] if isinstance(skipped, list) else []
    blocking = [row for row in rows if not row.get("not_applicable")]
    if payload.get("installed") is True and not blocking:
        return "installed"
    edited = sum(1 for row in blocking if row.get("local_edit"))
    unverified = sum(1 for row in blocking if row.get("unverified"))
    if payload.get("installed") is True:
        return f"install-failed: partial ({len(blocking)} skipped: {edited} edited, {unverified} unverified)"
    return f"install-failed: nothing installed ({len(rows)} targets skipped, {edited} with local edits)"


def _skipped_state(target: Path, artifact_id: str, harness: str) -> str | None:
    """The veto outcome for a harness copy that must not be rolled back or removed, if any."""
    state = importlib.import_module("brigade.skills_cmd.install").installed_local_edit(target, artifact_id, harness)
    if state == "changed":
        return f"{harness}:{SKIPPED_LOCAL_EDITS}"
    if state == "unknown":
        return f"{harness}:{SKIPPED_UNVERIFIED}"
    return None


def execute(target: Path, artifact_id: str, action: str) -> str:
    """Perform a decision's physical side effect for a skill artifact.

    install -> install across all harnesses. rollback -> restore the last good
    snapshot per harness, or uninstall when a first install has no prior
    snapshot (the first-install-safe rule); a copy with local edits is skipped
    for both. Defensive: a skills failure is recorded, never raised, so one
    artifact cannot abort the autonomous run.
    """
    from . import skills_cmd

    try:
        if action == "install":
            if not skills_cmd._registry_entry_present(target, artifact_id):
                # The ledger named an artifact that was never accepted into the
                # registry, so there is nothing to install. Report it distinctly
                # instead of a generic rc failure; reconcile keeps it a candidate.
                return "install-skipped: not in registry"
            rc, stdout = _run(
                skills_cmd.install,
                workspace=target,
                skill=artifact_id,
                harness="all",
                force=True,
                unattended=True,
                json_output=True,
            )
            return _install_result(stdout) if rc == 0 else f"install-failed(rc={rc})"
        if action == "rollback":
            outcomes: list[str] = []
            for harness in skills_cmd._install_targets(target):
                if (veto := _skipped_state(target, artifact_id, harness)) is not None:
                    outcomes.append(veto)
                    continue
                rc, _ = _run(
                    skills_cmd.rollback,
                    workspace=target,
                    skill=artifact_id,
                    harness=harness,
                    unattended=True,
                    json_output=True,
                )
                if rc == 0:
                    outcomes.append(f"{harness}:rollback")
                    continue
                # Checked again: the uninstall fallback must never remove an edited
                # or unverifiable copy, and a copy that changed during the failed
                # rollback is the operator's now.
                if (veto := _skipped_state(target, artifact_id, harness)) is not None:
                    outcomes.append(veto)
                    continue
                rc, _ = _run(
                    skills_cmd.uninstall,
                    workspace=target,
                    skill=artifact_id,
                    harness=harness,
                    unattended=True,
                    json_output=True,
                )
                if rc == 2 and (veto := _skipped_state(target, artifact_id, harness)) is not None:
                    outcomes.append(veto)
                    continue
                outcomes.append(f"{harness}:uninstall" if rc == 0 else f"{harness}:noop")
            return "reverted:" + ",".join(outcomes)
        return "noop"
    except Exception as exc:  # noqa: BLE001 - autonomy must survive any skills failure
        return f"error:{type(exc).__name__}"
