from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

_BARE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_bare_run_id(run_id: str) -> bool:
    """True when *run_id* is a single run-directory name, not a path.

    Mirrors the containment check in ``runs_cmd._is_bare_run_id`` so an
    untrusted run_id cannot escape the runs root with ``..``, path separators,
    or an absolute path.
    """
    if not isinstance(run_id, str) or not run_id or run_id in {".", ".."}:
        return False
    if os.sep in run_id or (os.altsep is not None and os.altsep in run_id):
        return False
    path = Path(run_id)
    if path.is_absolute() or bool(path.anchor) or len(path.parts) != 1:
        return False
    if path.parts[0] in {".", ".."}:
        return False
    return bool(_BARE_RUN_ID_RE.fullmatch(run_id))


def trailer(run_id: str) -> int:
    from .causal_receipt import receipt_digest

    if not _is_bare_run_id(run_id):
        print("unknown run")
        return 1

    run_json = Path(".brigade/runs") / run_id / "run.json"
    if not run_json.is_file():
        print("unknown run")
        return 1
    try:
        with run_json.open("r", encoding="utf-8") as f:
            receipt = json.load(f)
    except Exception:
        print("unknown run")
        return 1

    digest = receipt_digest(receipt)
    print(f"Brigade-Run: {run_id}")
    print(f"Brigade-Receipt: sha256:{digest}")
    return 0


def verify_commit(commit_sha: str, target: Path = Path(".")) -> int:
    from .causal_receipt import receipt_digest

    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%B", commit_sha],
            stderr=subprocess.STDOUT,
            cwd=str(target),
        )
    except (subprocess.CalledProcessError, OSError):
        print("missing trailer")
        return 1

    msg = out.decode("utf-8", errors="replace")
    run_id = None
    expected_digest = None

    for line in msg.splitlines():
        if line.startswith("Brigade-Run: "):
            run_id = line[len("Brigade-Run: ") :].strip()
        elif line.startswith("Brigade-Receipt: sha256:"):
            expected_digest = line[len("Brigade-Receipt: sha256:") :].strip()

    if not run_id or not expected_digest:
        print("missing trailer")
        return 1

    if not _is_bare_run_id(run_id):
        print("unknown run")
        return 1

    run_json = target / ".brigade/runs" / run_id / "run.json"
    if not run_json.is_file():
        print("unknown run")
        return 1

    try:
        with run_json.open("r", encoding="utf-8") as f:
            receipt = json.load(f)
    except Exception:
        print("unknown run")
        return 1

    actual_digest = receipt_digest(receipt)
    if actual_digest != expected_digest:
        print("digest mismatch")
        return 1

    print("ok")
    return 0
