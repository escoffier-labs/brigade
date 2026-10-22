"""Shared fenced recovery for claims whose local owner died."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path


def _workspace_from_run_dir(raw_run_dir: str | None) -> tuple[Path | None, str]:
    """(workspace, reason) for a claim's recorded run directory.

    Resolution is ``runguard.resolve_run_lock_workspace`` so this CLI path
    cannot drift from every other run-lock caller (#1159). Existence on
    this machine is CLI-only: a missing run directory or a resolved path
    that is not a directory is ``None`` and a refusal reason, never a
    guessed ancestor.
    """
    import json as _json

    from . import runguard

    if not raw_run_dir:
        return None, "the claim records no run directory"
    run_dir = Path(raw_run_dir).expanduser()
    if not run_dir.is_dir():
        return None, f"its run directory {raw_run_dir} does not exist on this machine"
    try:
        meta = _json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        meta = None
    resolved = runguard.resolve_run_lock_workspace(meta if isinstance(meta, dict) else {}, run_dir)
    if resolved is not None and resolved.is_dir():
        return resolved, "ok"
    if resolved is not None:
        return None, f"its recorded workspace {resolved} does not exist on this machine"
    return None, (
        f"its run directory {raw_run_dir} has no readable run.json naming its workspace and is not under a "
        "<workspace>/.brigade/runs layout"
    )


def _print_claim_failure(decision, *, what: str) -> bool:
    """Print a hub/config failure for ``what`` and return True when there was one."""
    if decision.reason == "no-hub":
        print("error: no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)", file=sys.stderr)
        return True
    if decision.reason == "no-identity":
        print(
            f"error: no usable fleet node identity ({decision.detail}); initialize this machine's "
            "identity with `brigade node --machine`",
            file=sys.stderr,
        )
        return True
    if decision.reason == "auth-failed":
        print(
            f"error: fleet hub rejected this node's credentials ({decision.detail}); enroll this machine "
            "with 'brigade fleet nodes add <node_id>' and set [fleet] node_token_file",
            file=sys.stderr,
        )
        return True
    if decision.reason == "hub-unavailable":
        detail = decision.detail or ""
        if "'holder'" in detail or "'scope'" in detail or "'action'" in detail:
            detail += " (this fleet hub predates token-less release; upgrade it)"
        print(f"error: fleet hub claim {what} failed: {detail}", file=sys.stderr)
        return True
    if decision.reason == "invalid":
        print(f"error: {decision.detail}", file=sys.stderr)
        return True
    if not decision.granted and decision.reason not in ("held", "missing"):
        reason = f"active claim quota: {decision.detail or ''}" if decision.reason == "quota" else decision.reason
        print(f"error: fleet hub claim {what} refused: {reason}", file=sys.stderr)
        return True
    return False


def _release_claim(
    raw_target: str,
    *,
    as_path: bool,
    node_override: str | None,
    force: bool,
    as_json: bool,
    expected_run_dir: Path | None = None,
    hold_run_lock: bool = False,
) -> int:
    """``brigade fleet claims --release`` (issue #1141): free a claim whose
    holder token died with its run. Without ``force``, both modes run the
    same proof: this node must own the claim, its recorded run directory
    must resolve to a workspace on this machine (with ``--path``, the one
    given), and that workspace's run lock must have no live owner; the
    release is then fenced to the inspected row. When the row records no
    run directory, ``--path`` proves deadness via the pointed-at
    workspace's own run.lock (#1160)."""
    import json as _json

    from . import fleet_client, runguard
    from .fleet_claim_target import ClaimTargetError

    workspace: Path | None = None
    if as_path:
        candidate = Path(raw_target).expanduser()
        if not candidate.is_dir():
            print(f"error: --path target is not a directory: {raw_target}", file=sys.stderr)
            return 1
        try:
            workspace = candidate.resolve()
        except OSError:
            workspace = candidate
        # Never an ancestor walk: the directory must be the workspace itself.
        # The per-user home identity is not a repo workspace (#1639).
        enclosing = fleet_client.find_workspace_for_path(workspace)
        if (
            enclosing is not None
            and enclosing != workspace
            and enclosing != fleet_client.home_identity_target().resolve()
        ):
            print(
                f"error: {workspace} is inside the workspace {enclosing}; pass that workspace path instead",
                file=sys.stderr,
            )
            return 1
        # The same resolver `brigade run` claims with, so release cannot diverge.
        try:
            target = fleet_client.resolve_claim_target(workspace)
        except ClaimTargetError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        local_node = fleet_client.resolve_node_id(workspace)
    else:
        target = raw_target
        local_node = fleet_client.resolve_node_id()
    if node_override is not None and not fleet_client._node_id_is_claimable(node_override):
        print(f"error: --node {node_override!r} is not a usable fleet node identity", file=sys.stderr)
        return 1
    if node_override is not None and node_override != local_node and not force:
        print(
            f"error: --node {node_override} is not this node's identity ({local_node}); releasing another "
            "node's claim requires --force",
            file=sys.stderr,
        )
        return 1
    node_id = node_override or local_node
    if not fleet_client._node_id_is_claimable(node_id):
        # ``--node`` cannot repair a missing local identity: any other value
        # differs from ``unknown`` and is refused without ``--force`` (#1160).
        print(
            f"error: no usable fleet node identity ({node_id}); initialize this machine's "
            "identity with `brigade node --machine`",
            file=sys.stderr,
        )
        return 1
    inspected_acquired_at: str | None = None
    run_workspace: Path | None = None
    if not force:
        probe = fleet_client.inspect_claim(target, node_id=node_id)
        if _print_claim_failure(probe, what="lookup"):
            return 1
        if probe.claim is None or probe.claim.get("owner_node") != node_id:
            # Nothing this node verifiably owns: an unfenced delete here
            # could hit a row a fresh run acquires between this probe and
            # the release. Refuse; only --force deletes unverified.
            if as_json:
                receipt = {
                    "target": target,
                    "released": False,
                    "forced": False,
                    "node_id": node_id,
                    "claim": None,
                    "owner": probe.claim,
                }
                if expected_run_dir is not None and probe.granted:
                    receipt["reason"] = "no-claim" if probe.claim is None else "foreign-owner"
                print(_json.dumps(receipt, indent=2, sort_keys=True))
            if probe.claim is None:
                print(
                    f"error: no claim owned by this node ({node_id}) on {target!r}; "
                    "use --force to delete another owner's or an unverifiable row",
                    file=sys.stderr,
                )
            else:
                print(
                    f"error: claim on {target!r} is held by node {probe.claim.get('owner_node') or '-'}, not "
                    f"this node ({node_id}); pass --force to release it anyway",
                    file=sys.stderr,
                )
            return 1
        if expected_run_dir is not None and (
            not probe.lock_run_dir or Path(probe.lock_run_dir).expanduser().resolve() != expected_run_dir.resolve()
        ):
            # A missing generation is unknown, not evidence of a different run.
            if as_json and probe.granted and probe.lock_run_dir:
                print(
                    _json.dumps(
                        {
                            "target": target,
                            "released": False,
                            "forced": False,
                            "node_id": node_id,
                            "claim": None,
                            "owner": probe.claim,
                            "reason": "different-run-generation",
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            print("error: claim belongs to a different run generation", file=sys.stderr)
            return 1
        if probe.lock_run_dir:
            run_workspace, why = _workspace_from_run_dir(probe.lock_run_dir)
            if run_workspace is None:
                print(
                    f"error: cannot verify that the run holding {target!r} is dead ({why}); "
                    "pass --force to release it anyway",
                    file=sys.stderr,
                )
                return 1
            if workspace is not None and run_workspace != workspace:
                print(
                    f"error: the claim on {target!r} was taken by a run in {run_workspace}, not {workspace}; "
                    f"release it from that workspace (--release {run_workspace} --path) or pass --force",
                    file=sys.stderr,
                )
                return 1
        elif workspace is not None:
            # --path: the operator pointed at the workspace. When the row
            # recorded no run_dir (--no-artifacts, or a pre-lease claim),
            # prove deadness via that workspace's own run.lock (#1160).
            run_workspace = workspace
        else:
            print(
                f"error: cannot verify that the run holding {target!r} is dead "
                "(the claim records no run directory); pass --force to release it anyway",
                file=sys.stderr,
            )
            return 1
        verdict = runguard.inspect_run_lock_reconcile(run_workspace)
        if verdict == "live":
            print(
                f"error: a run owner is still alive on the run lock of {run_workspace}; refusing to "
                f"release {target!r} (pass --force to release it anyway)",
                file=sys.stderr,
            )
            return 1
        if verdict == "invalid":
            print(
                f"error: the run lock of {run_workspace} is malformed, so liveness cannot be verified; "
                f"refusing to release {target!r} (pass --force to release it anyway)",
                file=sys.stderr,
            )
            return 1
        if expected_run_dir is not None and runguard.run_lock_state(run_workspace, expected_run_dir) not in {
            "stale",
            "absent",
        }:
            print("error: live or foreign run lock prevents patrol release", file=sys.stderr)
            return 1
        acquired_at = probe.claim.get("acquired_at")
        if expected_run_dir is not None and (not isinstance(acquired_at, str) or not acquired_at):
            print("error: claim generation is unavailable for patrol release", file=sys.stderr)
            return 1
        inspected_acquired_at = acquired_at if isinstance(acquired_at, str) else None
    # Patrol holds the matching run lock through the Hub write so a resume
    # cannot start against the inspected generation while it is released.
    fence = (
        runguard.run_lock(run_workspace, run_dir=expected_run_dir, wait_seconds=0, stale_action="claim")
        if hold_run_lock and expected_run_dir is not None and run_workspace is not None
        else nullcontext()
    )
    try:
        with fence:
            decision = fleet_client.release_claim(
                target, node_id=node_id, force=force, acquired_at=inspected_acquired_at
            )
    except runguard.RunLockError:
        print("error: run lock acquisition refused for patrol release", file=sys.stderr)
        return 1
    if _print_claim_failure(decision, what="release"):
        return 1
    if as_json:
        payload = {
            "target": target,
            "released": decision.granted,
            "forced": force,
            "node_id": node_id,
            "claim": decision.claim,
            "owner": decision.owner,
        }
        print(_json.dumps(payload, indent=2, sort_keys=True))
    if decision.granted:
        claim = decision.claim or {}
        if not as_json:
            print(
                f"released claim on {target!r} held by node {claim.get('owner_node') or '-'} "
                f"(conductor {claim.get('owner_conductor') or '-'})"
            )
        return 0
    if decision.reason == "held":
        owner = decision.owner or {}
        if owner.get("owner_node") == node_id:
            print(
                f"error: the claim on {target!r} was re-acquired since it was inspected (now acquired "
                f"{owner.get('acquired_at') or '-'}); not released",
                file=sys.stderr,
            )
        else:
            print(
                f"error: claim on {target!r} is held by node {owner.get('owner_node') or '-'}, not this node "
                f"({node_id}); pass --force to release it anyway",
                file=sys.stderr,
            )
        return 1
    if not as_json:
        print(f"no active claim on {target!r}")
    return 0
