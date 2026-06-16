"""install_selection - the new install engine.

Composes a depth manifest + N harness manifests + M include manifests
into a single deduped file/dir list, then copies+renders into target.
Persists the Selection to .brigade/config.json.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

from .config import Config, write_config
from .selection import Selection, WRITER_INBOXES
from .templates import (
    harness_memory_owner,
    is_text,
    load_depth_manifest,
    load_harness_manifest,
    load_include_manifest,
    render,
    template_root,
)

GITIGNORE_BEGIN = "# >>> brigade gitignore block >>>"
GITIGNORE_END = "# <<< brigade gitignore block <<<"
LEGACY_GITIGNORE_BEGIN = "# >>> solo-mise gitignore block >>>"
LEGACY_GITIGNORE_END = "# <<< solo-mise gitignore block <<<"


def build_gitignore_block(selection: Selection) -> str:
    lines = [
        GITIGNORE_BEGIN,
        "# Managed by `brigade init`. Edit between the markers to customize.",
        "# Re-running `brigade init` replaces only the content between markers.",
        "",
    ]
    for h in selection.harnesses:
        inbox = WRITER_INBOXES.get(h)
        if inbox:
            lines.extend(
                [
                    f"# {h}: handoffs are session-local and may contain private context.",
                    f"{inbox}/*",
                    f"!{inbox}/TEMPLATE.md",
                    f"!{inbox}/.gitkeep",
                    "",
                ]
            )
    lines.extend(
        [
            "# Daily session logs are machine-local raw context.",
            "memory/20[0-9][0-9]-[0-1][0-9]-[0-3][0-9].md",
            "",
            "# Review inbox: ambiguous handoffs awaiting human triage.",
            "memory/handoff-inbox/",
            "",
            "# brigade local state (logs, scrub cache, dogfood runs, work sessions).",
            ".brigade/",
            ".brigade/backups/",
            ".brigade/backups.toml",
            ".brigade/center/",
            ".brigade/context/",
            ".brigade/dogfood.toml",
            ".brigade/handoffs/",
            ".brigade/handoff-sources.json",
            ".brigade/learn/",
            ".brigade/projects.toml",
            ".brigade/release/",
            ".brigade/repos.toml",
            ".brigade/chat-surfaces.toml",
            ".brigade/daily.toml",
            ".brigade/memory-care.toml",
            ".brigade/reviews.toml",
            ".brigade/scanners.toml",
            ".brigade/security.toml",
            ".brigade/tools.toml",
            ".brigade/logs/",
            ".brigade/runs/",
            ".brigade/scrub-cache/",
            ".brigade/scanners/",
            ".brigade/security/",
            ".brigade/tools/",
            ".brigade/chat-memory-sweeps/",
            ".brigade/work/",
            "",
            "# Generated tool projections are local harness state.",
            ".claude/commands/",
            ".codex/skills/",
            ".opencode/commands/",
            ".opencode/superpowers/",
            ".antigravity/commands/",
            ".antigravity/superpowers/",
            ".pi/commands/",
            ".pi/superpowers/",
            ".cursor/rules/",
            ".cursor/skills/",
            ".aider/commands/",
            ".aider/skills/",
            ".goose/commands/",
            ".goose/skills/",
            ".continue/rules/",
            ".continue/skills/",
            ".copilot/instructions/",
            ".copilot/skills/",
            ".qwen/commands/",
            ".qwen/skills/",
            ".kimi/commands/",
            ".kimi/skills/",
            ".adal/commands/",
            ".adal/skills/",
            ".openhands/instructions/",
            ".openhands/skills/",
            ".grok/instructions/",
            ".grok/skills/",
            ".amp/instructions/",
            ".amp/skills/",
            ".crush/instructions/",
            ".crush/skills/",
            ".hermes/commands/",
            ".hermes/superpowers/",
            ".openclaw/commands/",
            ".openclaw/superpowers/",
            ".mcp/",
            "scripts/*.md",
            GITIGNORE_END,
            "",
        ]
    )
    return "\n".join(lines)


def apply_gitignore(target: Path, selection: Selection, *, use_git_exclude: bool = False) -> str:
    """Insert or replace the managed ignore block. Returns a 'created/updated (<file>)' label.

    By default the block goes in the tracked `.gitignore`. With use_git_exclude
    (for a third-party clone you do not want to commit Brigade ignores into) it
    goes in the untracked `.git/info/exclude` instead, falling back to `.gitignore`
    when there is no `.git` directory to hold it.
    """
    block = build_gitignore_block(selection)
    if use_git_exclude and (target / ".git").is_dir():
        gi = target / ".git" / "info" / "exclude"
        gi.parent.mkdir(parents=True, exist_ok=True)
        location = ".git/info/exclude"
    else:
        gi = target / ".gitignore"
        location = ".gitignore"
    if not gi.exists():
        gi.write_text(block)
        return f"created ({location})"
    existing = gi.read_text()
    markers = (
        (GITIGNORE_BEGIN, GITIGNORE_END),
        (LEGACY_GITIGNORE_BEGIN, LEGACY_GITIGNORE_END),
    )
    new_text, replaced = _replace_managed_gitignore_blocks(existing, block, markers)
    if replaced:
        gi.write_text(new_text)
        return f"updated ({location})"
    sep = "" if existing.endswith("\n") else "\n"
    gi.write_text(existing + sep + "\n" + block)
    return f"updated ({location})"


def _replace_managed_gitignore_blocks(
    existing: str,
    block: str,
    markers: tuple[tuple[str, str], ...],
) -> tuple[str, bool]:
    """Replace all complete known managed blocks with one regenerated block."""
    output: list[str] = []
    cursor = 0
    inserted = False
    replaced = False
    while True:
        next_block: tuple[int, int] | None = None
        for begin, end in markers:
            start = existing.find(begin, cursor)
            if start == -1:
                continue
            end_start = existing.find(end, start + len(begin))
            if end_start == -1:
                continue
            stop = end_start + len(end)
            if next_block is None or start < next_block[0]:
                next_block = (start, stop)
        if next_block is None:
            break
        start, stop = next_block
        output.append(existing[cursor:start])
        if not inserted:
            output.append(block)
            inserted = True
        cursor = stop
        replaced = True
    if not replaced:
        return existing, False
    output.append(existing[cursor:])
    return "".join(output), True


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
    use_git_exclude: bool = False,
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
            dst = target / entry["dst"]
            if dst.exists():
                print(f"  file  {dst} (exists; refused without --force)")
            else:
                print(f"  file  {dst}")
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
    writer_inboxes = [WRITER_INBOXES[h] for h in selection.harnesses if h in WRITER_INBOXES]
    owner_inbox = WRITER_INBOXES.get(selection.owner)
    if owner_inbox:
        writer_inboxes = [owner_inbox] + [p for p in writer_inboxes if p != owner_inbox]
    if not writer_inboxes:
        writer_inboxes = [WRITER_INBOXES["claude"]]
    context = {
        "memory_owner": selection.owner,
        "memory_owner_name": owner_label,
        "harness": selection.owner,
        "handoff_inbox": f"{writer_inboxes[0]}/",
        "handoff_inboxes": ", ".join(f"`{p}/`" for p in writer_inboxes),
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

    result = apply_gitignore(target, selection, use_git_exclude=use_git_exclude)
    print(f"brigade: gitignore {result}")

    # Post-install output.
    print(
        f"brigade: installed depth={selection.depth} harnesses={','.join(selection.harnesses) or '(none)'} -> {target}"
    )
    print(f"brigade: memory owner -> {owner_label}")
    if "hermes" in selection.harnesses:
        print(
            "brigade: NOTE - the hermes adapter is experimental. "
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
