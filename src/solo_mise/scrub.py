"""`solo-mise scrub` — run the content-guard scanner against a target."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run(
    target: Path,
    policy: str = "public-repo",
    dry_run: bool = False,
) -> int:
    target = target.expanduser().resolve()
    scanner_dir = Path(
        os.environ.get("CONTENT_GUARD_DIR", str(Path.home() / "repos" / "content-guard"))
    )

    if not scanner_dir.is_dir():
        print(
            f"solo-mise scrub: content-guard not found at {scanner_dir}",
            file=sys.stderr,
        )
        print(
            "solo-mise scrub: clone https://github.com/solomonneas/content-guard "
            "or set CONTENT_GUARD_DIR",
            file=sys.stderr,
        )
        return 2

    try:
        policy_path = _resolve_policy(target, scanner_dir, policy)
    except ValueError as exc:
        print(f"solo-mise scrub: {exc}", file=sys.stderr)
        return 4
    if not policy_path.is_file():
        print(f"solo-mise scrub: policy not found: {policy_path}", file=sys.stderr)
        return 3

    cmd = [
        sys.executable,
        "-m",
        "content_guard",
        "scan",
        str(target),
        "--policy",
        str(policy_path),
    ]
    if dry_run:
        print("solo-mise scrub: would run:")
        print(" ", " ".join(cmd))
        print(f"  PYTHONPATH={scanner_dir / 'src'}")
        return 0

    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{scanner_dir / 'src'}{os.pathsep}{existing_pp}" if existing_pp else str(scanner_dir / "src")
    )
    return subprocess.call(cmd, env=env)


def _resolve_policy(target: Path, scanner_dir: Path, policy: str) -> Path:
    """Resolve a policy name to a JSON path.

    Lookup order:
      1. If `policy` looks like a path (contains `/` or `\\` or ends in `.json`),
         treat it as a literal file path and use it as-is.
      2. Otherwise, treat it as a basename and search the safe lookup chain:
         `<target>/.solo-mise/policies/<policy>.json`, then
         `<scanner_dir>/policies/<policy>.json`.
    """
    looks_like_path = "/" in policy or "\\" in policy or policy.endswith(".json")
    if looks_like_path:
        return Path(policy)

    # Bare name: must be a simple slug, no path segments.
    safe = policy.strip()
    if not safe or any(c in safe for c in ("/", "\\", "..")):
        raise ValueError(f"unsafe policy name: {policy!r}")

    candidates = [
        target / ".solo-mise" / "policies" / f"{safe}.json",
        scanner_dir / "policies" / f"{safe}.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]  # caller prints "not found" with this path
