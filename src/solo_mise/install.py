"""install_selection - the new install engine.

Composes a depth manifest + N harness manifests + M include manifests
into a single deduped file/dir list, then copies+renders into target.
Persists the Selection to .solo-mise/config.json.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

from .config import Config, write_config
from .selection import Selection
from .templates import (
    harness_memory_owner,
    is_text,
    load_depth_manifest,
    load_harness_manifest,
    load_include_manifest,
    render,
    template_root,
)

GITIGNORE_BEGIN = "# >>> solo-mise gitignore block >>>"
GITIGNORE_END = "# <<< solo-mise gitignore block <<<"

# Writer harness -> inbox-dir prefix. Only writer harnesses have an inbox.
_WRITER_INBOX = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
}


def build_gitignore_block(selection: Selection) -> str:
    lines = [
        GITIGNORE_BEGIN,
        "# Managed by `solo-mise init`. Edit between the markers to customize.",
        "# Re-running `solo-mise init` replaces only the content between markers.",
        "",
    ]
    for h in selection.harnesses:
        inbox = _WRITER_INBOX.get(h)
        if inbox:
            lines.extend([
                f"# {h}: handoffs are session-local and may contain private context.",
                f"{inbox}/*",
                f"!{inbox}/TEMPLATE.md",
                f"!{inbox}/.gitkeep",
                "",
            ])
    lines.extend([
        "# Daily session logs are machine-local raw context.",
        "memory/20[0-9][0-9]-[0-1][0-9]-[0-3][0-9].md",
        "",
        "# Review inbox: ambiguous handoffs awaiting human triage.",
        "memory/handoff-inbox/",
        "",
        "# solo-mise local state (logs, scrub cache).",
        ".solo-mise/logs/",
        ".solo-mise/scrub-cache/",
        GITIGNORE_END,
        "",
    ])
    return "\n".join(lines)


def apply_gitignore(target: Path, selection: Selection) -> str:
    """Insert or replace the managed block in target's .gitignore. Returns 'created' or 'updated'."""
    gi = target / ".gitignore"
    block = build_gitignore_block(selection)
    if not gi.exists():
        gi.write_text(block)
        return "created"
    existing = gi.read_text()
    if GITIGNORE_BEGIN in existing and GITIGNORE_END in existing:
        prefix, _, rest = existing.partition(GITIGNORE_BEGIN)
        _, _, suffix = rest.partition(GITIGNORE_END)
        # Strip a trailing newline from prefix and a leading newline from suffix to avoid drift.
        new_text = prefix.rstrip("\n") + ("\n\n" if prefix.strip() else "") + block + suffix.lstrip("\n")
        gi.write_text(new_text)
        return "updated"
    sep = "" if existing.endswith("\n") else "\n"
    gi.write_text(existing + sep + "\n" + block)
    return "updated"


def resolve_manifests(selection: Selection) -> Tuple[List[dict], List[str], List[str]]:
    """Return (files, dirs, post_install_notes) for a Selection.

    Files are deduped by `dst`: later manifests win, so a harness can
    override a depth-baseline file by referencing the same dst.
    """
    files: List[dict] = []
    dirs: List[str] = []
    notes: List[str] = []

    depth_manifest = load_depth_manifest(selection.depth)
    files.extend(depth_manifest.get("files", []))
    dirs.extend(depth_manifest.get("dirs", []))
    notes.extend(depth_manifest.get("post_install_notes", []))

    for harness_id in selection.harnesses:
        m = load_harness_manifest(harness_id)
        files.extend(m.get("files", []))
        dirs.extend(m.get("dirs", []))
        notes.extend(m.get("post_install_notes", []))

    for include_id in selection.includes:
        m = load_include_manifest(include_id)
        files.extend(m.get("files", []))
        dirs.extend(m.get("dirs", []))
        notes.extend(m.get("post_install_notes", []))

    # Dedupe files by dst (last-wins).
    seen: dict[str, dict] = {}
    for entry in files:
        seen[entry["dst"]] = entry
    deduped_files = list(seen.values())
    deduped_dirs = sorted(set(dirs))

    return deduped_files, deduped_dirs, notes


def install_selection(
    target: Path,
    selection: Selection,
    force: bool = False,
    dry_run: bool = False,
    allow_home: bool = False,
) -> int:
    """Install a Selection into `target`. Returns process exit code."""
    selection.validate()
    target = target.expanduser().resolve()

    if target == Path.home() and not allow_home:
        print(
            f"error: refusing to install directly into $HOME ({target}).",
            file=sys.stderr,
        )
        return 5

    files, dirs, notes = resolve_manifests(selection)

    if dry_run:
        print(f"[dry-run] target: {target}")
        print(f"[dry-run] depth: {selection.depth}")
        print(f"[dry-run] harnesses: {','.join(selection.harnesses) or '(none)'}")
        print(f"[dry-run] owner: {selection.owner}")
        print(f"[dry-run] includes: {','.join(selection.includes) or '(none)'}")
        print(f"[dry-run] would create {len(dirs)} dir(s) and {len(files)} file(s)")
        for d in dirs:
            print(f"  dir   {target / d}")
        for entry in files:
            print(f"  file  {target / entry['dst']}")
        return 0

    target.mkdir(parents=True, exist_ok=True)

    if not force:
        conflicts = [target / f["dst"] for f in files if (target / f["dst"]).exists()]
        if conflicts:
            print("error: refusing to overwrite existing files (use --force):", file=sys.stderr)
            for c in conflicts:
                print(f"  {c}", file=sys.stderr)
            return 3

    for d in dirs:
        (target / d).mkdir(parents=True, exist_ok=True)

    owner_label = harness_memory_owner(selection.owner, selection.owner)
    context = {
        "memory_owner": selection.owner,
        "memory_owner_name": owner_label,
        "harness": selection.owner,
    }

    root = template_root()
    for entry in files:
        src = root / entry["src"]
        dst = target / entry["dst"]
        if not src.is_file():
            print(f"error: template missing: {src}", file=sys.stderr)
            return 4
        dst.parent.mkdir(parents=True, exist_ok=True)
        if is_text(entry["src"]):
            dst.write_text(render(src.read_text(), context))
        else:
            shutil.copyfile(src, dst)
        mode_str = entry.get("mode")
        if mode_str:
            os.chmod(dst, int(mode_str, 8))

    # Persist config.json.
    write_config(target, Config(version=1, selection=selection))

    result = apply_gitignore(target, selection)
    print(f"solo-mise: gitignore {result}")

    # Post-install output.
    print(f"solo-mise: installed depth={selection.depth} harnesses={','.join(selection.harnesses) or '(none)'} -> {target}")
    print(f"solo-mise: memory owner -> {owner_label}")
    if "hermes" in selection.harnesses:
        print(
            "solo-mise: NOTE - the hermes adapter is experimental. "
            "Validate against your real Hermes install before relying on it. "
            "See CONTRIBUTING.md for graduation criteria.",
            file=sys.stderr,
        )
    if notes:
        print()
        print("Next steps:")
        for note in notes:
            print(f"  - {note}")
    return 0
