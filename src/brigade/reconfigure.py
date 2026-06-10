"""brigade reconfigure - adjust an existing install to a new Selection."""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import load_config
from .install import install_selection
from .selection import Selection


_WRITER_DIRS = {
    "claude": ".claude",
    "codex": ".codex",
}
_READER_DIRS = {
    "openclaw": ".brigade/openclaw",
    "hermes": ".brigade/hermes",
}
_HARNESS_BRIDGE_FILES = {
    "claude": ["CLAUDE.md"],
    "codex": [],
}


def reconfigure(target: Path, new_selection: Selection, prune: bool) -> int:
    target = target.expanduser().resolve()
    new_selection.validate()
    existing = load_config(target)
    old_harnesses = set(existing.selection.harnesses) if existing else set()
    new_harnesses = set(new_selection.harnesses)

    added = new_harnesses - old_harnesses
    removed = old_harnesses - new_harnesses

    # Re-run install with force=True to lay down baseline + new-harness files.
    # install_selection is idempotent: unchanged files re-render identically.
    rc = install_selection(target, new_selection, force=True)
    if rc != 0:
        return rc

    # Prune removed harnesses if requested.
    if prune:
        for h in sorted(removed):
            wdir = _WRITER_DIRS.get(h)
            if wdir and (target / wdir).is_dir():
                shutil.rmtree(target / wdir)
            for bridge in _HARNESS_BRIDGE_FILES.get(h, []):
                bp = target / bridge
                if bp.is_file():
                    bp.unlink()
            rdir = _READER_DIRS.get(h)
            if rdir and (target / rdir).is_dir():
                shutil.rmtree(target / rdir)
            print(f"brigade: pruned {h}")

    print(f"brigade: reconfigured -> harnesses={','.join(new_selection.harnesses) or '(none)'}")
    if added:
        print(f"  added:   {','.join(sorted(added))}")
    if removed:
        verb = "pruned" if prune else "orphaned (use --prune to delete)"
        print(f"  removed: {','.join(sorted(removed))} ({verb})")
    return 0
