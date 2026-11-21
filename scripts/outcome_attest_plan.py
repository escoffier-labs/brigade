#!/usr/bin/env python3
"""List the verify manifests a pushed commit range touches, as a JSON array of manifest ids.

Used by .github/workflows/outcome-attest.yml to build the job matrix. Reads only the
tracked manifests under verify/manifests/ and `git diff --name-only <before> <after>`.
Prints `[]` when nothing is touched or the range cannot be resolved, so the attest job
is skipped and nothing is attested by accident.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ZERO_SHA = "0" * 40


def changed_paths(repo: Path, before: str, after: str) -> list[str]:
    base = f"{after}^" if not before or before == ZERO_SHA else before
    completed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", base, after],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--before", default="")
    parser.add_argument("--after", required=True)
    args = parser.parse_args(argv)
    from brigade import verify_manifest

    repo = Path(args.repo).resolve()
    paths = changed_paths(repo, args.before, args.after)
    ids = sorted({m.manifest_id for m in verify_manifest.manifests_matching_paths(repo, paths)})
    print(json.dumps(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
