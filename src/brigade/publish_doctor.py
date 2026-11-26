"""Doctor diagnostics for publication hooks and their scanner."""

from __future__ import annotations

import subprocess
from pathlib import Path


def push_gate_checks(target: Path) -> list[tuple[str, str, str]]:
    from .work_cmd import push_gate

    results: list[tuple[str, str, str]] = []

    try:
        repo = push_gate.Repository.locate(target)
        if repo is not None:
            info = push_gate.status(repo)
            severity = "INFO"
            if info["mode"] == "enforce" and not info["enforce_effective"]:
                severity = "FAIL"
            elif (
                info["invalid_mode"]
                or info["ignored_mode"] is not None
                or (info["mode"] == "warn" and (not info["will_run"] or not info["current"]))
            ):
                severity = "WARN"
            results.append((severity, "verify push gate", push_gate.describe(info)))
            from . import config
            from .work_cmd.verify_push import _RECEIPT_LIMIT

            keep = config.resolve_verify_runs_keep(repo.root)
            if keep > _RECEIPT_LIMIT:
                results.append(
                    (
                        "WARN",
                        "verify run retention",
                        f"verify_runs_keep is {keep} in {config.CONFIG_REL_PATH}; the push gate can scan at most {_RECEIPT_LIMIT}.",
                    )
                )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        results.append(("WARN", "verify push gate", "could not inspect the hook or its log"))
    return results
