"""Skill discovery and bundle fingerprinting for the outcome ledger.

Split from ``outcome_cmd`` to keep that module under the size ratchet.
Registry enumeration and state-root fingerprints go through the
``skills_cmd`` snapshot helpers so a directory symlink cannot pass
containment.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

# Align with Brigade public/slug identifiers (no arbitrary length cap).
_SAFE_SKILL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Files inside a skill bundle that are not part of its logic: OS cruft and the
# install-time metadata sidecar (which changes on every install, not on edit).
_BUNDLE_IGNORED_NAMES = frozenset({".DS_Store", "skill.json"})


def _artifact_id_has_controls(artifact_id: str) -> bool:
    """True when artifact_id contains Unicode control (Cc) or format (Cf) characters."""
    return any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in artifact_id)


def _safe_path_component(artifact_id: str) -> str | None:
    """Return artifact_id only when it is a single safe on-disk path component."""
    if not artifact_id or artifact_id in {".", ".."}:
        return None
    if any(sep in artifact_id for sep in ("/", "\\")):
        return None
    if artifact_id.startswith("~") or ":" in artifact_id:
        return None
    if any(ch in artifact_id for ch in "*?[]"):
        return None
    if _artifact_id_has_controls(artifact_id):
        return None
    return artifact_id


def _safe_skill_id_component(skill_id: str) -> str | None:
    """Return skill_id only when it is a single safe slug path component."""
    safe = _safe_path_component(skill_id)
    if safe is None:
        return None
    if _SAFE_SKILL_ID_RE.fullmatch(safe) is None:
        return None
    return safe


def _resolve_root(root: Path) -> Path | None:
    try:
        return root.expanduser().resolve()
    except OSError:
        return None


def _contained_under(root_resolved: Path, candidate: Path) -> Path | None:
    """Return candidate when it resolves to a real file inside root."""
    try:
        if not candidate.is_file():
            return None
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root_resolved):
        return None
    return candidate


def _skill_names_at(root: Path) -> set[str]:
    """Skill ids present under one checkout: harness installs or registry dirs."""
    names: set[str] = set()
    root_resolved = _resolve_root(root)
    if root_resolved is None:
        return names
    from . import skills_cmd

    for skill_md in root.glob(".*/skills/*/SKILL.md"):
        if skills_cmd._lexical_state_root_parts(root, skill_md) is not None:
            continue
        skill_id = _safe_skill_id_component(skill_md.parent.name)
        if skill_id is None:
            continue
        if _contained_under(root_resolved, skill_md) is None:
            continue
        names.add(skill_id)
    if (root / ".brigade").exists():
        try:
            with skills_cmd._held_state_root(root) as anchor:
                listed = skills_cmd._plain_subdirs_under_anchor(anchor, "skills", "registry")
                if listed:
                    for name in listed:
                        skill_id = _safe_skill_id_component(name)
                        if skill_id is None:
                            continue
                        try:
                            raw = skills_cmd._read_state_file_bytes(anchor, "skills", "registry", name, "SKILL.md")
                        except skills_cmd.SkillsStatePathError:
                            continue
                        if raw is not None:
                            names.add(skill_id)
        except skills_cmd.SkillsStatePathError:
            pass
    return names


def _linked_worktree_skill_roots(target: Path) -> list[Path]:
    """Target first, then linked-worktree parent. Never global home skill dirs."""
    roots = [target]
    from . import roster

    parent = roster._linked_worktree_parent(target)
    if parent is None:
        return roots
    try:
        if parent.resolve() == target.expanduser().resolve():
            return roots
    except OSError:
        return roots
    roots.append(parent)
    return roots


def _artifact_content_path_at(root: Path, artifact_id: str) -> Path | None:
    """Harness-installed copy first, then registry master, under one root."""
    skill_id = _safe_skill_id_component(artifact_id)
    if skill_id is None:
        return None
    root_resolved = _resolve_root(root)
    if root_resolved is None:
        return None
    from . import skills_cmd

    # Enumerate harness skill roots without interpolating the untrusted id into glob.
    # `.brigade/skills` matches this glob but is state-root content; skip it
    # so a registry directory symlink cannot pass containment.
    for skills_root in sorted(path for path in root.glob(".*/skills") if path.is_dir()):
        if skills_cmd._lexical_state_root_parts(root, skills_root) is not None:
            continue
        candidate = skills_root / skill_id / "SKILL.md"
        contained = _contained_under(root_resolved, candidate)
        if contained is not None:
            return contained
    if (root / ".brigade").exists():
        if skills_cmd._registry_entry_present(root, skill_id):
            registry_dir = root / ".brigade" / "skills" / "registry" / skill_id
            _dirs, files = skills_cmd._installed_tree_snapshot(root, registry_dir)
            if ("SKILL.md",) in files:
                return registry_dir / "SKILL.md"
    return None


def _bundle_fingerprint(skill_dir: Path) -> str | None:
    """sha256 over a skill's whole bundle, reducing to sha256(SKILL.md) for a lone file.

    CocoIndex's logic_tracking walks the fingerprint through nested calls so
    editing a helper invalidates its callers. A skill is a directory, not just
    SKILL.md, so a bundled helper is that skill's "helper": hashing only SKILL.md
    leaves a signal vouching for a bundle whose script has since changed. This
    folds every bundle file (path + content) into the fingerprint.

    A skill whose only content file is SKILL.md hashes to *exactly*
    ``sha256(SKILL.md)`` - byte-identical to the pre-bundle fingerprint - so
    existing single-file records are never invalidated. Only a genuinely
    multi-file bundle takes the composite path.

    Symlinks that resolve outside the selected skill directory are excluded so
    the fingerprint never hashes external dependency bytes.
    """
    skill_root = _resolve_root(skill_dir)
    if skill_root is None:
        return None
    files = sorted(
        p
        for p in skill_dir.rglob("*")
        if p.name not in _BUNDLE_IGNORED_NAMES and _contained_under(skill_root, p) is not None
    )
    if not files:
        return None
    skill_md = skill_dir / "SKILL.md"
    try:
        if files == [skill_md]:
            return hashlib.sha256(skill_md.read_bytes()).hexdigest()
        digest = hashlib.sha256()
        for path in files:
            rel = path.relative_to(skill_dir).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()
    except OSError:
        return None


def _bundle_fingerprint_from_files(files: dict[tuple[str, ...], bytes]) -> str | None:
    """``_bundle_fingerprint`` digest over an already-collected snapshot."""
    content = {parts: data for parts, data in files.items() if parts[-1] not in _BUNDLE_IGNORED_NAMES}
    if not content:
        return None
    if set(content) == {("SKILL.md",)}:
        return hashlib.sha256(content[("SKILL.md",)]).hexdigest()
    digest = hashlib.sha256()
    for parts in sorted(content, key=lambda key: "/".join(key)):
        digest.update("/".join(parts).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content[parts]).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _skill_bundle_fingerprint(target: Path, skill_dir: Path) -> str | None:
    """Fingerprint a skill bundle, snapshotting state-root copies through the anchor."""
    from . import skills_cmd

    for root in _linked_worktree_skill_roots(target):
        if skills_cmd._lexical_state_root_parts(root, skill_dir) is not None:
            _dirs, files = skills_cmd._installed_tree_snapshot(root, skill_dir)
            return _bundle_fingerprint_from_files(files)
    return _bundle_fingerprint(skill_dir)
