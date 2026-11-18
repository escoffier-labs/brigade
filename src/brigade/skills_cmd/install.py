"""Install commands and user-profile skill packages."""
# ruff: noqa: F401

from __future__ import annotations

from .mutation import skill_command

import contextlib
import difflib
import hashlib
import json
import os
import shlex
import shutil
import stat as stat_module
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, cast

from .. import __version__ as BRIGADE_VERSION
from .. import dirfd
from .. import mcp_server
from ..localio import slugify, utc_now_iso as _now
from ..projection import kernel as projection
from ..templates import template_root
from ..untrusted import scan_untrusted

from . import registry
from . import search_validate
from . import sync_history
from . import packs as _packs_mod


def _evaluate_install_dir(
    workspace: Path, harness: str, skill_id: str, *, leaf_link_is_local: bool = False
) -> tuple[Path, bool]:
    """Return ``(projected_install_dir, escapes_workspace)`` without raising.

    Read-only callers (fleet status) use this to describe a copy that resolves
    outside the workspace instead of treating it as a writable destination.

    ``leaf_link_is_local`` is for callers that classify the leaf themselves
    (``_installed_path_state``) and refuse or skip every non-plain state: a leaf
    that is a link (or a Windows reparse point) is then judged by where the
    link lives, never by where it points, so an outside-pointing link is
    reported and skipped like an in-workspace one instead of raising. A real
    (non-link) leaf resolves exactly as before, and a symlinked parent
    directory is still followed and judged by ``resolve()``.
    """
    if harness == "hermes":
        # Real Hermes reads skills from its own data dir (auto-discovered as a
        # local skill), not the repo. Install there so it actually takes effect.
        return registry._hermes_skills_root() / registry._slug(skill_id), False
    adapter = _adapter_map(workspace)[harness]
    install_dir = workspace / str(adapter["install_path"]).format(skill_id=skill_id)
    parts = _packs_mod._lexical_state_root_parts(workspace, install_dir)
    if parts is not None:
        # State paths are lexical projections only. Read-only callers inspect
        # them through held anchors; resolving here would follow a junction
        # before those callers can refuse it.
        return install_dir, not parts or ".." in install_dir.parts
    workspace_resolved = workspace.resolve()
    if leaf_link_is_local and (install_dir.is_symlink() or registry._is_reparse_point(install_dir)):
        resolved = install_dir.parent.resolve() / install_dir.name
    else:
        resolved = install_dir.resolve()
    escapes = (
        ".." in install_dir.parts or resolved == workspace_resolved or not resolved.is_relative_to(workspace_resolved)
    )
    return install_dir, escapes


def _install_dir(workspace: Path, harness: str, skill_id: str, *, leaf_link_is_local: bool = False) -> Path:
    install_dir, escapes = _evaluate_install_dir(workspace, harness, skill_id, leaf_link_is_local=leaf_link_is_local)
    if escapes:
        raise ValueError(f"skill install path escapes workspace: {install_dir}")
    parts = _packs_mod._lexical_state_root_parts(workspace, install_dir)
    if parts is not None:
        # Preserve the writable destination guard now that projection avoids
        # resolve(). Missing state is created only by the mutation owner.
        try:
            with registry._held_state_root(workspace, writable=False) as anchor:
                registry._state_entry_kind(anchor, *parts)
        except registry.SkillsStatePathError as exc:
            if isinstance(exc.__context__, FileNotFoundError):
                return install_dir
            if leaf_link_is_local and install_dir.is_symlink():
                # The caller classifies the linked leaf and skips it; nothing
                # reads or writes through it.
                return install_dir
            raise
    return install_dir


def _safe_install_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if path.is_absolute() or ".." in path.parts:
        return None
    return value.strip()


def _adapter_map(target: Path) -> dict[str, dict[str, Any]]:
    adapters = {key: dict(value) for key, value in registry.HARNESS_ADAPTERS.items()}
    config = registry._state_json_via_anchor(target, "skills", "adapters.json")
    overlay = config.get("adapters")
    if isinstance(overlay, dict):
        for adapter_id, value in overlay.items():
            if not isinstance(value, dict):
                continue
            adapters[registry._slug(str(adapter_id))] = {
                "status": str(value.get("status") or "local"),
                "format": str(value.get("format") or "custom-skill"),
                "install_path": _safe_install_path(value.get("install_path")),
                "source": "local-config",
            }
    return adapters


def _install_targets(workspace: Path) -> tuple[str, ...]:
    return tuple(
        key
        for key, value in _adapter_map(workspace).items()
        if value.get("status") in {"built-in", "local"} and value.get("install_path")
    )


def _latest_install_receipt(target: Path, skill_id: str, harness: str) -> dict[str, Any]:
    """Read the canonical receipt through the held state-root anchor.

    A symlinked (or otherwise refused) receipt contributes no data: callers
    report the installation as unknown instead of surfacing outside JSON.
    """
    try:
        with registry._held_state_root(target, writable=False) as state_anchor:
            raw = registry._read_state_file_bytes(
                state_anchor, "skills", "installs", f"{registry._slug(skill_id)}-{harness}.json"
            )
    except registry.SkillsStatePathError:
        return {}
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _install_history(target: Path, skill_id: str | None = None, harness: str | None = None) -> list[dict[str, Any]]:
    """Read ``history.jsonl`` through the held state-root anchor.

    A symlinked or refused history file yields no rows rather than following
    the link to outside content.
    """
    try:
        with registry._held_state_root(target, writable=False) as state_anchor:
            raw = registry._read_state_file_bytes(state_anchor, "skills", "installs", "history.jsonl")
    except registry.SkillsStatePathError:
        raw = None
    rows: list[dict[str, Any]] = []
    if raw is not None:
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    if skill_id is not None:
        rows = [row for row in rows if row.get("skill_id") == registry._slug(skill_id)]
    if harness is not None:
        rows = [row for row in rows if row.get("target") == harness]
    rows.sort(key=lambda row: str(row.get("installed_at") or row.get("receipt_id") or ""), reverse=True)
    return rows


def _renderer_identity(contract: dict[str, Any]) -> str:
    identity = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"skills-renderer:{identity}"


def _renderer_contract(target: Path, harness: str) -> dict[str, Any]:
    adapter = _adapter_map(target).get(harness, {})
    contract = {
        "schema_version": registry.RENDERER_SCHEMA_VERSION,
        "harness": harness,
        "format": adapter.get("format"),
    }
    return {**contract, "identity": _renderer_identity(contract)}


def _valid_receipt_contract(receipt: dict[str, Any], *, skill_id: str, harness: str) -> bool:
    source = receipt.get("source")
    render = receipt.get("render")
    installed = receipt.get("installed")
    if receipt.get("schema_version") != registry.RECEIPT_SCHEMA_VERSION:
        return False
    if receipt.get("skill_id") != skill_id or receipt.get("target") != harness:
        return False
    if not isinstance(source, dict) or not isinstance(render, dict) or not isinstance(installed, dict):
        return False
    if source.get("schema_version") != registry.SOURCE_SCHEMA_VERSION:
        return False
    if not all(
        isinstance(source.get(key), str) and source.get(key)
        for key in ("identity", "fingerprint", "metadata_fingerprint")
    ):
        return False
    kind = source.get("kind")
    identity = source["identity"]
    reviewed = source.get("reviewed")
    if not isinstance(reviewed, bool) or not isinstance(source.get("skill_version"), str):
        return False
    if kind == "brigade-bundle":
        if (
            identity != f"{registry.BUNDLED_SOURCE_PREFIX}{skill_id}"
            or reviewed is not True
            or not isinstance(source.get("brigade_version"), str)
            or not source["brigade_version"]
        ):
            return False
    elif kind == "registry":
        if identity != f"registry://skills/{skill_id}" or reviewed is not False:
            return False
    elif kind == "path":
        source_path = identity.removeprefix("path:")
        if identity == source_path or reviewed is not False or not Path(source_path).is_absolute():
            return False
    else:
        return False
    if not all(isinstance(render.get(key), str) and render.get(key) for key in ("identity", "fingerprint")):
        return False
    render_contract = {
        "schema_version": render.get("schema_version"),
        "harness": render.get("harness"),
        "format": render.get("format"),
    }
    if (
        type(render_contract["schema_version"]) is not int
        or render_contract["schema_version"] < 1
        or render_contract["harness"] != harness
        or not isinstance(render_contract["format"], str)
        or not render_contract["format"]
        or render["identity"] != _renderer_identity(render_contract)
    ):
        return False
    if not all(
        isinstance(installed.get(key), str) and installed.get(key)
        for key in ("bundle_fingerprint", "skill_fingerprint", "metadata_fingerprint")
    ):
        return False
    return installed.get("skill_fingerprint") == render.get("fingerprint")


def _previous_install_receipt(anchor: registry._StateRootAnchor, *, skill_id: str, harness: str) -> dict[str, Any]:
    """Read the canonical receipt through the anchor; retain only contract-valid receipts.

    The read goes through ``_read_state_file_bytes`` (``O_NOFOLLOW``, regular
    file, single link), so a symlinked or hardlinked receipt refuses the
    operation instead of dragging outside content into state or output.
    Content that parses but does not satisfy the receipt contract contributes
    no ``previous_receipt``.
    """
    raw = registry._read_state_file_bytes(anchor, "skills", "installs", f"{skill_id}-{harness}.json")
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or not _valid_receipt_contract(data, skill_id=skill_id, harness=harness):
        return {}
    return data


def _dir_fd_subtree_plain(fd: int) -> bool:
    for name in dirfd.child_names(fd, sys.maxsize):
        try:
            st = dirfd.stat_child(fd, name, allow_reparse=True)
        except OSError:
            return False
        if stat_module.S_ISLNK(st.st_mode):
            return False
        if stat_module.S_ISDIR(st.st_mode):
            try:
                child = dirfd.open_child_directory(fd, name, writable=False)
            except OSError:
                return False
            try:
                if not _dir_fd_subtree_plain(child):
                    return False
            finally:
                os.close(child)
        elif not stat_module.S_ISREG(st.st_mode):
            return False
    return True


def _anchor_subtree_is_plain(anchor: registry._StateRootAnchor, *relative: str) -> bool:
    """True when the anchored subtree holds only plain directories and files.

    A skipped symlink anywhere below means the on-disk copy is richer than
    anything the collector can return; mutation planners must refuse such a
    destination instead of writing over or around the planted entry.
    """
    if not registry._HAS_DESCRIPTOR_ANCHOR:
        raise registry._fallback_mutation_refusal("reads", anchor.workspace / ".brigade")
    try:
        fd, opened = registry._anchor_open_chain(anchor, *relative, missing_ok=True, writable=False)
    except registry.SkillsStatePathError:
        return False
    if fd is None:
        return True
    try:
        try:
            return _dir_fd_subtree_plain(fd)
        except OSError:
            return False
    finally:
        for held in reversed(opened):
            os.close(held)


def _plain_link_target(target: str) -> str:
    r"""Drop the Windows extended-length prefix so a target reads the same everywhere.

    ``os.readlink`` returns ``\\?\D:\x`` for an absolute Windows link and
    ``\\?\UNC\srv\share`` for a network one; report ``D:\x`` and ``\\srv\share``.
    The result is display text only: nothing opens, resolves, or compares it.
    """
    if target[:8].upper() == "\\\\?\\UNC\\":
        return "\\\\" + target[8:]
    if target.startswith("\\\\?\\"):
        return target[4:]
    return target


def _link_target(installed_dir: Path) -> str | None:
    """Read one symlink's target string without following the link."""
    try:
        return _plain_link_target(os.readlink(installed_dir))
    except OSError:
        return None


def _installed_path_state(
    workspace: Path, installed_dir: Path, anchor: registry._StateRootAnchor | None = None
) -> tuple[str, str | None]:
    """Classify an installed copy path and return ``(state, link_target)``.

    States are ``dir``, ``missing``, ``symlink``, ``file``, ``other``, and
    ``unreadable``. The leaf is never followed: ``lstat``/``readlink`` and the
    anchored state-root walk classify it, so callers can report a symlinked or
    otherwise non-plain install instead of walking through it. A symlinked
    PARENT directory outside the state root is still followed by the OS; only
    the leaf is classified here.
    """
    parts = _packs_mod._lexical_state_root_parts(workspace, installed_dir)
    if parts is not None:
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return "other", None
        try:
            if anchor is not None:
                kind = registry._state_entry_kind(anchor, *parts)
            else:
                with registry._held_state_root(workspace, writable=False) as held:
                    kind = registry._state_entry_kind(held, *parts)
        except registry.SkillsStatePathError as exc:
            if isinstance(exc.__context__, FileNotFoundError):
                # No state root yet, so nothing is installed there (same
                # distinction _install_dir makes); nothing to look up by path.
                return "missing", None
            if installed_dir.is_symlink():
                # The anchored walk refused: a redirected leaf is never
                # followed, it is the non-plain entry it is. This runs only
                # after that refusal, to report the link and where it points.
                return "symlink", _link_target(installed_dir)
            return "unreadable", None
        # No pathname probe here: the anchored lookup already reports a link or
        # reparse point at the leaf as a refusal (handled above), and a lookup
        # by path under the state root is what the containment tests forbid.
        return kind if kind in {"missing", "dir", "file"} else "other", None
    try:
        info = os.lstat(installed_dir)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "other", None
    if stat_module.S_ISLNK(info.st_mode):
        return "symlink", _link_target(installed_dir)
    if registry._is_reparse_point(installed_dir):
        # An NTFS junction reads as a directory to lstat; it is still a link.
        return "other", None
    if stat_module.S_ISDIR(info.st_mode):
        return "dir", None
    if stat_module.S_ISREG(info.st_mode):
        return "file", None
    return "other", None


def _remove_plain_install_dir(dest: Path) -> None:
    """Remove one installed directory, refusing a link or reparse point at the leaf.

    Callers classify the leaf first; this repeats the check immediately before
    the removal so a link planted after the classification is refused, never
    followed or deleted.
    """
    if dest.is_symlink() or registry._is_reparse_point(dest):
        raise registry.SkillsStatePathError(f"installed copy changed to a link while writing; left untouched: {dest}")
    shutil.rmtree(dest)


_STATE_PHRASES = {
    "symlink": "a symlink",
    "file": "a regular file",
    "other": "not a plain directory",
    "unreadable": "not readable as a plain directory",
}


def _installed_state_reason(state: str, link_target: str | None, installed_dir: Path) -> str:
    """One human-readable line explaining why an installed copy was skipped."""
    phrase = _STATE_PHRASES.get(state, "not a plain directory")
    target = f" to {link_target}" if state == "symlink" and link_target else ""
    return f"installed copy is {phrase}{target} ({installed_dir}); left untouched"


@dataclass(frozen=True)
class InstalledTreeInspection:
    """Read-only view of one installed copy; a link at the leaf is reported, not followed."""

    state: str
    link_target: str | None
    dirs: list[tuple[str, ...]]
    files: dict[tuple[str, ...], bytes]


def _inspect_installed_tree(
    workspace: Path, installed_dir: Path, anchor: registry._StateRootAnchor | None = None
) -> InstalledTreeInspection:
    """Inspect an installed copy, reporting a non-plain path as its own state.

    Copies located inside ``.brigade`` (e.g. the built-in ``mcp`` target's
    ``mcp-resources``) are state-root content: they are read through the held
    state-root descriptor, so a planted symlink is skipped or refuses instead
    of dragging outside bytes into diffs, drift, or rollback material. Trusted
    harness directories outside the state root keep the plain descriptor walk.
    A symlinked, dangling, regular-file, or otherwise refused leaf yields an
    empty snapshot and the state that explains it, so read-only callers never
    raise and never follow a link at the leaf (a symlinked parent directory is
    still followed outside the state root).
    """
    state, link_target = _installed_path_state(workspace, installed_dir, anchor=anchor)
    if state != "dir":
        return InstalledTreeInspection(state, link_target, [], {})
    parts = _packs_mod._lexical_state_root_parts(workspace, installed_dir)
    if parts is None:
        try:
            dirs, files = registry._collect_source_tree(installed_dir)
        except registry.SkillsStatePathError:
            return InstalledTreeInspection("unreadable", link_target, [], {})
        return InstalledTreeInspection("dir", link_target, dirs, files)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return InstalledTreeInspection("unreadable", link_target, [], {})
    try:
        if anchor is not None:
            dirs, files = registry._read_tree_from_anchor(anchor, *parts)
        else:
            with registry._held_state_root(workspace, writable=False) as held:
                dirs, files = registry._read_tree_from_anchor(held, *parts)
    except registry.SkillsStatePathError:
        return InstalledTreeInspection("unreadable", link_target, [], {})
    return InstalledTreeInspection("dir", link_target, dirs, files)


def _installed_tree_snapshot(
    workspace: Path, installed_dir: Path, anchor: registry._StateRootAnchor | None = None
) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
    """Collect an installed copy's plain files for inspection.

    A non-plain installed path (symlink, dangling link, regular file, refused
    subtree) contributes an empty snapshot; use ``_inspect_installed_tree``
    when the caller also needs the state that explains the absence.
    """
    inspection = _inspect_installed_tree(workspace, installed_dir, anchor=anchor)
    return inspection.dirs, inspection.files


def _local_edit_state(
    receipt: dict[str, Any], files: dict[tuple[str, ...], bytes], *, skill_id: str, harness: str
) -> str:
    """Compare installed bytes with the receipt: ``current``, ``changed`` or ``unknown``.

    ``unknown`` means the bytes cannot be judged: no SKILL.md on disk, no
    receipt satisfying the contract to prove what was installed, or a receipt
    from before ``installed.edit_fingerprint`` that cannot vouch for a nested
    ``skill.json``. Callers that protect hand edits treat only ``current`` as
    safe to replace; ``changed`` is a proven edit.
    """
    installed_raw = files.get(("SKILL.md",))
    if installed_raw is None or not receipt or not _valid_receipt_contract(receipt, skill_id=skill_id, harness=harness):
        return "unknown"
    installed = receipt["installed"]
    if installed.get("skill_fingerprint") != registry._text_fingerprint(
        installed_raw.decode("utf-8", errors="replace")
    ) or installed.get("metadata_fingerprint") != registry._bytes_fingerprint(files.get(("skill.json",))):
        return "changed"
    edit_fingerprint = installed.get("edit_fingerprint")
    if isinstance(edit_fingerprint, str) and edit_fingerprint:
        return "current" if edit_fingerprint == registry._edit_fingerprint(files) else "changed"
    # The receipt predates the edit digest, whose bundle digest ignored every
    # skill.json. A difference elsewhere is a real edit; matching bytes are only
    # provable pristine when no nested skill.json exists to have been skipped.
    if installed.get("bundle_fingerprint") != registry._files_fingerprint(files):
        return "changed"
    nested_metadata = any(parts[-1] == "skill.json" and parts != ("skill.json",) for parts in files)
    return "unknown" if nested_metadata else "current"


@dataclass(frozen=True)
class TreeIdentity:
    """What a drift check saw of an installed copy: its directory inode and a digest of every byte."""

    device: int
    inode: int
    digest: str


def _tree_identity(installed_dir: Path, inspection: InstalledTreeInspection) -> TreeIdentity | None:
    """Identity of the directory that was just inspected, or ``None`` when it is not a plain directory."""
    if inspection.state != "dir":
        return None
    try:
        info = os.lstat(installed_dir)
    except OSError:
        return None
    digest = hashlib.sha256()
    for parts in sorted(inspection.dirs):
        digest.update(b"d\0" + "/".join(parts).encode("utf-8") + b"\0")
    for parts in sorted(inspection.files):
        digest.update(b"f\0" + "/".join(parts).encode("utf-8") + b"\0")
        digest.update(inspection.files[parts] + b"\0")
    return TreeIdentity(info.st_dev, info.st_ino, digest.hexdigest())


def _require_tree_unchanged(
    expected: TreeIdentity | None,
    workspace: Path,
    installed_dir: Path,
    *,
    anchor: registry._StateRootAnchor | None = None,
) -> None:
    """Refuse the mutation unless the copy is still exactly what the drift check saw.

    The edit check and the removal it protects are separated by snapshots,
    lints and binding checks, so a hand edit or a swapped directory can land in
    between. Callers run this immediately before each removal or replacement.
    """
    actual = _tree_identity(installed_dir, _inspect_installed_tree(workspace, installed_dir, anchor=anchor))
    if expected is None or actual != expected:
        raise registry.SkillsStatePathError(
            f"installed copy changed after it was checked: {installed_dir}; left untouched, run again"
        )


def _local_edit_diff_command(*, workspace: Path, selector: str, harness: str, against: str) -> str:
    return shlex.join(
        ["brigade", "skills", "diff", selector, "--target", str(workspace), "--harness", harness, "--against", against]
    )


def _local_edit_selector(receipt: dict[str, Any], *, skill_id: str, fallback: str) -> tuple[str, str]:
    """Selector and ``--against`` baseline for the diff and discard commands.

    Derived from the receipt's recorded source so the printed commands name the
    same source the copy was installed from; ``fallback`` is the caller's own
    selector when the receipt records none that can be named.
    """
    from .adapters import _fleet_source_selector

    selector = _fleet_source_selector(skill_id, receipt) or fallback
    return selector, "registry" if selector.startswith("registry:") else "bundled"


def _local_edit_discard_command(*, workspace: Path, selector: str, harness: str) -> str:
    """The explicit override: reinstall over the edit; a rollback snapshot is taken first."""
    return shlex.join(
        [
            "brigade",
            "skills",
            "install",
            selector,
            "--workspace",
            str(workspace),
            "--target",
            harness,
            "--force",
            "--discard-local-edits",
        ]
    )


def _local_edit_rollback_discard_command(*, workspace: Path, skill_id: str, harness: str) -> str:
    """The explicit override for a rollback: the edit is snapshotted, then replaced."""
    return shlex.join(
        [
            "brigade",
            "skills",
            "rollback",
            skill_id,
            "--workspace",
            str(workspace),
            "--target",
            harness,
            "--discard-local-edits",
        ]
    )


def installed_local_edit(workspace: Path, skill_id: str, harness: str) -> str:
    """Read-only edit state of an installed copy against its receipt.

    ``current``, ``changed`` or ``unknown``, plus ``missing`` when no copy exists.
    Autonomous callers (outcome reconcile) use it to leave a hand-edited or
    unverifiable copy alone before they roll back or uninstall; they never see
    the bytes. ``unknown`` is a veto exactly like ``changed``: a receipt the
    agent's own user can delete or rewrite may stop a write, never authorize it.
    """
    try:
        dest = _install_dir(workspace, harness, skill_id, leaf_link_is_local=True)
    except (ValueError, registry.SkillsStatePathError):
        return "unknown"
    inspection = _inspect_installed_tree(workspace, dest)
    if inspection.state == "missing":
        return "missing"
    receipt = _latest_install_receipt(workspace, skill_id, harness)
    return _local_edit_state(receipt, inspection.files, skill_id=skill_id, harness=harness)


def _local_edit_reason(*, installed_dir: Path, diff_command: str, discard_command: str) -> str:
    return (
        f"installed copy {installed_dir} has local edits and was kept; "
        f"review with `{diff_command}`, or overwrite with `{discard_command}` "
        "(a rollback snapshot is taken first)"
    )


def _drift_payload(
    *,
    target: Path,
    skill_id: str,
    harness: str,
    lint_payload: dict[str, Any],
    rendered: str,
    installed_dir: Path,
    inspection: InstalledTreeInspection | None = None,
) -> dict[str, Any]:
    receipt = _latest_install_receipt(target, skill_id, harness)
    if inspection is None:
        inspection = _inspect_installed_tree(target, installed_dir)
    path_state = inspection.state
    snapshot_files = inspection.files
    installed_raw = snapshot_files.get(("SKILL.md",))
    installed_present = installed_raw is not None
    installed_text = installed_raw.decode("utf-8", errors="replace") if installed_raw is not None else ""
    current_source = raw_current_source if isinstance((raw_current_source := lint_payload.get("source")), dict) else {}
    current_render = _renderer_contract(target, harness)
    current_render_fingerprint = registry._text_fingerprint(rendered)
    installed_skill_fingerprint = registry._text_fingerprint(installed_text) if installed_present else None
    installed_bundle_fingerprint = registry._files_fingerprint(snapshot_files) if snapshot_files else None
    installed_metadata_fingerprint = registry._bytes_fingerprint(snapshot_files.get(("skill.json",)))
    known = _valid_receipt_contract(receipt, skill_id=skill_id, harness=harness)
    if path_state == "symlink":
        # A linked installed copy is reported, never followed or diffed (leaf only).
        overall = "symlink"
        source_state = "unknown"
        render_state = "unknown"
        local_state = "unknown"
    elif path_state != "dir" and path_state != "missing":
        overall = "unusable"
        source_state = "unknown"
        render_state = "unknown"
        local_state = "unknown"
    elif not installed_present and (snapshot_files or (receipt and not known)):
        # Leftover files without SKILL.md are neither missing nor current: a
        # write would have to guess what to overwrite, so every reader reports
        # unknown and ``install --force`` is the only way to repair it.
        overall = "unknown"
        source_state = "unknown"
        render_state = "unknown"
        local_state = "unknown"
    elif not installed_present:
        overall = "missing"
        source_state = "unknown"
        render_state = "unknown"
        local_state = "unknown"
    elif not known:
        overall = "unknown"
        source_state = "unknown"
        render_state = "unknown"
        local_state = "unknown"
    else:
        receipt_source = receipt["source"]
        receipt_render = receipt["render"]
        source_state = (
            "current"
            if receipt_source.get("identity") == current_source.get("identity")
            and receipt_source.get("fingerprint") == current_source.get("fingerprint")
            and receipt_source.get("metadata_fingerprint") == current_source.get("metadata_fingerprint")
            else "changed"
        )
        render_state = "current" if receipt_render.get("identity") == current_render.get("identity") else "changed"
        local_state = _local_edit_state(receipt, snapshot_files, skill_id=skill_id, harness=harness)
        overall = "changed" if "changed" in {source_state, render_state, local_state} else "current"
    return {
        "overall": overall,
        "source": source_state,
        "render": render_state,
        "local_edit": local_state,
        "receipt_known": known,
        "content_changed": installed_text != rendered,
        "installed": installed_present,
        "path_state": path_state,
        "link_target": inspection.link_target,
        "installed_skill_fingerprint": installed_skill_fingerprint,
        "installed_bundle_fingerprint": installed_bundle_fingerprint,
        "installed_metadata_fingerprint": installed_metadata_fingerprint,
        "current_source": current_source,
        "current_render": {**current_render, "fingerprint": current_render_fingerprint},
        "receipt": receipt or None,
    }


def _review_binding(
    workspace: Path,
    skill_id: str,
    harness: str,
    selector: str | None,
    *,
    anchor: Any = None,
    source_files: dict[tuple[str, ...], bytes] | None = None,
    rollback_files: dict[tuple[str, ...], bytes] | None = None,
    installed_files: dict[tuple[str, ...], bytes] | None = None,
    target_files: dict[tuple[str, ...], bytes] | None = None,
) -> dict[str, Any] | None:
    """Observe exact bytes through existing no-follow collectors, never receipt hashes."""
    from ..localio import canonical_json_digest
    from ..fleet_client import resolve_node_id

    node_id = resolve_node_id()
    if node_id == "unknown":
        return None

    if anchor is None:
        with registry._held_state_root(workspace, writable=False) as held:
            return _review_binding(
                workspace,
                skill_id,
                harness,
                selector,
                anchor=held,
                source_files=source_files,
                rollback_files=rollback_files,
                installed_files=installed_files,
                target_files=target_files,
            )
    receipt = _previous_install_receipt(anchor, skill_id=skill_id, harness=harness)
    if not _valid_receipt_contract(receipt, skill_id=skill_id, harness=harness):
        return None
    from .adapters import _fleet_source_selector

    if _fleet_source_selector(skill_id, receipt) != selector:
        return None
    if selector not in {"registry:" + skill_id, "bundled:" + skill_id}:
        return None
    if source_files is None:
        if selector.startswith("registry:"):
            _dirs, source_files = registry._read_registry_entry_tree(anchor, skill_id)
        else:
            _dirs, source_files = registry._collect_source_tree(registry._bundled_skill_path(skill_id))
    dest = _install_dir(workspace, harness, skill_id)
    if installed_files is None:
        inspection = _inspect_installed_tree(workspace, dest, anchor=anchor)
        if inspection.state not in {"dir", "missing"}:
            # A link, regular file, or a directory the no-follow collector
            # refuses (hardlink, unreadable entry) has no honest installed
            # identity; refuse instead of binding an empty one.
            raise registry.SkillsStatePathError(_installed_state_reason(inspection.state, inspection.link_target, dest))
        installed_files = inspection.files
    rollback = None
    snapshot = receipt.get("rollback_snapshot")
    if snapshot is not None:
        try:
            relative = Path(snapshot).relative_to(registry._rollback_root(workspace, skill_id, harness))
        except (ValueError, TypeError):
            return None
        if len(relative.parts) != 1 or relative.parts[0] in {"", ".", ".."}:
            return None
        if rollback_files is None:
            _dirs, rollback_files = registry._read_tree_from_anchor(
                anchor, "skills", "rollback", skill_id, harness, relative.parts[0]
            )
        rollback = canonical_json_digest(
            {"snapshot": relative.parts[0], "package": registry._package_identity(rollback_files)}
        )
    metadata = registry._metadata_from_collected(source_files)
    if target_files is None:
        target_files = dict(source_files)
        raw = source_files.get(("SKILL.md",), b"")
        rendered = registry._render_skill_text_for_harness(
            raw.decode("utf-8", errors="replace"), metadata, skill_id, harness
        )
        target_files[("SKILL.md",)] = rendered.encode("utf-8")
    contract = metadata.get("behavioral_contract")
    # Future paired evaluation publishes a validated digest here; absence remains null.
    evaluation = metadata.get("evaluation_evidence_sha256")
    result = {
        "schema_version": 1,
        "node": node_id,
        "workspace": registry._text_fingerprint(str(workspace.resolve())),
        "skill_id": skill_id,
        "harness": harness,
        "source_selector": selector,
        "installed": registry._package_identity(installed_files),
        "source": registry._package_identity(source_files),
        "target": registry._package_identity(target_files),
        "rollback": rollback,
        "contract": canonical_json_digest(contract) if contract is not None else None,
        "evaluation": evaluation,
    }
    return registry._validate_expected_binding(result)


def _check_review_binding(
    expected: dict[str, Any], workspace: Path, skill_id: str, harness: str, **kwargs: Any
) -> None:
    try:
        registry._validate_expected_binding(expected)
        observed = _review_binding(workspace, skill_id, harness, expected["source_selector"], **kwargs)
    except (ValueError, OSError, registry.SkillsStatePathError) as exc:
        raise registry.SkillsStatePathError("review-stale: package provenance unavailable") from exc
    if observed != expected:
        raise registry.SkillsStatePathError("review-stale: expected package binding changed; review again")


@skill_command
def install(
    *,
    workspace: Path,
    skill: str,
    harness: str,
    force: bool = False,
    discard_local_edits: bool = False,
    json_output: bool = False,
    expected_binding: dict[str, Any] | None = None,
) -> int:
    """Install a skill."""
    workspace = workspace.expanduser().resolve()
    install_targets = _install_targets(workspace)
    if harness not in (*install_targets, "all"):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    # Fail fast on a redirected or broken state root so the typed refusal
    # surfaces the same way for every source kind.
    try:
        registry._hold_state_root(workspace).close()
    except registry.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Resolve the source once (the metadata read here is discarded), then
    # collect the source tree exactly once before anything validates it.
    # Metadata, lint, rendering, and every fingerprint below consume only
    # these collected bytes, so a source swapped between validation steps can
    # never make the command install a generation nobody linted. Registry
    # selections are collected through the held state-root anchor; external
    # and bundled sources are trusted operator paths.
    try:
        source_dir, _discarded_metadata, resolved_source = registry._load_skill(workspace, skill)
    except registry.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    collected: tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]] | None = None
    staged_dir: Path | None = None
    staging_root: str | None = None
    try:
        if resolved_source.get("kind") == "registry":
            # Collect exactly the entry that was requested (by its directory
            # name), never one named by attacker-controlled metadata.
            skill_slug = registry._slug(skill.removeprefix("registry:") if skill.startswith("registry:") else skill)
            try:
                with registry._held_state_root(workspace) as state_anchor:
                    snapshot_dirs, snapshot_files = registry._read_registry_entry_tree(state_anchor, skill_slug)
            except registry.SkillsStatePathError:
                raise
        else:
            snapshot_dirs, snapshot_files = registry._collect_source_tree(source_dir)
        collected = (snapshot_dirs, snapshot_files)
        with tempfile.TemporaryDirectory(prefix="brigade-install-lint-") as staging:
            staged_dir = Path(staging) / source_dir.name
            staging_root = staging
            registry._write_snapshot_tree(snapshot_dirs, snapshot_files, staged_dir)
            lint_payload = search_validate._lint_payload(Path(staging), str(staged_dir))
            # Repoint immediately so every later consumer — success or
            # failure — references the real source, never the ephemeral
            # staging directory.
            lint_payload["target"] = str(workspace)
            lint_payload = search_validate._repoint_staged_lint_paths(lint_payload, source_dir, staged_dir)
    except registry.SkillsStatePathError as exc:
        lint_payload = {
            "target": str(workspace),
            "skill_dir": str(source_dir),
            "skill_id": source_dir.name,
            "valid": False,
            "errors": [str(exc)],
            "warnings": [],
            "harness": None,
            "render_errors": [],
            "injection": {"flagged": False, "count": 0, "markers": []},
            "metadata": {},
            "source": {},
            "agent_skills": {
                "mode": "lenient",
                "fields": {},
                "diagnostics": [],
                "allowed_tools_are_permissions": False,
            },
            "fingerprint": None,
            "changelog": {"present": False, "path": None, "fingerprint": None, "headings": []},
            "trust_score": {
                "score": 0,
                "trust_level": "unreviewed",
                "signals": ["skill directory unreadable"],
                "tests_declared": 0,
                "changelog": {"present": False, "path": None, "fingerprint": None, "headings": []},
            },
        }
    lint_payload["target"] = str(workspace)
    lint_payload["skill_dir"] = str(source_dir)
    snapshot_dirs, snapshot_files = collected if collected is not None else ([], {})
    source_skill_md = snapshot_files.get(("SKILL.md",))
    if source_skill_md is None:
        # The collector skips symlinks instead of following them: a symlinked
        # SKILL.md must refuse the install rather than let the pathname
        # re-reads below launder outside content into state.
        message = "SKILL.md must be a plain regular file in the skill source (symlinks are never followed): " + str(
            registry._skill_md_path(source_dir)
        )
        if json_output:
            print(
                json.dumps(
                    {"workspace": str(workspace), "installed": False, "errors": [message], "lint": lint_payload},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: {message}", file=sys.stderr)
        return 2
    if not lint_payload["valid"]:
        if json_output:
            print(
                json.dumps(
                    {"workspace": str(workspace), "installed": False, "lint": lint_payload}, indent=2, sort_keys=True
                )
            )
        else:
            print(f"error: skill lint failed: {skill}", file=sys.stderr)
        return 1
    skill_id = registry._slug(str(lint_payload["skill_id"]))
    metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else {}
    source_identity = dict(resolved_source)
    source_identity["skill_version"] = str(metadata.get("version") or "0.1.0")
    version = str(metadata.get("version") or "0.1.0")
    source_path = str(metadata.get("source") or source_dir)
    snapshot_fingerprint = registry._files_fingerprint(snapshot_files)
    # Provenance is derived from the original resolution; the fingerprints are
    # derived from the collected snapshot. Together they describe exactly the
    # generation that was linted and that the copies below materialize.
    source_identity["fingerprint"] = snapshot_fingerprint
    source_identity["metadata_fingerprint"] = registry._bytes_fingerprint(snapshot_files.get(("skill.json",)))
    lint_payload["fingerprint"] = snapshot_fingerprint
    lint_payload["source"] = source_identity
    # Trust scoring is provenance-sensitive: recompute it against the real
    # resolution so a staged-lint run scores the skill exactly like a direct
    # lint of the source would.
    if staged_dir is not None:
        lint_payload["trust_score"] = registry._trust_score_payload(staged_dir, metadata, lint_payload, source_identity)
    # The staged copy must not leak its temporary location into output or
    # receipts: report changelog paths against the real source directory.
    trust_block = lint_payload.get("trust_score")
    for container in (
        lint_payload.get("changelog"),
        trust_block.get("changelog") if isinstance(trust_block, dict) else None,
    ):
        if isinstance(container, dict):
            path_text = container.get("path")
            if staging_root is not None and isinstance(path_text, str) and path_text.startswith(staging_root):
                container["path"] = str(source_dir / Path(path_text).relative_to(staging_root))
    if expected_binding is not None and harness == "all":
        print("error: review-stale: expected binding requires one explicit harness", file=sys.stderr)
        return 2
    targets = install_targets if harness == "all" else (harness,)
    receipts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        state_anchor = registry._hold_state_root(workspace)
    except registry.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        for install_target in targets:
            if install_target == "hermes" and not registry._hermes_home().exists():
                # Do not create ~/.hermes for someone who does not run Hermes.
                skipped.append(
                    {
                        "target": "hermes",
                        "reason": "Hermes home not found (is Hermes installed?)",
                        "not_applicable": True,
                    }
                )
                continue
            try:
                dest = _install_dir(workspace, install_target, skill_id, leaf_link_is_local=True)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            source_text = source_skill_md.decode("utf-8", errors="replace")
            rendered_text = registry._render_skill_text_for_harness(source_text, metadata, skill_id, install_target)
            render_fingerprint = registry._text_fingerprint(rendered_text)
            render_contract = _renderer_contract(workspace, install_target)
            render_errors = registry._rendered_skill_validation(rendered_text, install_target)
            if render_errors:
                if json_output:
                    print(
                        json.dumps(
                            {
                                "workspace": str(workspace),
                                "installed": False,
                                "target": install_target,
                                "errors": render_errors,
                                "lint": lint_payload,
                            },
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    for error in render_errors:
                        print(f"error: {install_target}: {error}", file=sys.stderr)
                return 1
            install_files = dict(snapshot_files)
            install_files[("SKILL.md",)] = rendered_text.encode("utf-8")
            dest_state, dest_link_target = _installed_path_state(workspace, dest, anchor=state_anchor)
            if dest_state not in {"dir", "missing"}:
                # A symlinked, dangling, or otherwise non-plain destination is
                # never replaced, followed, or cleaned up, with or without --force.
                # An explicit single harness without --force keeps the old
                # non-zero exit; --force or --harness all records the skip.
                reason = _installed_state_reason(dest_state, dest_link_target, dest)
                if harness != "all" and not force:
                    print(f"error: installed skill already exists and was not replaced: {reason}", file=sys.stderr)
                    return 2
                skipped.append({"target": install_target, "reason": reason})
                continue
            install_rel = str(_adapter_map(workspace)[install_target]["install_path"]).format(skill_id=skill_id)
            under_state_root = install_rel.startswith(".brigade/")
            state_parts = tuple(part for part in install_rel.split("/") if part)[1:]
            dest_present = dest_state == "dir"
            if dest_present and not force:
                print(f"error: installed skill already exists: {dest}", file=sys.stderr)
                return 2
            state_anchor.revalidate()
            receipt_name = f"{skill_id}-{install_target}.json"
            receipt_path = registry._installs_root(workspace) / receipt_name
            # Refuse redirected state paths before anything is mutated.
            registry._require_plain_state_dirs(state_anchor, "skills", "installs")
            registry._require_plain_state_dirs(state_anchor, "skills", "rollback")
            if expected_binding is not None:
                if skill != expected_binding.get("source_selector"):
                    raise registry.SkillsStatePathError("review-stale: source selector changed")
                _check_review_binding(
                    expected_binding,
                    workspace,
                    skill_id,
                    install_target,
                    anchor=state_anchor,
                    source_files=snapshot_files,
                    target_files=install_files,
                )
            # Classify again immediately before anything is written: a link
            # planted since the first check is skipped here, before any
            # rollback snapshot exists.
            recheck_state, recheck_link_target = _installed_path_state(workspace, dest, anchor=state_anchor)
            if recheck_state not in {"dir", "missing"}:
                reason = _installed_state_reason(recheck_state, recheck_link_target, dest)
                if harness != "all" and not force:
                    print(
                        f"error: installed skill changed while installing and was not replaced: {reason}",
                        file=sys.stderr,
                    )
                    return 2
                skipped.append({"target": install_target, "reason": reason})
                continue
            if recheck_state != dest_state:
                raise registry.SkillsStatePathError(f"installed copy changed while installing: {dest}; run again")
            previous_receipt = _previous_install_receipt(state_anchor, skill_id=skill_id, harness=install_target)
            checked_identity: TreeIdentity | None = None
            if dest_present:
                inspected = _inspect_installed_tree(workspace, dest, anchor=state_anchor)
                checked_identity = _tree_identity(dest, inspected)
            if dest_present and not discard_local_edits:
                # A hand-edited copy is never replaced by --force alone: the
                # receipt proves what was installed, so bytes that differ are
                # an edit. Without a receipt a manual --force (operator-driven)
                # may repair a legacy copy. This runs before any snapshot or
                # removal.
                edit_state = _local_edit_state(
                    previous_receipt, inspected.files, skill_id=skill_id, harness=install_target
                )
                if edit_state == "changed":
                    selector, against = _local_edit_selector(previous_receipt, skill_id=skill_id, fallback=skill)
                    reason = _local_edit_reason(
                        installed_dir=dest,
                        diff_command=_local_edit_diff_command(
                            workspace=workspace, selector=selector, harness=install_target, against=against
                        ),
                        discard_command=_local_edit_discard_command(
                            workspace=workspace, selector=selector, harness=install_target
                        ),
                    )
                    if harness != "all":
                        print(f"error: {reason}", file=sys.stderr)
                        return 2
                    skipped.append({"target": install_target, "reason": reason, "local_edit": True})
                    continue
            rollback_snapshot: str | None = None
            rollback_snapshot_fingerprint: str | None = None
            if dest_present:
                stamp = _now().replace(":", "").replace("+", "Z").replace(".", "-")
                rollback_dir = registry._rollback_root(workspace, skill_id, install_target) / stamp
                # State-backed destinations are snapshotted through the held
                # anchor. Existence uses _state_entry_kind so a parent
                # directory symlink is refused before dest.exists() can
                # follow it; a non-plain subtree is not copied into rollback.
                if under_state_root:
                    if dest_state == "dir" and _anchor_subtree_is_plain(state_anchor, *state_parts):
                        replaced_dirs, replaced_files = _installed_tree_snapshot(workspace, dest, anchor=state_anchor)
                    else:
                        replaced_dirs, replaced_files = [], {}
                else:
                    replaced_dirs, replaced_files = registry._collect_source_tree(dest)
                if expected_binding is not None:
                    _check_review_binding(
                        expected_binding,
                        workspace,
                        skill_id,
                        install_target,
                        anchor=state_anchor,
                        source_files=snapshot_files,
                        installed_files=replaced_files,
                        target_files=install_files,
                    )
                registry._write_collected_tree_into_anchor(
                    replaced_dirs, replaced_files, state_anchor, "skills", "rollback", skill_id, install_target, stamp
                )
                rollback_snapshot = str(rollback_dir)
                rollback_snapshot_fingerprint = registry._files_fingerprint(replaced_files)
                try:
                    if expected_binding is not None:
                        _check_review_binding(
                            expected_binding, workspace, skill_id, install_target, anchor=state_anchor
                        )
                    # Bind the removal to the bytes the edit check and snapshot saw.
                    _require_tree_unchanged(checked_identity, workspace, dest, anchor=state_anchor)
                    if not under_state_root:
                        _remove_plain_install_dir(dest)
                except registry.SkillsStatePathError:
                    # Nothing was replaced yet, so the snapshot just written
                    # must not outlive the refusal.
                    with contextlib.suppress(registry.SkillsStatePathError, OSError):
                        registry._remove_tree_in_anchor(
                            state_anchor, "skills", "rollback", skill_id, install_target, stamp
                        )
                    raise
                if under_state_root:
                    registry._remove_tree_in_anchor(state_anchor, *state_parts)
            if under_state_root:
                registry._write_collected_tree_into_anchor(snapshot_dirs, install_files, state_anchor, *state_parts)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                registry._write_snapshot_tree(snapshot_dirs, install_files, dest)
            # Fingerprints are computed from the same bytes that were written;
            # nothing is read back through a pathname.
            installed_fingerprint = registry._files_fingerprint(install_files)
            installed_skill_fingerprint = render_fingerprint
            installed_metadata_fingerprint = registry._bytes_fingerprint(install_files.get(("skill.json",)))
            installed_at = _now()
            receipt = {
                "schema_version": registry.RECEIPT_SCHEMA_VERSION,
                "workspace": str(workspace),
                "receipt_id": f"{installed_at[:19].replace(':', '').replace('-', '')}-{skill_id}-{install_target}",
                "skill_id": skill_id,
                "target": install_target,
                "installed_dir": str(dest),
                "installed_at": installed_at,
                "version": version,
                "source_path": source_path,
                "source": source_identity,
                "render": {**render_contract, "fingerprint": render_fingerprint},
                "installed": {
                    "bundle_fingerprint": installed_fingerprint,
                    "edit_fingerprint": registry._edit_fingerprint(install_files),
                    "skill_fingerprint": installed_skill_fingerprint,
                    "metadata_fingerprint": installed_metadata_fingerprint,
                },
                "fingerprint": lint_payload.get("fingerprint"),
                "source_fingerprint": lint_payload.get("fingerprint"),
                "render_fingerprint": render_fingerprint,
                "installed_fingerprint": installed_fingerprint,
                "format": _adapter_map(workspace)[install_target].get("format"),
                "rollback_snapshot": rollback_snapshot,
                "rollback_snapshot_fingerprint": rollback_snapshot_fingerprint,
                "previous_receipt": previous_receipt if previous_receipt else None,
                "trust_score": lint_payload.get("trust_score"),
                "changelog": lint_payload.get("changelog"),
            }
            receipt["receipt_path"] = str(receipt_path)
            history_receipt = dict(receipt)
            history_receipt["receipt_path"] = str(receipt_path)
            # The canonical receipt file keeps its original schema (no
            # receipt_path key); the single anchored write below replaces the
            # former raw path-based write plus anchored rewrite pair.
            canonical_receipt = {key: value for key, value in receipt.items() if key != "receipt_path"}
            registry._write_state_file(
                state_anchor, "skills", "installs", receipt_name, data=registry._json_bytes(canonical_receipt)
            )
            registry._append_state_line(
                state_anchor,
                "skills",
                "installs",
                "history.jsonl",
                data=(json.dumps(history_receipt, sort_keys=True) + "\n").encode("utf-8"),
            )
            receipts.append(receipt)
    except (registry.SkillsStatePathError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        state_anchor.close()
    receipt = {
        "workspace": str(workspace),
        "skill_id": skill_id,
        "target": harness,
        "installed_at": _now(),
        "fingerprint": lint_payload.get("fingerprint"),
        "source": lint_payload.get("source"),
        "targets": list(targets),
        "receipts": receipts,
        "skipped": skipped,
    }
    # Honest result: nothing is reported installed when every target was skipped.
    payload = {"installed": bool(receipts), "receipt": receipt}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_install: {skill_id}")
    print(f"target: {harness}")
    for item in receipts:
        print(f"- {item['target']}: {item['installed_dir']}")
        print(f"  receipt: {item['receipt_path']}")
    for item in skipped:
        print(f"- {item['target']}: skipped ({item['reason']})")
    if not receipts:
        print("installed: no (every target was skipped)")
    return 0


@dataclass(frozen=True)
class UserProfileSkillPackage:
    """A reviewed, supported skill package materialized as in-memory bytes.

    ``files`` maps POSIX relative paths to file bytes. ``SKILL.md`` is replaced
    with harness-rendered UTF-8 bytes; every other file is copied byte-for-byte
    from the registry skill directory. No directories are created.
    """

    skill_id: str
    source_identity: str
    source_fingerprint: str
    metadata_fingerprint: str
    files: dict[str, bytes]


_USER_PROFILE_MAX_FILES = 512
_USER_PROFILE_MAX_BYTES = 8 * 1024 * 1024
_USER_PROFILE_HARNESSES = {"claude", "codex", "openclaw", "kimi", "grok", "cursor", "opencode"}


def _snapshot_package_files(snapshot_files: dict[tuple[str, ...], bytes]) -> dict[str, bytes] | None:
    """Package-file view of an already-collected registry snapshot.

    Returns ``None`` if the package would exceed the file-count or byte cap,
    so the caller excludes the whole package rather than truncating it.
    """
    files: dict[str, bytes] = {}
    total = 0
    for parts, data in sorted(snapshot_files.items()):
        rel = Path(*parts).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        # lexical containment: reject absolute-looking keys and ".." components
        if rel.startswith("/") or ".." in Path(rel).parts:
            continue
        if len(files) >= _USER_PROFILE_MAX_FILES:
            return None
        if total + len(data) > _USER_PROFILE_MAX_BYTES:
            return None
        files[rel] = data
        total += len(data)
    return dict(sorted(files.items()))


def _user_profile_package(
    *,
    workspace: Path,
    harness: str,
    registry_row: dict[str, Any],
    minimum_trust: str,
) -> UserProfileSkillPackage | None:
    registry_metadata = registry_row["metadata"]
    skill_id = registry._slug(str(registry_metadata.get("id") or Path(str(registry_row["skill_dir"])).name))
    lint_payload = search_validate._lint_payload(workspace, f"registry:{skill_id}", harness=harness)
    if not lint_payload.get("valid"):
        return None
    metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else registry_metadata
    if metadata.get("enabled", True) is False:
        return None
    trust_score = raw_trust_score if isinstance((raw_trust_score := lint_payload.get("trust_score")), dict) else {}
    actual_trust = str(trust_score.get("trust_level") or "unreviewed")
    if not sync_history._trust_at_least(actual_trust, minimum_trust):
        return None
    supported = metadata.get("supported_harnesses")
    supported_harnesses = set(supported) if isinstance(supported, list) else set()
    if supported_harnesses and harness not in supported_harnesses:
        return None
    source = raw_source if isinstance((raw_source := lint_payload.get("source")), dict) else {}
    source_identity = source.get("identity")
    if not (isinstance(source_identity, str) and source_identity):
        return None
    source_fingerprint = lint_payload.get("fingerprint")
    if not (isinstance(source_fingerprint, str) and source_fingerprint):
        return None
    metadata_fingerprint = source.get("metadata_fingerprint")
    if not (isinstance(metadata_fingerprint, str) and metadata_fingerprint):
        return None
    # The package body comes from one anchored registry snapshot; nothing is
    # walked or re-read by pathname under the state root.
    try:
        with registry._held_state_root(workspace, writable=False) as anchor:
            _snapshot_dirs, snapshot_files = registry._read_registry_entry_tree(anchor, skill_id)
    except registry.SkillsStatePathError:
        return None
    files = _snapshot_package_files(snapshot_files)
    if files is None:
        return None
    source_text = snapshot_files.get(("SKILL.md",))
    if "SKILL.md" in files and source_text is not None:
        rendered = registry._render_skill_text_for_harness(
            source_text.decode("utf-8", errors="replace"), metadata, skill_id, harness
        )
        if registry._rendered_skill_validation(rendered, harness):
            return None
        files = {**files, "SKILL.md": rendered.encode("utf-8")}
        files = dict(sorted(files.items()))
    return UserProfileSkillPackage(
        skill_id=skill_id,
        source_identity=source_identity,
        source_fingerprint=source_fingerprint,
        metadata_fingerprint=metadata_fingerprint,
        files=files,
    )


def user_profile_skill_packages(
    *,
    workspace: Path,
    harness: str,
    minimum_trust: str = "workspace",
) -> tuple[UserProfileSkillPackage, ...]:
    """Reviewed, supported skill packages for a native user-profile harness.

    Iterates the registry, runs lint + render validation through the existing
    helpers, and returns whole in-memory packages (regular files only, SKILL.md
    rendered for the harness). Excludes disabled, untrusted, unsupported,
    lint-invalid, render-invalid, and over-cap packages. Creates no directories
    and never calls ``_install_dir()``.
    """
    workspace = workspace.expanduser().resolve()
    if harness not in _USER_PROFILE_HARNESSES:
        raise ValueError(f"unknown harness adapter: {harness}")
    if minimum_trust not in registry.TRUST_LEVELS:
        raise ValueError(f"unknown skill trust level: {minimum_trust}")
    packages: list[UserProfileSkillPackage] = []
    for registry_row in registry._iter_registry(workspace):
        package = _user_profile_package(
            workspace=workspace,
            harness=harness,
            registry_row=registry_row,
            minimum_trust=minimum_trust,
        )
        if package is not None:
            packages.append(package)
    return tuple(sorted(packages, key=lambda package: package.skill_id))
