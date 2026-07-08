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
DEFAULT_WIRED_SKILLS = ("brigade-work", "ultra-work-scout")


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
            ".brigade/receipt-signing-key",
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
            ".brigade/mcp/",
            "# .brigade/mcp.json is the shared canonical MCP server catalog: keep it tracked.",
            "!.brigade/mcp.json",
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


def _git_info_dir(target: Path) -> Path | None:
    """Resolve the git `info/` dir that holds `exclude`, or None when there is no git dir.

    Handles both a normal `.git` directory and a linked-worktree `.git` file
    ("gitdir: <path>"), whose exclude lives under the worktree's own git dir.
    """
    git_path = target / ".git"
    if git_path.is_dir():
        return git_path / "info"
    if git_path.is_file():
        try:
            content = git_path.read_text().strip()
        except OSError:
            return None
        if content.startswith("gitdir:"):
            gitdir = Path(content.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (target / gitdir).resolve()
            if gitdir.is_dir():
                return gitdir / "info"
    return None


def apply_gitignore(target: Path, selection: Selection, *, use_git_exclude: bool = False) -> str:
    """Insert or replace the managed ignore block. Returns a 'created/updated (<file>)' label.

    By default the block goes in the tracked `.gitignore`. With use_git_exclude
    (for a third-party clone you do not want to commit Brigade ignores into) it
    goes in the untracked `.git/info/exclude` instead, falling back to `.gitignore`
    when there is no `.git` directory to hold it.
    """
    block = build_gitignore_block(selection)
    exclude_dir = _git_info_dir(target) if use_git_exclude else None
    if exclude_dir is not None:
        gi = exclude_dir / "exclude"
        gi.parent.mkdir(parents=True, exist_ok=True)
        location = "git info/exclude"
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

    # A file entry may carry an optional "depth" key to select a
    # depth-specific variant (e.g. repo/CLAUDE.md vs workspace/CLAUDE.md).
    files = [entry for entry in files if entry.get("depth") in (None, selection.depth)]

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
    update_gitignore: bool = True,
    wire_skills: bool = True,
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
                print(f"  file  {dst} (exists; kept unless --force)")
            else:
                print(f"  file  {dst}")
        return 0

    target.mkdir(parents=True, exist_ok=True)

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
    kept_files: list[Path] = []
    for entry in files:
        src = root / entry["src"]
        dst = target / entry["dst"]
        if not src.is_file():
            print(f"error: template missing: {src}", file=sys.stderr)
            return 4
        if dst.exists() and not force:
            # Never clobber an existing file without --force. Keep it; the
            # additive brigade-work skill wiring below still runs, so an upgrade
            # or a brownfield repo still gets wired without losing local edits.
            kept_files.append(dst)
            continue
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

    # Wire Brigade's built-in skills into each harness's skills directory so
    # agents actually USE Brigade and can scout large work before editing.
    # Skipped with --no-wire.
    wired_skills: list[tuple[str, str]] = []
    if wire_skills:
        from .skills_cmd import HARNESS_ADAPTERS

        for skill_id in DEFAULT_WIRED_SKILLS:
            skill_src = root / "skills" / skill_id / "SKILL.md"
            if not skill_src.is_file():
                print(f"warning: built-in skill template missing: {skill_src}", file=sys.stderr)
                continue
            for h in selection.harnesses:
                adapter = HARNESS_ADAPTERS.get(h)
                if not adapter:
                    continue
                rel = str(adapter.get("install_path", "")).format(skill_id=skill_id)
                if not rel.startswith("."):
                    # user-scoped/absolute adapters (e.g. hermes): use `brigade skills install`.
                    continue
                dst = target / rel / "SKILL.md"
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(skill_src.read_text())
                wired_skills.append((h, skill_id))

    if update_gitignore:
        result = apply_gitignore(target, selection, use_git_exclude=use_git_exclude)
        print(f"brigade: gitignore {result}")
    else:
        print("brigade: gitignore skipped (--no-gitignore)")

    # Post-install output.
    print(
        f"brigade: installed depth={selection.depth} harnesses={','.join(selection.harnesses) or '(none)'} -> {target}"
    )
    print(f"brigade: memory owner -> {owner_label}")
    if kept_files:
        print(
            f"brigade: kept {len(kept_files)} existing file(s); run with --force to overwrite "
            "(e.g. to refresh AGENTS.md / CLAUDE.md with the latest directives)."
        )
    if wired_skills:
        harnesses = sorted({h for h, _ in wired_skills})
        skills = sorted({skill for _, skill in wired_skills})
        print()
        print(
            f"brigade: wired built-in skills ({', '.join(skills)}) into {', '.join(harnesses)} "
            "so your agent USES Brigade, not just installs it."
        )
        print("Agent: each work session, follow the `brigade-work` skill -")
        print("  1. `brigade work brief --target .`                                  (see pending work)")
        print('  2. `brigade work verify run --target . --command "<test>"`          (verify THROUGH brigade, not raw)')
        print("  3. `brigade outcome capture <skill-or-card-id> --run-id latest`     (record the result)")
        print("  4. write a Memory Handoff at the end.")
        print('  If `brigade outcome rank` ever says "ranking: none", work is not flowing through Brigade.')
    if "hermes" in selection.harnesses:
        print(
            "brigade: the hermes adapter is validated against a real Hermes install "
            "(handoffs and skill install). Reviewed skills install into your Hermes store.",
            file=sys.stderr,
        )
    if notes:
        print()
        print("Next steps:")
        for note in notes:
            print(f"  - {note}")
    return 0
