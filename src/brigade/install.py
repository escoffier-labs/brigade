"""install_selection - the new install engine.

Composes a depth manifest + N harness manifests + M include manifests
into a single deduped file/dir list, then copies+renders into target.
Persists the Selection to .brigade/config.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import dataclasses
import stat
import sys
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import List, Tuple

from . import shipped_history
from .config import Config, load_config, write_config
from .skill_wiring import BLOCKED, SYMLINKED, SkillWirer
from .selection import Selection, SurfaceInstallRefusal, SurfaceRecord, WRITER_INBOXES
from .templates import (
    canonical_template_bytes,
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
SURFACE_EVIDENCE_REL_PATH = ".brigade/surface-evidence.json"


# Antigravity projections lived under .antigravity/ until agy's documented
# workspace roots (.agents/skills/ and friends) were adopted (#2810).
_LEGACY_ANTIGRAVITY_PROJECTION_DIRS = ("commands", "superpowers", "skills")


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_plain(path: Path, *, directory: bool) -> bool:
    """True for a real directory or regular file: not a symlink and not a Windows junction or other reparse point."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)


def _plain_tree(base: Path) -> tuple[list[Path], list[Path]]:
    """Regular files and real directories under ``base``, never descending through a link or junction."""
    files: list[Path] = []
    dirs: list[Path] = []
    pending = [base]
    while pending:
        current = pending.pop()
        dirs.append(current)
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if _is_plain(child, directory=True):
                pending.append(child)
            elif _is_plain(child, directory=False):
                files.append(child)
    return sorted(files), dirs


def _brigade_owned_legacy_skill(rel: str, skill_id: str, data: bytes, recorded: Mapping[str, object]) -> bool:
    """A legacy SKILL.md is Brigade's only while it is a text Brigade wrote or shipped, unedited."""
    digest = shipped_history.skill_digest(data)
    record = recorded.get(rel)
    recorded_sha = record.get("sha256") if isinstance(record, dict) else None
    if recorded_sha in (hashlib.sha256(data).hexdigest(), digest):
        return True
    return digest in shipped_history.digests(shipped_history.skill_key(skill_id))


def _managed_projection_metadata(path: Path) -> dict[str, object] | None:
    """Projection marker metadata when ``path`` is a Brigade-managed tool projection, else None."""
    from .tools_cmd.projections import _read_projection

    return _read_projection(path)[0]


def _brigade_owned_legacy_projection(path: Path) -> bool:
    """A legacy tool projection is Brigade's only while its body still matches the recorded fingerprint."""
    from .tools_cmd import helpers as tools_helpers
    from .tools_cmd.projections import _read_projection

    metadata, body = _read_projection(path)
    if metadata is None:
        return False
    recorded = str(metadata.get("projection_fingerprint") or "")
    return bool(recorded) and recorded == tools_helpers._text_hash(body or "")


def _remove_emptied_parents(path: Path, base: Path) -> None:
    """rmdir ``path.parent`` and upward through ``base``, stopping at the first directory that is not empty."""
    current = path.parent
    while True:
        try:
            current.rmdir()
        except OSError:
            return
        if current == base:
            return
        current = current.parent


def prune_legacy_antigravity_projections(
    target: Path,
    recorded: Mapping[str, object] | None = None,
    wired_skills: Collection[tuple[str, str]] | None = None,
) -> list[str]:
    """Remove Brigade-owned Antigravity projections from the pre-#2810 .antigravity/ paths.

    Owned means a managed tool projection whose body still matches its
    recorded fingerprint, or a SKILL.md that is still byte-identical
    (newline-folded) to a text Brigade wrote or shipped. Anything an operator
    edited, any symlink or junction (and nothing below one), and any other file
    stays. `.antigravity/memory-handoffs` is never touched.

    A legacy file goes only after its replacement exists: a skill needs
    ``("antigravity", <id>)`` in ``wired_skills`` (this run placed
    `.agents/skills/<id>/SKILL.md`), and a tool projection needs the same-named
    managed projection under `.agents/`. Only directories emptied by a removal
    are removed. Returns the removed paths.
    """
    recorded = recorded or {}
    wired_skills = wired_skills or ()
    root = target / ".antigravity"
    removed: list[str] = []
    if not _is_plain(root, directory=True):
        return removed
    for name in _LEGACY_ANTIGRAVITY_PROJECTION_DIRS:
        base = root / name
        if not _is_plain(base, directory=True):
            continue
        files, _dirs = _plain_tree(base)
        for path in files:
            rel = path.relative_to(target).as_posix()
            if name == "skills":
                parts = path.relative_to(base).parts
                if len(parts) != 2 or parts[1] != "SKILL.md":
                    continue
                if ("antigravity", parts[0]) not in wired_skills:
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                owned = _brigade_owned_legacy_skill(rel, parts[0], data, recorded)
            else:
                replacement = target / ".agents" / path.relative_to(root)
                owned = (
                    _is_plain(replacement, directory=False)
                    and _managed_projection_metadata(replacement) is not None
                    and _brigade_owned_legacy_projection(path)
                )
            if not owned:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(rel)
            _remove_emptied_parents(path, base)
    for rel in removed:
        print(f"brigade: removed legacy Antigravity projection: {rel}")
    return removed


def is_skill_md(dst: str) -> bool:
    """True for a `<dir>/SKILL.md` template destination (never overwritten once installed)."""
    parts = Path(dst).parts
    return len(parts) >= 2 and parts[-1] == "SKILL.md"


def _writer_inbox_gitignore_lines(inbox: str) -> list[str]:
    """Gitignore lines for a writer inbox, including parent-directory un-ignores.

    A parent rule such as `.claude/` prevents descendant `!.../TEMPLATE.md`
    negations from being traversed; un-ignore the harness root and inbox parent
    before the managed template exception.
    """
    parts = Path(inbox).parts
    if len(parts) < 2:
        return [
            f"{inbox}/*",
            f"!{inbox}/TEMPLATE.md",
            f"!{inbox}/.gitkeep",
        ]
    harness_root = parts[0]
    return [
        f"!{harness_root}/",
        f"{harness_root}/*",
        f"!{inbox}/",
        f"{inbox}/*",
        f"!{inbox}/TEMPLATE.md",
        f"!{inbox}/.gitkeep",
    ]


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
            lines.append(f"# {h}: handoffs are session-local and may contain private context.")
            lines.extend(_writer_inbox_gitignore_lines(inbox))
            lines.append("")
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
            "!.brigade/",
            ".brigade/*",
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
            ".brigade/campaigns/",
            ".brigade/chat-surfaces.toml",
            ".brigade/daily.toml",
            ".brigade/memory-care.toml",
            ".brigade/node.toml",
            ".brigade/vault.toml",
            ".brigade/vault-index/",
            ".brigade/vault-propose/",
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
            "# The code graph index is derived from source and rebuilt by `brigade code sync`.",
            ".brigade/code-graph/",
            "# .brigade/mcp.json is the shared canonical MCP server catalog: keep it tracked.",
            "!.brigade/mcp.json",
            ".brigade/mcp/",
            "",
            "# Generated tool projections are local harness state.",
            ".claude/commands/",
            ".codex/skills/",
            ".opencode/commands/",
            ".opencode/superpowers/",
            ".agents/commands/",
            ".agents/superpowers/",
            "# Legacy Antigravity projection paths (pre-#2810): can go after the next release.",
            ".antigravity/commands/",
            ".antigravity/superpowers/",
            # Not .agents/skills/: other harnesses read it and teams commit
            # skills there, so ignoring it would hide them (#2810 review).
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
            content = git_path.read_text(encoding="utf-8").strip()
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
        gi.write_text(block, encoding="utf-8")
        return f"created ({location})"
    existing = gi.read_text(encoding="utf-8")
    markers = (
        (GITIGNORE_BEGIN, GITIGNORE_END),
        (LEGACY_GITIGNORE_BEGIN, LEGACY_GITIGNORE_END),
    )
    new_text, replaced = _replace_managed_gitignore_blocks(existing, block, markers)
    if replaced:
        gi.write_text(new_text, encoding="utf-8")
        return f"updated ({location})"
    sep = "" if existing.endswith("\n") else "\n"
    gi.write_text(existing + sep + "\n" + block, encoding="utf-8")
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


def build_render_context(selection: Selection) -> dict[str, str]:
    """Build the placeholder context used when rendering text templates."""
    owner_label = harness_memory_owner(selection.owner, selection.owner)
    writer_inboxes = [WRITER_INBOXES[h] for h in selection.harnesses if h in WRITER_INBOXES]
    owner_inbox = WRITER_INBOXES.get(selection.owner)
    if owner_inbox:
        writer_inboxes = [owner_inbox] + [p for p in writer_inboxes if p != owner_inbox]
    if not writer_inboxes:
        writer_inboxes = [WRITER_INBOXES["claude"]]
    return {
        "memory_owner": selection.owner,
        "memory_owner_name": owner_label,
        "harness": selection.owner,
        "handoff_inbox": f"{writer_inboxes[0]}/",
        "handoff_inboxes": ", ".join(f"`{p}/`" for p in writer_inboxes),
    }


def resolve_manifests(
    selection: Selection, *, templates_root: Path | None = None
) -> Tuple[List[dict], List[str], List[str]]:
    """Return (files, dirs, post_install_notes) for a Selection.

    Files are deduped by `dst`: later manifests win, so a harness can
    override a depth-baseline file by referencing the same dst.
    """
    files: List[dict] = []
    dirs: List[str] = []
    notes: List[str] = []

    depth_manifest = load_depth_manifest(selection.depth, templates_root=templates_root)
    files.extend(depth_manifest.get("files", []))
    dirs.extend(depth_manifest.get("dirs", []))
    notes.extend(depth_manifest.get("post_install_notes", []))

    for harness_id in selection.harnesses:
        m = load_harness_manifest(harness_id, templates_root=templates_root)
        files.extend(m.get("files", []))
        dirs.extend(m.get("dirs", []))
        notes.extend(m.get("post_install_notes", []))

    for include_id in selection.includes:
        m = load_include_manifest(include_id, templates_root=templates_root)
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


def ensure_surface_installable(surface: SurfaceRecord, *, projection_only: bool) -> None:
    """Refuse runtime-absent surfaces unless projection-only mode is explicit."""
    state = surface.availability.get("state")
    reason = surface.availability.get("reason")
    if state == "externally_blocked" and reason == "binary_not_found" and not projection_only:
        raise SurfaceInstallRefusal(surface.surface_id, "availability is externally_blocked: binary_not_found")
    if state == "external_only" and not projection_only:
        raise SurfaceInstallRefusal(surface.surface_id, "external-only surfaces require --projection-only")


def _preflight_surfaces(selection: Selection, *, projection_only: bool) -> None:
    for surface in selection.surfaces:
        ensure_surface_installable(surface, projection_only=projection_only)


def _write_surface_evidence(target: Path, selection: Selection, *, projection_only: bool) -> None:
    if not selection.surfaces:
        return
    path = target / SURFACE_EVIDENCE_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "surfaces": {
            surface.surface_id: surface.persisted_evidence(projection_only=projection_only)
            for surface in selection.surfaces
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _merged_config(target: Path, selection: Selection, *, work_mode: str | None = None) -> Config:
    try:
        existing = load_config(target)
    except (ValueError, AttributeError, TypeError) as exc:
        # An unreadable config is replaced, as a first install would.
        print(f"warning: replacing unreadable .brigade/config.json ({exc})", file=sys.stderr)
        existing = None
    merged = Config(version=1, selection=selection) if existing is None else existing
    merged = dataclasses.replace(merged, version=1, selection=selection)
    # None preserves the recorded mode; explicit flags select canonical values.
    return merged if work_mode is None else dataclasses.replace(merged, work_mode=work_mode)


def install_selection(
    target: Path,
    selection: Selection,
    force: bool = False,
    dry_run: bool = False,
    allow_home: bool = False,
    use_git_exclude: bool = False,
    update_gitignore: bool = True,
    wire_skills: bool = True,
    projection_only: bool = False,
    work_mode: str | None = None,
) -> int:
    """Install a Selection into `target`. Returns process exit code."""
    selection.validate()
    target = target.expanduser().resolve()
    _preflight_surfaces(selection, projection_only=projection_only)

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
                print(f"  file  {dst} (exists; kept)")
            else:
                print(f"  file  {dst}")
        return 0

    new_workspace = not any(
        (target / name).exists() or (target / name).is_symlink() for name in (".brigade", ".solo-mise")
    )
    target.mkdir(parents=True, exist_ok=True)

    for d in dirs:
        (target / d).mkdir(parents=True, exist_ok=True)

    context = build_render_context(selection)

    root = template_root()
    kept_files: list[Path] = []
    # A SKILL.md is never overwritten, `force` included (#1844): defer these to the
    # never-overwrite wiring pass below instead of the force-aware copy.
    skill_md_entries: list[tuple[tuple[str, ...], Path, str]] = []
    for entry in files:
        src = root / entry["src"]
        dst = target / entry["dst"]
        if not src.is_file():
            print(f"error: template missing: {src}", file=sys.stderr)
            return 4
        if is_skill_md(entry["dst"]):
            rel_parts = tuple(Path(entry["dst"]).parts)
            skill_md_entries.append((rel_parts, src, render(src.read_text(encoding="utf-8"), context)))
            continue
        if dst.exists() and not force:
            # Never clobber an existing file without --force. Keep it; the
            # additive brigade-work skill wiring below still runs, so an upgrade
            # or a brownfield repo still gets wired without losing local edits.
            kept_files.append(dst)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if is_text(entry["src"]):
            dst.write_text(render(src.read_text(encoding="utf-8"), context), encoding="utf-8", newline="\n")
        else:
            dst.write_bytes(canonical_template_bytes(src))
        mode_str = entry.get("mode")
        if mode_str:
            os.chmod(dst, int(mode_str, 8))

    # Persist config.json. Keep settings an operator already recorded (verify
    # retention, run-lock wait, recall target, ...); only the selection changes.
    write_config(target, _merged_config(target, selection, work_mode=work_mode))
    if new_workspace and not projection_only:
        from .memory_reflect_policy import initialize

        initialize(target)
    _write_surface_evidence(target, selection, projection_only=projection_only)

    # Wire Brigade's built-in skills into each harness's skills directory so
    # agents actually USE Brigade and can scout large work before editing.
    # Skipped with --no-wire.
    wired_skills: list[tuple[str, str]] = []
    wirer = SkillWirer(target)
    # finish() always runs, so the records of files already written survive a failure partway through.
    try:
        for rel_parts, src, text in skill_md_entries:
            wirer.place(rel_parts, src.parent, text, skill_id=rel_parts[-2], harness="workspace")
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
                    status = wirer.place(
                        (*Path(rel).parts, "SKILL.md"),
                        skill_src.parent,
                        skill_src.read_text(encoding="utf-8"),
                        skill_id=skill_id,
                        harness=h,
                    )
                    if status not in (SYMLINKED, BLOCKED):
                        wired_skills.append((h, skill_id))
    finally:
        wirer.finish()

    # New projections are in place; retire the Brigade-owned ones at the old path.
    # --no-wire writes no replacements, so it removes nothing either.
    if wire_skills and "antigravity" in selection.harnesses:
        try:
            prune_legacy_antigravity_projections(target, wirer.recorded, set(wired_skills))
        except OSError as exc:
            print(f"warning: unable to prune legacy .antigravity projections: {exc}", file=sys.stderr)
        # A catalog written before #2810 still maps antigravity to the old paths.
        try:
            from .tools_cmd.catalog_manage import migrate_legacy_antigravity_paths

            for tool_id in migrate_legacy_antigravity_paths(target):
                print(f"brigade: moved tools catalog antigravity projection for {tool_id} to .agents/")
        except OSError as exc:
            print(f"warning: unable to migrate legacy antigravity paths in the tools catalog: {exc}", file=sys.stderr)

    # Claude Code project hooks enforce the same work loop described by the
    # managed skill. The merge helper owns only Brigade handlers and preserves
    # every unrelated setting and hook already present in the repo.
    if wire_skills and "claude" in selection.harnesses:
        from .claude_hooks.install_cmd import hooks_install

        if hooks_install(target=target, quiet=True) != 0:
            print(
                "warning: unable to wire project-scoped Claude work-loop hooks; "
                f"run `brigade work hooks install --target {target}` after checking .claude/settings.json",
                file=sys.stderr,
            )

    if wire_skills and "antigravity" in selection.harnesses:
        from .antigravity_presence import install_hooks

        try:
            install_hooks(target)
        except (OSError, ValueError) as exc:
            print(f"warning: unable to wire Antigravity presence hooks: {exc}", file=sys.stderr)

    if wire_skills and "cursor" in selection.harnesses:
        from .cursor_user_cmd import install_project_presence_hooks

        error = install_project_presence_hooks(target)
        if error:
            print(
                f"warning: unable to wire project-scoped Cursor presence hooks; check .cursor/hooks.json ({error})",
                file=sys.stderr,
            )

    if update_gitignore:
        result = apply_gitignore(target, selection, use_git_exclude=use_git_exclude)
        print(f"brigade: gitignore {result}")
    else:
        print("brigade: gitignore skipped (--no-gitignore)")

    # Post-install output.
    print(
        f"brigade: installed depth={selection.depth} harnesses={','.join(selection.harnesses) or '(none)'} -> {target}"
    )
    print(f"brigade: memory owner -> {context['memory_owner_name']}")
    if kept_files:
        print(
            f"brigade: kept {len(kept_files)} existing file(s); re-run init only if you intend "
            "to replace them with current templates "
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
        from .work_mode import resolve_mode

        mode = resolve_mode(target)
        if mode in {"off", "observe"}:
            print(f"Agent: {mode} work mode - verification is not enforced here -")
            print("  1. `brigade work brief --target .`                                  (see pending work)")
            print("  2. run checks directly; `brigade work verify run` still records a receipt when you want one")
            print(
                "  3. write a Memory Handoff only if the session learned something durable, else end with `Nothing durable: <reason>`."
            )
            print("  `brigade work mode set strict` restores the verification loop.")
        else:
            print("Agent: each work session, follow the `brigade-work` skill -")
            print("  1. `brigade work brief --target .`                                  (see pending work)")
            print(
                '  2. `brigade work verify run --target . --command "<test>" --capture auto`  (verify THROUGH brigade, not raw)'
            )
            print(
                "  3. `--capture auto` records the result. A typed id still works after the fact with `brigade outcome capture`"
            )
            print(
                "  4. write a Memory Handoff only if the session learned something durable, else end with `Nothing durable: <reason>`."
            )
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
