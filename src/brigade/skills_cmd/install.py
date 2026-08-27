"""Install commands and user-profile skill packages."""
# ruff: noqa: F401

from __future__ import annotations

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
from .. import mcp_server
from ..localio import slugify, utc_now_iso as _now
from ..projection import kernel as projection
from ..templates import template_root
from ..untrusted import scan_untrusted

from . import registry
from . import search_validate
from . import sync_history
from . import packs as _packs_mod


def _evaluate_install_dir(workspace: Path, harness: str, skill_id: str) -> tuple[Path, bool]:
    """Return ``(projected_install_dir, escapes_workspace)`` without raising.

    Read-only callers (fleet status) use this to describe a copy that resolves
    outside the workspace instead of treating it as a writable destination.
    """
    if harness == "hermes":
        # Real Hermes reads skills from its own data dir (auto-discovered as a
        # local skill), not the repo. Install there so it actually takes effect.
        return registry._hermes_skills_root() / registry._slug(skill_id), False
    adapter = _adapter_map(workspace)[harness]
    install_dir = workspace / str(adapter["install_path"]).format(skill_id=skill_id)
    workspace_resolved = workspace.resolve()
    resolved = install_dir.resolve()
    escapes = (
        ".." in install_dir.parts or resolved == workspace_resolved or not resolved.is_relative_to(workspace_resolved)
    )
    return install_dir, escapes


def _install_dir(workspace: Path, harness: str, skill_id: str) -> Path:
    install_dir, escapes = _evaluate_install_dir(workspace, harness, skill_id)
    if escapes:
        raise ValueError(f"skill install path escapes workspace: {install_dir}")
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
        with registry._held_state_root(target) as state_anchor:
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
        with registry._held_state_root(target) as state_anchor:
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
    for name in os.listdir(fd):
        try:
            st = os.lstat(name, dir_fd=fd)
        except OSError:
            return False
        if stat_module.S_ISLNK(st.st_mode):
            return False
        if stat_module.S_ISDIR(st.st_mode):
            try:
                child = os.open(name, registry._DIR_OPEN_FLAGS, dir_fd=fd)
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
        return True
    try:
        fd, opened = registry._anchor_open_chain(anchor, *relative, missing_ok=True)
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


def _installed_tree_snapshot(
    workspace: Path, installed_dir: Path, anchor: registry._StateRootAnchor | None = None
) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
    """Collect an installed copy's plain files for inspection.

    Copies located inside ``.brigade`` (e.g. the built-in ``mcp`` target's
    ``mcp-resources``) are state-root content: they are read through the held
    state-root descriptor, so a planted symlink is skipped or refuses instead
    of dragging outside bytes into diffs, drift, or rollback material.
    Trusted harness directories outside the state root keep the plain
    descriptor walk.
    """
    parts = _packs_mod._lexical_state_root_parts(workspace, installed_dir)
    if parts is None:
        if not installed_dir.is_dir():
            return [], {}
        return registry._collect_source_tree(installed_dir)

    def _collect(held: registry._StateRootAnchor) -> tuple[list[tuple[str, ...]], dict[tuple[str, ...], bytes]]:
        if registry._state_entry_kind(held, *parts) != "dir":
            return [], {}
        return registry._read_tree_from_anchor(held, *parts)

    if anchor is not None:
        return _collect(anchor)
    try:
        with registry._held_state_root(workspace) as fresh_anchor:
            return _collect(fresh_anchor)
    except registry.SkillsStatePathError:
        # A redirected or planted component contributes nothing rather than
        # being followed; callers report absence instead of outside content.
        return [], {}


def _drift_payload(
    *,
    target: Path,
    skill_id: str,
    harness: str,
    lint_payload: dict[str, Any],
    rendered: str,
    installed_dir: Path,
) -> dict[str, Any]:
    receipt = _latest_install_receipt(target, skill_id, harness)
    snapshot_dirs, snapshot_files = _installed_tree_snapshot(target, installed_dir)
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
    if not installed_present and receipt and not known:
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
        receipt_installed = receipt["installed"]
        source_state = (
            "current"
            if receipt_source.get("identity") == current_source.get("identity")
            and receipt_source.get("fingerprint") == current_source.get("fingerprint")
            and receipt_source.get("metadata_fingerprint") == current_source.get("metadata_fingerprint")
            else "changed"
        )
        render_state = "current" if receipt_render.get("identity") == current_render.get("identity") else "changed"
        local_state = (
            "current"
            if receipt_installed.get("bundle_fingerprint") == installed_bundle_fingerprint
            and receipt_installed.get("skill_fingerprint") == installed_skill_fingerprint
            and receipt_installed.get("metadata_fingerprint") == installed_metadata_fingerprint
            else "changed"
        )
        overall = "changed" if "changed" in {source_state, render_state, local_state} else "current"
    return {
        "overall": overall,
        "source": source_state,
        "render": render_state,
        "local_edit": local_state,
        "receipt_known": known,
        "content_changed": installed_text != rendered,
        "installed": installed_present,
        "installed_skill_fingerprint": installed_skill_fingerprint,
        "installed_bundle_fingerprint": installed_bundle_fingerprint,
        "installed_metadata_fingerprint": installed_metadata_fingerprint,
        "current_source": current_source,
        "current_render": {**current_render, "fingerprint": current_render_fingerprint},
        "receipt": receipt or None,
    }


def install(
    *,
    workspace: Path,
    skill: str,
    harness: str,
    force: bool = False,
    json_output: bool = False,
) -> int:
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
                skipped.append({"target": "hermes", "reason": "Hermes home not found (is Hermes installed?)"})
                continue
            dest = _install_dir(workspace, install_target, skill_id)
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
            if dest.exists() and not force:
                print(f"error: installed skill already exists: {dest}", file=sys.stderr)
                return 2
            state_anchor.revalidate()
            receipt_name = f"{skill_id}-{install_target}.json"
            receipt_path = registry._installs_root(workspace) / receipt_name
            install_rel = str(_adapter_map(workspace)[install_target]["install_path"]).format(skill_id=skill_id)
            under_state_root = install_rel.startswith(".brigade/")
            state_parts = tuple(part for part in install_rel.split("/") if part)[1:]
            # Refuse redirected state paths before anything is mutated.
            registry._require_plain_state_dirs(state_anchor, "skills", "installs")
            registry._require_plain_state_dirs(state_anchor, "skills", "rollback")
            previous_receipt = _previous_install_receipt(state_anchor, skill_id=skill_id, harness=install_target)
            rollback_snapshot: str | None = None
            rollback_snapshot_fingerprint: str | None = None
            if dest.exists():
                stamp = _now().replace(":", "").replace("+", "Z").replace(".", "-")
                rollback_dir = registry._rollback_root(workspace, skill_id, install_target) / stamp
                # Collect the about-to-be-replaced copy once and materialize
                # the snapshot from those bytes; the snapshot fingerprint is
                # recorded in the new receipt so rollback can prove the
                # snapshot it restores is byte-identical to what was captured.
                # dest is normally a trusted harness dir; for custom adapters
                # installing under .brigade it is state-root content captured
                # to the rollback bar (accepted residual, see docstring).
                replaced_dirs, replaced_files = registry._collect_source_tree(dest)
                registry._write_collected_tree_into_anchor(
                    replaced_dirs, replaced_files, state_anchor, "skills", "rollback", skill_id, install_target, stamp
                )
                rollback_snapshot = str(rollback_dir)
                rollback_snapshot_fingerprint = registry._files_fingerprint(replaced_files)
                if under_state_root:
                    registry._remove_tree_in_anchor(state_anchor, *state_parts)
                else:
                    shutil.rmtree(dest)
            install_files = dict(snapshot_files)
            install_files[("SKILL.md",)] = rendered_text.encode("utf-8")
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
    except registry.SkillsStatePathError as exc:
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
    payload = {"installed": True, "receipt": receipt}
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
        with registry._held_state_root(workspace) as anchor:
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
