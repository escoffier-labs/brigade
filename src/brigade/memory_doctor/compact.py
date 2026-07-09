"""Compact verb: flatten multi-line MEMORY.md entries into topic files."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from brigade.memory_doctor.paths import PathConfig
from brigade.memory_doctor.safety import (
    UnsafeTargetError,
    atomic_write_text,
    resolve_card_target,
)


BULLET_RE = re.compile(r"^- \[([^\]]+)\]\(([^)]+)\)\s*(.*)$")
INDENTED_CONTINUATION_RE = re.compile(r"^\s{2,}\S")

# Unicode punctuation that violates the no-em-dash rule (and other ASCII-only
# conventions) mapped to safe replacements. Applied to every line we rewrite
# plus a final whole-file pass on apply. We deliberately do NOT touch the link
# TARGET (the URL inside the parentheses); only the visible text is normalized.
UNICODE_NORMALIZATION = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "―": "-",  # horizontal bar
    "→": "->",  # right arrow
    "≥": ">=",  # greater-than-or-equal
    "≤": "<=",  # less-than-or-equal
    "≈": "~",  # almost equal
    "·": "-",  # middle dot
}


def _normalize_unicode(text: str) -> str:
    """Replace em/en dashes and a few other unicode glyphs with ASCII."""
    for src, dst in UNICODE_NORMALIZATION.items():
        text = text.replace(src, dst)
    return text


def _has_normalizable(text: str) -> bool:
    """True if `text` contains any glyph the normalizer would rewrite."""
    return any(src in text for src in UNICODE_NORMALIZATION)


def _flatten_marker(today: str, flatten: "Flatten") -> str:
    """Stable marker so re-applying the same flatten plan is idempotent.

    Hashes the FULL payload (target + title + bullet hook + detail lines) so
    two entries with the same title but different detail don't collide and
    silently swallow each other's content.
    """
    payload = "|".join(
        [
            flatten.target_name,
            flatten.title,
            flatten.bullet_text.strip(),
            "\n".join(flatten.detail_lines),
        ]
    )
    h = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"<!-- compact:{today}:{h} -->"


def _tighten_marker(today: str, tighten: "Tighten") -> str:
    """Stable marker so re-applying the same tighten plan is idempotent.

    Hashes the FULL original hook + target + title so two entries that share a
    title but differ in hook text don't collide. Uses a distinct `tighten:`
    prefix so it never aliases a flatten marker.
    """
    payload = "|".join(
        [
            tighten.target_name,
            tighten.title,
            tighten.full_hook.strip(),
        ]
    )
    h = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"<!-- compact:tighten:{today}:{h} -->"


@dataclass(frozen=True)
class Flatten:
    line_index: int  # index of the bullet line in MEMORY.md
    title: str
    target_name: str  # filename, e.g. 'topic-b.md'
    bullet_text: str  # the bullet's original hook text (kept in the index)
    detail_lines: list[str]  # the continuation lines (flattened into the topic file)


@dataclass(frozen=True)
class Tighten:
    line_index: int  # index of the bullet line in MEMORY.md
    title: str
    target_name: str  # filename, e.g. 'topic-b.md'
    full_hook: str  # the entry's original (overlong) hook text
    short_hook: str  # the truncated hook that replaces it in the index


@dataclass(frozen=True)
class CompactionPlan:
    original_lines: int
    flattens: list[Flatten]
    missing_targets: list[str]
    projected_lines: int
    tightens: list[Tighten] = field(default_factory=list)
    projected_bytes: int = 0
    original_bytes: int = 0
    unsafe_targets: list[str] = field(default_factory=list)


def _truncate_hook(hook: str, max_hook_chars: int) -> str:
    """Truncate `hook` to <= max_hook_chars at a word boundary, append '...'.

    The ellipsis itself counts toward the budget so the rewritten visible text
    never exceeds the limit. If even the first word overflows, hard-truncate.
    """
    hook = hook.strip()
    if len(hook) <= max_hook_chars:
        return hook
    budget = max(0, max_hook_chars - 3)  # room for the trailing '...'
    cut = hook[:budget]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + "..."


def plan_compaction(
    memory_dir: Path,
    max_lines: int,
    *,
    max_hook_chars: int = 140,
) -> CompactionPlan:
    index_path = memory_dir / "MEMORY.md"
    raw = index_path.read_bytes() if index_path.exists() else b""
    lines = raw.decode("utf-8", errors="replace").splitlines() if raw else []
    flattens: list[Flatten] = []
    tightens: list[Tighten] = []
    missing: list[str] = []
    unsafe: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = BULLET_RE.match(line)
        if not m:
            i += 1
            continue
        title = m.group(1)
        target_url = m.group(2)
        bullet_text = m.group(3)
        target_name = target_url.removeprefix("cards/")
        details: list[str] = []
        j = i + 1
        while j < len(lines) and INDENTED_CONTINUATION_RE.match(lines[j]):
            details.append(lines[j].strip())
            j += 1
        if details:
            # Reject targets that would escape memory_dir or are otherwise unsafe;
            # exclude them from the flatten plan entirely so we never write outside.
            try:
                resolve_card_target(memory_dir, target_name)
            except UnsafeTargetError:
                unsafe.append(target_name)
                i = j
                continue
            flattens.append(
                Flatten(
                    line_index=i,
                    title=title,
                    target_name=target_name,
                    bullet_text=bullet_text,
                    detail_lines=details,
                )
            )
            if not (memory_dir / target_name).exists():
                missing.append(target_name)
            i = j
            continue
        # Single-line entry: a "tighten" candidate when the hook is overlong AND
        # the linked card actually exists (a dangling link may be the only record
        # of this hook, so we never truncate those).
        hook = bullet_text.strip()
        if len(_normalize_unicode(hook)) > max_hook_chars:
            try:
                resolved = resolve_card_target(memory_dir, target_name)
            except UnsafeTargetError:
                unsafe.append(target_name)
                i = j
                continue
            if resolved.exists():
                normalized = _normalize_unicode(hook)
                tightens.append(
                    Tighten(
                        line_index=i,
                        title=title,
                        target_name=target_name,
                        full_hook=normalized,
                        short_hook=_truncate_hook(normalized, max_hook_chars),
                    )
                )
        i = j

    projected_lines = len(lines) - sum(len(f.detail_lines) for f in flattens)
    projected_bytes = _projected_index_bytes(lines, flattens, tightens, max_hook_chars)
    return CompactionPlan(
        original_lines=len(lines),
        flattens=flattens,
        missing_targets=missing,
        projected_lines=projected_lines,
        tightens=tightens,
        projected_bytes=projected_bytes,
        original_bytes=len(raw),
        unsafe_targets=unsafe,
    )


def _rewrite_index_lines(
    lines: list[str],
    flattens: list[Flatten],
    tightens: list[Tighten],
    max_hook_chars: int,
) -> list[str]:
    """Pure projection of MEMORY.md after applying flattens + tightens.

    Used for byte estimation in planning and for the actual rewrite on apply,
    so the projected byte count and the real result stay in lockstep.
    """
    skip_indexes: set[int] = set()
    for flatten in flattens:
        for off in range(1, len(flatten.detail_lines) + 1):
            skip_indexes.add(flatten.line_index + off)
    tighten_by_index = {t.line_index: t for t in tightens}
    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in skip_indexes:
            continue
        if idx in tighten_by_index:
            t = tighten_by_index[idx]
            m = BULLET_RE.match(line)
            if m:
                prefix = f"- [{m.group(1)}]({m.group(2)}) "
                out.append(_normalize_unicode(prefix) + t.short_hook)
                continue
        out.append(_normalize_unicode(line))
    return out


def _projected_index_bytes(
    lines: list[str],
    flattens: list[Flatten],
    tightens: list[Tighten],
    max_hook_chars: int,
) -> int:
    rewritten = _rewrite_index_lines(lines, flattens, tightens, max_hook_chars)
    text = "\n".join(rewritten) + ("\n" if rewritten else "")
    return len(text.encode("utf-8"))


def _apply_flatten(memory_dir: Path, plan: CompactionPlan) -> None:
    index_path = memory_dir / "MEMORY.md"
    lines = index_path.read_bytes().decode("utf-8", errors="replace").splitlines()
    today = dt.date.today().isoformat()

    applied: list[Flatten] = []
    for flatten in plan.flattens:
        # Defense-in-depth: re-validate target on apply; plan_compaction already
        # filters unsafe entries, but never trust a cached plan with raw paths.
        try:
            target_path = resolve_card_target(memory_dir, flatten.target_name)
        except UnsafeTargetError:
            continue
        existing = target_path.read_text()
        marker = _flatten_marker(today, flatten)
        if marker in existing:
            # Already applied for this title/date - skip the topic append but
            # still drop the detail lines from the index below for consistency.
            applied.append(flatten)
            continue
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        appended = (
            f"{existing}{sep}{marker}\n## From index ({today})\n\n"
            f"{flatten.bullet_text.strip()}\n\n" + "\n".join(flatten.detail_lines) + "\n"
        )
        atomic_write_text(target_path, appended)
        applied.append(flatten)

    applied_tightens: list[Tighten] = []
    for tighten in plan.tightens:
        # Mirror _apply_flatten: re-validate, append FULL hook under an
        # idempotent marker, then let the index rewrite swap in the short hook.
        try:
            target_path = resolve_card_target(memory_dir, tighten.target_name)
        except UnsafeTargetError:
            continue
        if not target_path.exists():
            # Dangling link: index may be the only record. Do not move; the
            # whole-file normalization pass below still scrubs unicode in place.
            continue
        existing = target_path.read_text()
        marker = _tighten_marker(today, tighten)
        if marker not in existing:
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            appended = f"{existing}{sep}{marker}\n## From index ({today})\n\n{tighten.full_hook.strip()}\n"
            atomic_write_text(target_path, appended)
        applied_tightens.append(tighten)

    rewritten = _rewrite_index_lines(lines, applied, applied_tightens, 0)
    atomic_write_text(index_path, "\n".join(rewritten) + ("\n" if rewritten else ""))


def run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
) -> int:
    import sys
    from brigade.memory_doctor.git import (
        commit_run,
        files_have_uncommitted_changes,
        is_git_repo,
        validate_author_format,
        working_tree_sane,
    )

    if commit and not apply:
        print("brigade memory compact: skipping commit (dry-run; use --apply)")

    index_path = cfg.memory_dir / "MEMORY.md"
    if not index_path.exists():
        print(f"brigade memory compact: {index_path} does not exist")
        return 0

    plan = plan_compaction(cfg.memory_dir, cfg.max_lines, max_hook_chars=cfg.max_hook_chars)

    index_text = index_path.read_bytes().decode("utf-8", errors="replace")
    has_unicode = _has_normalizable(index_text)
    over_lines = plan.original_lines > cfg.max_lines
    over_bytes = plan.original_bytes > cfg.max_bytes
    has_work = bool(plan.flattens) or bool(plan.tightens) or has_unicode

    # Nothing to do only when genuinely nothing: under both thresholds AND no
    # flatten/tighten candidates AND no unicode to scrub.
    if not over_lines and not over_bytes and not has_work:
        print(
            f"brigade memory compact: {plan.original_lines} lines <= {cfg.max_lines}, "
            f"{plan.original_bytes} bytes <= {cfg.max_bytes}, no action needed"
        )
        return 0

    mode = "APPLY" if apply else "dry-run"
    print(
        f"brigade memory compact ({mode}): MEMORY.md "
        f"{plan.original_lines} -> ~{plan.projected_lines} lines, "
        f"{plan.original_bytes} -> ~{plan.projected_bytes} bytes"
    )

    if plan.unsafe_targets:
        print("\nWARNING: skipping entries with unsafe targets (path traversal / escapes memory dir):")
        for t in plan.unsafe_targets:
            print(f"  - {t}")

    if plan.missing_targets:
        print("\nERROR: target topic files missing for some flatten candidates:")
        for t in plan.missing_targets:
            print(f"  - {t}")
        print("\nRefusing to compact: would orphan content. Create the missing card(s) first.")
        return 2

    if plan.flattens:
        print("\nFlatten candidates:")
        for f in plan.flattens:
            print(f"  [{f.title}] -> {f.target_name} (+{len(f.detail_lines)} line(s))")

    if plan.tightens:
        print("\nTighten candidates:")
        for tighten in plan.tightens:
            print(
                f"  [{tighten.title}] -> {tighten.target_name} "
                f"({len(tighten.full_hook)} -> {len(tighten.short_hook)} chars)"
            )

    if not plan.flattens and not plan.tightens:
        if has_unicode:
            print("\nNo flatten/tighten candidates; normalizing unicode punctuation only.")
        else:
            print("\nNo multi-line entries to flatten and no overlong hooks to tighten.")
            print("Manual archival of older sections is required.")

    if plan.projected_lines > cfg.max_lines:
        print(
            f"\nWARNING: even after compacting, MEMORY.md would be {plan.projected_lines} lines (still over {cfg.max_lines})."
        )
        print("Manual archival of older entries is required.")
    if plan.projected_bytes > cfg.max_bytes:
        print(
            f"\nWARNING: even after compacting, MEMORY.md would be {plan.projected_bytes} bytes (still over {cfg.max_bytes})."
        )
        print("Manual archival of older entries is required.")

    if not apply:
        return 0

    if commit:
        author_error = validate_author_format(commit_author)
        if author_error:
            print(
                f'brigade memory: invalid --commit-author: {author_error}\n  fix: use `--commit-author "Name <email>"`',
                file=sys.stderr,
            )
            return 2
        if not is_git_repo(cfg.memory_dir):
            print(
                f"brigade memory: --commit requires the memory dir to be a git repo\n"
                f"  memory dir: {cfg.memory_dir}\n"
                f"  fix: run `brigade memory init-git` once, then retry",
                file=sys.stderr,
            )
            return 2
        ok, reason = working_tree_sane(cfg.memory_dir)
        if not ok:
            print(
                f"brigade memory: refusing to commit, git is in the middle of a {reason}\n"
                f"  fix: complete or abort the in-progress operation, then retry",
                file=sys.stderr,
            )
            return 2
        card_targets = [cfg.memory_dir / f.target_name for f in plan.flattens] + [
            cfg.memory_dir / t.target_name for t in plan.tightens
        ]
        planned = card_targets + [index_path]
        dirty = files_have_uncommitted_changes(cfg.memory_dir, planned)
        if dirty:
            print(
                "brigade memory: refusing to commit, target files have uncommitted local changes:",
                file=sys.stderr,
            )
            for path, status in dirty:
                print(f"  - {path.name} ({status})", file=sys.stderr)
            print("  fix: review with `git diff`, commit/stash/discard, then retry", file=sys.stderr)
            return 2

    _apply_flatten(cfg.memory_dir, plan)
    print(f"\nApplied. MEMORY.md now {plan.projected_lines} lines, ~{plan.projected_bytes} bytes.")

    if not commit:
        return 0

    # Dedupe target cards (flatten + tighten may both touch the same card).
    seen: set[str] = set()
    card_files: list[Path] = []
    for name in [f.target_name for f in plan.flattens] + [t.target_name for t in plan.tightens]:
        if name not in seen:
            seen.add(name)
            card_files.append(cfg.memory_dir / name)
    files = card_files + [index_path]
    n_flat = len(plan.flattens)
    n_tight = len(plan.tightens)
    subject = (
        f"brigade memory compact: {n_flat} flattened, {n_tight} tightened, "
        f"MEMORY.md {plan.original_lines} -> {plan.projected_lines} lines, "
        f"{plan.original_bytes} -> {plan.projected_bytes} bytes"
    )
    body_lines = [
        f"- {f.target_name} (appended {len(f.detail_lines)}-line detail block from index)" for f in plan.flattens
    ]
    body_lines += [
        f"- {t.target_name} (moved overlong hook from index, {len(t.full_hook)} chars)" for t in plan.tightens
    ]
    line_delta = plan.original_lines - plan.projected_lines
    byte_delta = plan.original_bytes - plan.projected_bytes
    body_lines.append(f"- MEMORY.md (-{line_delta} lines, -{byte_delta} bytes)")
    body = "\n".join(body_lines)
    result = commit_run(
        memory_dir=cfg.memory_dir,
        files=files,
        subject=subject,
        body=body,
        author=commit_author,
    )
    if result.error_kind is None:
        print(f"\nCommitted {result.sha}")
        return 0
    if result.error_kind == "hook":
        print(
            "\nerror: pre-commit hook rejected the commit; your file changes are staged but not committed",
            file=sys.stderr,
        )
        print(f"  files: {', '.join(f.name for f in files)}", file=sys.stderr)
        print(f"  details: {result.error_message}", file=sys.stderr)
        return 1
    print(f"\nerror: commit failed ({result.error_kind}): {result.error_message}", file=sys.stderr)
    return 1
