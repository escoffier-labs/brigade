"""init-git verb: bootstrap a memory dir as a git repo with one initial commit."""

from __future__ import annotations

import subprocess
import sys

from brigade.memory_doctor.git import is_git_repo
from brigade.memory_doctor.paths import PathConfig


def run(cfg: PathConfig) -> int:
    memory_dir = cfg.memory_dir
    if not memory_dir.exists():
        print(f"brigade memory init-git: memory dir does not exist: {memory_dir}", file=sys.stderr)
        return 2
    if is_git_repo(memory_dir):
        print(
            f"brigade memory init-git: memory dir is already a git repo: {memory_dir}",
            file=sys.stderr,
        )
        return 2

    # Initialize with `main` as the default branch for predictability across
    # git versions; older defaults of `master` are inconsistent.
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(memory_dir)], check=True)

    # Reserve .gitignore as a customization point even though we don't
    # exclude anything yet. Operators frequently add `.DS_Store` or editor
    # backup patterns; better to have the file present than discover the
    # need later.
    gitignore = memory_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("")

    cards = sorted(p for p in memory_dir.glob("*.md") if p.name != "MEMORY.md")
    has_index = (memory_dir / "MEMORY.md").exists()
    pieces = [f"{len(cards)} cards"]
    if has_index:
        pieces.append("MEMORY.md")
    summary = ", ".join(pieces)
    subject = f"memory: initial import ({summary})"

    subprocess.run(["git", "-C", str(memory_dir), "add", "-A"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(memory_dir), "commit", "--quiet", "-m", subject],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode != 0:
        print(
            f"brigade memory init-git: initial commit failed: {commit.stderr.strip()}",
            file=sys.stderr,
        )
        return 2

    sha = subprocess.run(
        ["git", "-C", str(memory_dir), "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    print(f"brigade memory init-git: initialized {memory_dir} at {sha}")
    return 0
