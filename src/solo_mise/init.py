"""`solo-mise init` — materialize a profile into a target directory."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable, List

from .templates import (
    harness_memory_owner,
    is_text,
    load_profile,
    render,
    template_root,
)


def run(
    target: Path,
    profile_id: str = "repo",
    force: bool = False,
    dry_run: bool = False,
    harness: str | None = None,
    allow_home: bool = False,
) -> int:
    """Materialize `profile_id` into `target`. Returns process exit code."""
    target = target.expanduser().resolve()

    if target == Path.home() and not allow_home:
        print(
            f"error: refusing to install profile '{profile_id}' directly into $HOME ({target}).",
            file=sys.stderr,
        )
        print(
            "  Pick a subdirectory (e.g. ~/agent-kitchen) or pass --allow-home to override.",
            file=sys.stderr,
        )
        return 5

    try:
        profile = load_profile(profile_id)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    memory_owner_id = harness or profile.get("memory_owner_default", "this-repo")
    memory_owner_name = harness_memory_owner(memory_owner_id, memory_owner_id)
    context = {
        "memory_owner": memory_owner_id,
        "memory_owner_name": memory_owner_name,
        "profile": profile_id,
        "harness": memory_owner_id,
    }

    root = template_root()
    files: List[dict] = profile.get("files", [])
    dirs: List[str] = profile.get("dirs", [])

    # Validate every manifest path before touching the filesystem. A
    # malformed or compromised profile JSON must not let us write outside
    # `target` or read outside `root`.
    try:
        for entry in files:
            _ensure_safe_rel(entry["src"], label="profile file src")
            _ensure_safe_rel(entry["dst"], label="profile file dst")
        for d in dirs:
            _ensure_safe_rel(d, label="profile dir")
    except ValueError as exc:
        print(f"error: profile '{profile_id}' invalid: {exc}", file=sys.stderr)
        return 6

    if dry_run:
        print(f"[dry-run] target: {target}")
        print(f"[dry-run] profile: {profile_id}")
        print(f"[dry-run] memory owner: {memory_owner_name}")
        print(f"[dry-run] would create {len(dirs)} dir(s) and {len(files)} file(s):")
        for d in dirs:
            print(f"  dir   {target / d}")
        for entry in files:
            print(f"  file  {target / entry['dst']}")
        return 0

    target.mkdir(parents=True, exist_ok=True)

    # Pre-flight: refuse to overwrite without --force.
    if not force:
        conflicts = _existing_files(target, [f["dst"] for f in files])
        if conflicts:
            print(
                "error: refusing to overwrite existing files (use --force):",
                file=sys.stderr,
            )
            for c in conflicts:
                print(f"  {c}", file=sys.stderr)
            return 3

    # Create directories.
    for d in dirs:
        dest = target / d
        dest.mkdir(parents=True, exist_ok=True)

    # Copy files.
    for entry in files:
        src = root / entry["src"]
        dst = target / entry["dst"]
        mode_str = entry.get("mode")
        if not src.is_file():
            print(f"error: template missing: {src}", file=sys.stderr)
            return 4
        dst.parent.mkdir(parents=True, exist_ok=True)
        if is_text(entry["src"]):
            text = render(src.read_text(), context)
            dst.write_text(text)
        else:
            shutil.copyfile(src, dst)
        if mode_str:
            dst.chmod(int(mode_str, 8))

    # Post-install notes.
    print(f"solo-mise: installed profile '{profile_id}' to {target}")
    print(f"solo-mise: memory owner -> {memory_owner_name}")
    notes = profile.get("post_install_notes", [])
    if notes:
        print()
        print("Next steps:")
        for note in notes:
            print(f"  - {note}")
    return 0


def _existing_files(target: Path, rel_paths: Iterable[str]) -> List[Path]:
    return [target / p for p in rel_paths if (target / p).exists()]


def _ensure_safe_rel(raw: str, label: str) -> None:
    """Reject absolute paths and `..` segments in profile manifest entries."""
    if not raw or not isinstance(raw, str):
        raise ValueError(f"{label}: empty or non-string entry: {raw!r}")
    p = PurePosixPath(raw)
    if p.is_absolute() or (len(raw) > 0 and raw[0] in "\\/"):
        raise ValueError(f"{label}: absolute paths not allowed: {raw!r}")
    if any(part == ".." for part in p.parts):
        raise ValueError(f"{label}: parent-dir segments not allowed: {raw!r}")
