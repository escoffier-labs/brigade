"""Sync, uninstall, rollback, history, and diff commands."""
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
from typing import TYPE_CHECKING, Any, Iterator, Literal, cast

from .. import __version__ as BRIGADE_VERSION
from .. import mcp_server
from ..localio import slugify, utc_now_iso as _now
from ..projection import kernel as projection
from ..templates import template_root
from ..untrusted import scan_untrusted

from . import registry as _registry_mod
from . import search_validate
from . import packs as _packs_mod

if TYPE_CHECKING:
    import brigade.skills_cmd.install as _install_mod
else:
    from . import install as _install_mod


def _trust_at_least(actual: str, minimum: str) -> bool:
    try:
        return _registry_mod.TRUST_LEVELS.index(actual) >= _registry_mod.TRUST_LEVELS.index(minimum)
    except ValueError:
        return False


def _sync_plan(*, workspace: Path, harness: str, trust: str) -> tuple[list[dict[str, Any]], str | None]:
    install_targets = _install_mod._install_targets(workspace)
    if harness not in (*install_targets, "all"):
        return [], f"unknown skill install target: {harness}"
    if trust not in _registry_mod.TRUST_LEVELS:
        return [], f"unknown skill trust level: {trust}"
    targets = install_targets if harness == "all" else (harness,)
    items: list[dict[str, Any]] = []
    registry = _registry_mod._iter_registry(workspace)
    for registry_row in registry:
        registry_metadata = registry_row["metadata"]
        skill_id = _registry_mod._slug(str(registry_metadata.get("id") or Path(str(registry_row["skill_dir"])).name))
        lint_payload = search_validate._lint_payload(workspace, f"registry:{skill_id}")
        metadata = raw if isinstance((raw := lint_payload.get("metadata")), dict) else registry_metadata
        trust_score = raw_trust_score if isinstance((raw_trust_score := lint_payload.get("trust_score")), dict) else {}
        actual_trust = str(trust_score.get("trust_level") or "unreviewed")
        supported = metadata.get("supported_harnesses")
        supported_harnesses = set(supported) if isinstance(supported, list) else set()
        source = raw_source if isinstance((raw_source := lint_payload.get("source")), dict) else {}
        for install_target in targets:
            item: dict[str, Any] = {
                "skill_id": skill_id,
                "harness": install_target,
                "source": source,
                "trust_level": actual_trust,
                "minimum_trust": trust,
                "state": "blocked",
                "action": "none",
                "result": "blocked",
                "reason": None,
                "receipt": None,
                "error": None,
            }
            if not lint_payload.get("valid"):
                errors = raw_errors if isinstance((raw_errors := lint_payload.get("errors")), list) else []
                item["reason"] = "; ".join(str(error) for error in errors) or "skill lint failed"
                items.append(item)
                continue
            if metadata.get("enabled", True) is False:
                item.update(
                    state="excluded",
                    result="excluded",
                    reason="skill is disabled by registry metadata",
                )
                items.append(item)
                continue
            if not _trust_at_least(actual_trust, trust):
                item.update(
                    state="excluded",
                    result="excluded",
                    reason=f"trust level {actual_trust} is below {trust}",
                )
                items.append(item)
                continue
            if supported_harnesses and install_target not in supported_harnesses:
                item.update(
                    state="excluded",
                    result="excluded",
                    reason=f"harness {install_target} is not supported by registry metadata",
                )
                items.append(item)
                continue
            if install_target == "hermes" and not _registry_mod._hermes_home().exists():
                item.update(
                    state="excluded",
                    result="excluded",
                    reason="Hermes home not found (is Hermes installed?)",
                )
                items.append(item)
                continue
            # Registry rendering text comes from the anchored read; a refused
            # entry contributes no text and the render below fails validation.
            source_text = _registry_mod._anchored_registry_text(workspace, skill_id, "SKILL.md") or ""
            rendered = _registry_mod._render_skill_text_for_harness(source_text, metadata, skill_id, install_target)
            render_errors = _registry_mod._rendered_skill_validation(rendered, install_target)
            if render_errors:
                item["reason"] = "; ".join(render_errors)
                items.append(item)
                continue
            installed_dir = _install_mod._install_dir(workspace, install_target, skill_id)
            drift = _install_mod._drift_payload(
                target=workspace,
                skill_id=skill_id,
                harness=install_target,
                lint_payload=lint_payload,
                rendered=rendered,
                installed_dir=installed_dir,
            )
            item["drift"] = {
                key: drift[key]
                for key in ("overall", "source", "render", "local_edit", "receipt_known", "content_changed")
            }
            if drift["overall"] == "current":
                item.update(state="current", result="unchanged")
            elif drift["overall"] == "missing":
                item.update(state="missing", action="install", result="planned")
            elif drift["overall"] == "changed":
                item.update(state="changed", action="update", result="planned")
            else:
                item["reason"] = "installed copy has no valid provenance receipt"
            items.append(item)
    return items, None


def _sync_file_entries(root: Path) -> dict[str, tuple[bytes, int]]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sync_rendered_files(
    snapshot_files: dict[tuple[str, ...], bytes], metadata: dict[str, Any], skill_id: str, harness: str
) -> dict[str, tuple[bytes, int]]:
    """Build the desired install bundle from an already-collected snapshot.

    Every byte comes from the snapshot; the registry source is never re-read
    by pathname after validation.
    """
    files = {Path(*parts).as_posix(): (data, 0o644) for parts, data in sorted(snapshot_files.items())}
    skill = snapshot_files.get(("SKILL.md",))
    if skill is not None:
        rendered = _registry_mod._render_skill_text_for_harness(
            skill.decode("utf-8", errors="replace"), metadata, skill_id, harness
        )
        files["SKILL.md"] = (rendered.encode("utf-8"), 0o644)
    return files


def _sync_files_fingerprint(files: dict[str, tuple[bytes, int]]) -> str:
    digest = hashlib.sha256()
    for relative, (content, _mode) in sorted(files.items()):
        if Path(relative).name in {".DS_Store", "skill.json"}:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _sync_file_mutations(
    *,
    root: Path,
    before: dict[str, tuple[bytes, int]],
    after: dict[str, tuple[bytes, int]],
    display_prefix: str,
) -> list[projection.MutationSpec]:
    mutations: list[projection.MutationSpec] = []
    for relative in sorted(set(before) | set(after)):
        old = before.get(relative)
        new = after.get(relative)
        if old == new:
            continue
        destination = root / relative
        mutation_type = "create" if old is None else "remove" if new is None else "replace"
        mutations.append(
            projection.mutation(
                destination=destination,
                display_path=f"{display_prefix}/{relative}",
                mutation=cast(Literal["create", "replace", "remove"], mutation_type),
                expected_before=projection.content_digest(old[0] if old else None),
                desired_after=projection.content_digest(new[0] if new else None),
                staged_bytes=new[0] if new else None,
                mode=new[1] if new else None,
            )
        )
    return mutations


def _sync_single_file_mutation(path: Path, before: bytes | None, after: bytes | None) -> projection.MutationSpec | None:
    if before == after:
        return None
    mutation_type = "create" if before is None else "remove" if after is None else "replace"
    return projection.mutation(
        destination=path,
        display_path=projection.safe_display_path(path, target=path.parent),
        mutation=cast(Literal["create", "replace", "remove"], mutation_type),
        expected_before=projection.content_digest(before),
        desired_after=projection.content_digest(after),
        staged_bytes=after,
    )


def _sync_recovery_records(workspace: Path) -> list[dict[str, str]]:
    return [
        {
            "operation_id": record.operation_id,
            "status": record.status,
            "recovery_command": f"brigade projection recover {record.operation_id}",
        }
        for record in projection.unfinished_operations(workspace)
        if record.operation_id.startswith("skills-")
    ]


def _sync_projection_plan(
    *, workspace: Path, items: list[dict[str, Any]]
) -> tuple[projection.Plan | None, list[dict[str, Any]], str | None]:
    """Plan sync mutations from one anchored snapshot per registry entry.

    Each entry's bytes are collected through the held state-root descriptor
    before linting; rendering, fingerprints, receipts, and every mutation
    derive only from those collected bytes. History and prior receipts are
    read through ``_read_state_file_bytes``, so a symlinked state file refuses
    the plan instead of being followed.
    """
    mutations: list[projection.MutationSpec] = []
    history_path = _registry_mod._install_history_path(workspace)
    history_lines: list[bytes] = []
    source_rows: list[dict[str, Any]] = []
    with _registry_mod._held_state_root(workspace) as state_anchor:
        history_before = _registry_mod._read_state_file_bytes(state_anchor, "skills", "installs", "history.jsonl")
        for item in items:
            skill_id = str(item["skill_id"])
            harness = str(item["harness"])
            # Collect exactly once, then validate the collected generation:
            # a source swapped after this point cannot change what is installed.
            snapshot_dirs, snapshot_files = _registry_mod._read_registry_entry_tree(state_anchor, skill_id)
            with tempfile.TemporaryDirectory(prefix="brigade-sync-lint-") as staging:
                staged_dir = Path(staging) / skill_id
                _registry_mod._write_snapshot_tree(snapshot_dirs, snapshot_files, staged_dir)
                lint = search_validate._lint_payload(Path(staging), str(staged_dir))
            search_validate._finalize_staged_registry_lint(
                lint, workspace, _registry_mod._skill_path(workspace, skill_id), staged_dir
            )
            if not lint.get("valid"):
                return None, items, f"skill lint failed during sync planning: {skill_id}"
            metadata = raw_metadata if isinstance((raw_metadata := lint.get("metadata")), dict) else {}
            # Every eligibility gate repeats on THIS snapshot's generation:
            # planning evaluated an earlier registry read, so a generation
            # swapped in between must be re-checked here, never installed on
            # the strength of the plan-phase decision.
            trust_score = raw_trust_score if isinstance((raw_trust_score := lint.get("trust_score")), dict) else {}
            actual_trust = str(trust_score.get("trust_level") or "unreviewed")
            minimum_trust = str(item.get("minimum_trust") or "unreviewed")
            supported = metadata.get("supported_harnesses")
            supported_harnesses = set(supported) if isinstance(supported, list) else set()
            enabled = metadata.get("enabled", True)
            ineligible_reason: str | None = None
            if enabled is False:
                ineligible_reason = "skill is disabled by registry metadata"
            elif not _trust_at_least(actual_trust, minimum_trust):
                ineligible_reason = f"trust level {actual_trust} is below {minimum_trust}"
            elif supported_harnesses and harness not in supported_harnesses:
                ineligible_reason = f"harness {harness} is not supported by registry metadata"
            if ineligible_reason is not None:
                item.update(state="excluded", action="none", result="excluded", reason=ineligible_reason)
                item["receipt"] = None
                continue
            desired_files = _sync_rendered_files(snapshot_files, metadata, skill_id, harness)
            installed_dir = _install_mod._install_dir(workspace, harness, skill_id)
            installed_parts = _packs_mod._lexical_state_root_parts(workspace, installed_dir)
            if installed_parts is not None:
                # State-backed install destinations are inspected through the
                # same held anchor: rollback material derives from collected
                # bytes, never from a pathname walk that could follow a
                # planted symlink out of the state root. A destination holding
                # non-plain entries is refused for this run instead of being
                # written over or around.
                if not _install_mod._anchor_subtree_is_plain(state_anchor, *installed_parts):
                    item.update(
                        state="excluded",
                        action="none",
                        result="excluded",
                        reason="installed state copy contains non-plain entries; remove it before syncing",
                    )
                    item["receipt"] = None
                    continue
                _previous_dirs, previous_snapshot = _install_mod._installed_tree_snapshot(
                    workspace, installed_dir, anchor=state_anchor
                )
                previous_files = {
                    Path(*parts).as_posix(): (data, 0o644) for parts, data in sorted(previous_snapshot.items())
                }
            else:
                previous_files = _sync_file_entries(installed_dir)
            mutations.extend(
                _sync_file_mutations(
                    root=installed_dir,
                    before=previous_files,
                    after=desired_files,
                    display_prefix=f"bundle:{skill_id}:{harness}",
                )
            )
            installed_at = _now()
            rollback_snapshot: str | None = None
            if previous_files:
                rollback_dir = (
                    _registry_mod._rollback_root(workspace, skill_id, harness)
                    / f"{installed_at[:19].replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
                )
                rollback_snapshot = str(rollback_dir)
                mutations.extend(
                    _sync_file_mutations(
                        root=rollback_dir,
                        before={},
                        after=previous_files,
                        display_prefix=f"rollback:{skill_id}:{harness}",
                    )
                )
            source = raw_source if isinstance((raw_source := lint.get("source")), dict) else {}
            render = _install_mod._renderer_contract(workspace, harness)
            skill_content = desired_files.get("SKILL.md", (b"", 0))[0].decode("utf-8", errors="replace")
            receipt_path = _registry_mod._installs_root(workspace) / f"{skill_id}-{harness}.json"
            receipt_before = _registry_mod._read_state_file_bytes(
                state_anchor, "skills", "installs", f"{skill_id}-{harness}.json"
            )
            previous_receipt: dict[str, Any] = {}
            if receipt_before is not None:
                try:
                    parsed = json.loads(receipt_before.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = None
                if isinstance(parsed, dict):
                    previous_receipt = parsed
            receipt = {
                "schema_version": _registry_mod.RECEIPT_SCHEMA_VERSION,
                "workspace": str(workspace),
                "receipt_id": f"{installed_at[:19].replace(':', '').replace('-', '')}-{skill_id}-{harness}",
                "skill_id": skill_id,
                "target": harness,
                "installed_dir": str(installed_dir),
                "installed_at": installed_at,
                "version": str(metadata.get("version") or "0.1.0"),
                "source_path": str(metadata.get("source") or _registry_mod._skill_path(workspace, skill_id)),
                "source": source,
                "render": {**render, "fingerprint": _registry_mod._text_fingerprint(skill_content)},
                "installed": {
                    "bundle_fingerprint": _sync_files_fingerprint(desired_files),
                    "skill_fingerprint": _registry_mod._text_fingerprint(skill_content),
                    "metadata_fingerprint": _registry_mod._text_fingerprint(
                        desired_files.get("skill.json", (b"", 0))[0].decode("utf-8", errors="replace")
                    ),
                },
                "fingerprint": lint.get("fingerprint"),
                "source_fingerprint": lint.get("fingerprint"),
                "render_fingerprint": _registry_mod._text_fingerprint(skill_content),
                "installed_fingerprint": _sync_files_fingerprint(desired_files),
                "format": _install_mod._adapter_map(workspace)[harness].get("format"),
                "rollback_snapshot": rollback_snapshot,
                "rollback_snapshot_fingerprint": (
                    _registry_mod._files_fingerprint(
                        {tuple(Path(relative).parts): data for relative, (data, _mode) in previous_files.items()}
                    )
                    if rollback_snapshot
                    else None
                ),
                "previous_receipt": previous_receipt if previous_receipt else None,
                "trust_score": lint.get("trust_score"),
                "changelog": lint.get("changelog"),
            }
            receipt_after = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
            receipt_mutation = _sync_single_file_mutation(receipt_path, receipt_before, receipt_after)
            if receipt_mutation is not None:
                mutations.append(receipt_mutation)
            item["receipt"] = {**receipt, "receipt_path": str(receipt_path)}
            history_lines.append((json.dumps(item["receipt"], sort_keys=True) + "\n").encode("utf-8"))
            source_rows.append({"skill_id": skill_id, "harness": harness, "fingerprint": lint.get("fingerprint")})
    history_after = (history_before or b"") + b"".join(history_lines)
    history_mutation = _sync_single_file_mutation(history_path, history_before, history_after)
    if history_mutation is not None:
        mutations.append(history_mutation)
    if not mutations:
        return None, items, None
    source_fingerprint = hashlib.sha256(json.dumps(source_rows, sort_keys=True).encode("utf-8")).hexdigest()
    return (
        projection.build_plan(
            operation_id=f"skills-{uuid.uuid4().hex}",
            projector="skills",
            source_fingerprint=source_fingerprint,
            mutations=mutations,
            target=workspace,
        ),
        items,
        None,
    )


def sync(
    *,
    workspace: Path,
    harness: str,
    trust: str = "workspace",
    write: bool = False,
    json_output: bool = False,
) -> int:
    """Reconcile every eligible registry skill against one or all harness targets."""
    workspace = workspace.expanduser().resolve()
    items, error = _sync_plan(workspace=workspace, harness=harness, trust=trust)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 2

    applied = {"installed": 0, "updated": 0, "restored": 0, "failed": 0}
    projection_view: dict[str, Any] | None = None
    if write:
        mutable_items = [item for item in items if item["action"] in {"install", "update"}]
        if mutable_items:
            recovery = _sync_recovery_records(workspace)
            if recovery:
                blocked = {"workspace": str(workspace), "recovery": recovery, "terminal_state": "recovery-required"}
                print(
                    json.dumps(blocked, indent=2, sort_keys=True)
                    if json_output
                    else f"error: recovery required: {recovery[0]['recovery_command']}"
                )
                return 1
            try:
                planned, mutable_items, error = _sync_projection_plan(workspace=workspace, items=mutable_items)
            except (OSError, projection.PlanError, ValueError, _registry_mod.SkillsStatePathError) as exc:
                planned, error = None, str(exc)
            if error is not None:
                print(f"error: {error}", file=sys.stderr)
                return 1
            if planned is not None:
                try:
                    receipt = projection.execute(planned, target=workspace)
                except projection.ProjectionError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                projection_view = receipt.to_dict()
                if receipt.terminal_state == "committed":
                    for item in mutable_items:
                        if item["action"] not in {"install", "update"}:
                            # The projection pass re-gated eligibility on its
                            # own snapshot and excluded this item.
                            continue
                        result = "installed" if item["action"] == "install" else "updated"
                        item["result"] = result
                        applied[result] += 1
                else:
                    state = "restored" if receipt.terminal_state == "restored" else "recovery-required"
                    for item in mutable_items:
                        item["result"] = state
                    applied["restored"] = len(mutable_items) if state == "restored" else 0
                    applied["failed"] = 1

    counts = {
        state: sum(item["state"] == state for item in items)
        for state in ("current", "missing", "changed", "blocked", "excluded")
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "workspace": str(workspace),
        "target": harness,
        "minimum_trust": trust,
        "write": write,
        "registry_count": len({str(item["skill_id"]) for item in items}),
        "item_count": len(items),
        "counts": counts,
        "applied": applied,
        "items": items,
        "projection": projection_view,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        mode = "write" if write else "dry-run"
        print(f"skills sync: {workspace}")
        print(f"mode: {mode}")
        print(f"target: {harness}")
        print(f"minimum_trust: {trust}")
        if projection_view is not None:
            print(f"projection operation: {projection_view['operation_id']}")
            print(f"projection status: {projection_view['terminal_state']}")
        print(
            " ".join(f"{state}={counts[state]}" for state in ("current", "missing", "changed", "blocked", "excluded"))
        )
        for item in items:
            detail_value = item.get("error") or item.get("reason")
            detail = f" ({detail_value})" if detail_value else ""
            print(
                f"- {item['skill_id']} [{item['harness']}] {item['state']} "
                f"action={item['action']} result={item['result']}{detail}"
            )
    return (
        1
        if applied["failed"] or (projection_view is not None and projection_view["terminal_state"] != "committed")
        else 0
    )


def uninstall(*, workspace: Path, skill: str, harness: str, json_output: bool = False) -> int:
    """Remove an installed skill from one or all harnesses, the inverse of install."""
    workspace = workspace.expanduser().resolve()
    install_targets = _install_mod._install_targets(workspace)
    if harness not in (*install_targets, "all"):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    skill_id = _registry_mod._slug(skill)
    targets = install_targets if harness == "all" else (harness,)
    removed: list[dict[str, Any]] = []
    try:
        state_anchor = _registry_mod._hold_state_root(workspace)
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        for install_target in targets:
            state_anchor.revalidate()
            try:
                dest = _install_mod._install_dir(workspace, install_target, skill_id)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            receipt_name = f"{skill_id}-{install_target}.json"
            install_rel = str(_install_mod._adapter_map(workspace)[install_target]["install_path"]).format(
                skill_id=skill_id
            )
            under_state_root = install_rel.startswith(".brigade/")
            state_parts = tuple(part for part in install_rel.split("/") if part)[1:]
            # Refuse redirected state paths before anything is deleted.
            _registry_mod._require_plain_state_dirs(state_anchor, "skills", "installs")
            has_receipt = _registry_mod._state_file_exists(state_anchor, "skills", "installs", receipt_name)
            if not dest.exists() and not has_receipt:
                continue
            if dest.exists():
                if under_state_root:
                    _registry_mod._remove_tree_in_anchor(state_anchor, *state_parts)
                else:
                    shutil.rmtree(dest)
            if has_receipt:
                _registry_mod._unlink_in_anchor(state_anchor, "skills", "installs", receipt_name)
            _registry_mod._append_state_line(
                state_anchor,
                "skills",
                "installs",
                "history.jsonl",
                data=(
                    json.dumps(
                        {
                            "action": "uninstall",
                            "skill_id": skill_id,
                            "harness": install_target,
                            "workspace": str(workspace),
                            "uninstalled_at": _now(),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            removed.append({"harness": install_target, "path": str(dest)})
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        state_anchor.close()
    if not removed:
        print(f"error: skill not installed: {skill} ({harness})", file=sys.stderr)
        return 1
    payload = {"workspace": str(workspace), "skill_id": skill_id, "uninstalled": removed, "count": len(removed)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skills uninstall: {skill_id}")
    for record in removed:
        print(f"removed: {record['harness']} -> {record['path']}")
    return 0


def rollback(*, workspace: Path, skill: str, harness: str, json_output: bool = False) -> int:
    workspace = workspace.expanduser().resolve()
    skill_id = _registry_mod._slug(skill)
    if harness not in _install_mod._install_targets(workspace):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    receipt_name = f"{skill_id}-{harness}.json"
    rollback_receipt_name = f"{skill_id}-{harness}-rollback.json"
    rollback_receipt_path = workspace / ".brigade" / "skills" / "installs" / rollback_receipt_name
    try:
        with _registry_mod._held_state_root(workspace) as state_anchor:
            # Refuse redirected installs and rollback components before any
            # receipt is read or anything is copied, written, or deleted.
            _registry_mod._require_plain_state_dirs(state_anchor, "skills", "installs")
            receipt_bytes = _registry_mod._read_state_file_bytes(state_anchor, "skills", "installs", receipt_name)
            current_receipt: dict[str, Any] = {}
            if receipt_bytes is not None:
                try:
                    parsed = json.loads(receipt_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = None
                if isinstance(parsed, dict):
                    current_receipt = parsed
            if not _install_mod._valid_receipt_contract(current_receipt, skill_id=skill_id, harness=harness):
                print(f"error: invalid rollback receipt for {skill_id} on {harness}", file=sys.stderr)
                return 1
            snapshot_value = current_receipt.get("rollback_snapshot")
            if not isinstance(snapshot_value, str) or not snapshot_value:
                print(f"error: no receipt-bound rollback snapshot for {skill_id} on {harness}", file=sys.stderr)
                return 1
            rollback_base = _registry_mod._rollback_root(workspace, skill_id, harness)
            try:
                relative_snapshot = Path(snapshot_value).relative_to(rollback_base)
            except ValueError:
                relative_snapshot = Path()
            if len(relative_snapshot.parts) != 1 or relative_snapshot.parts[0] in {"", ".", ".."}:
                print(f"error: invalid receipt-bound rollback snapshot for {skill_id} on {harness}", file=sys.stderr)
                return 1
            previous_receipt_value = current_receipt.get("previous_receipt")
            if previous_receipt_value is not None and (
                not isinstance(previous_receipt_value, dict)
                or not _install_mod._valid_receipt_contract(previous_receipt_value, skill_id=skill_id, harness=harness)
            ):
                print(f"error: invalid previous rollback receipt for {skill_id} on {harness}", file=sys.stderr)
                return 1
            previous_receipt = previous_receipt_value or {}
            # The snapshot must be a plain directory directly under the held
            # rollback component; symlinked components refuse the whole rollback.
            snapshot_rel = ("skills", "rollback", skill_id, harness, relative_snapshot.parts[0])
            if _registry_mod._state_entry_kind(state_anchor, *snapshot_rel) != "dir":
                print(f"error: invalid receipt-bound rollback snapshot for {skill_id} on {harness}", file=sys.stderr)
                return 1
            dest = _install_mod._install_dir(workspace, harness, skill_id)
            install_rel = str(_install_mod._adapter_map(workspace)[harness]["install_path"]).format(skill_id=skill_id)
            under_state_root = install_rel.startswith(".brigade/")
            state_parts = tuple(part for part in install_rel.split("/") if part)[1:]
            dirs, files = _registry_mod._read_tree_from_anchor(state_anchor, *snapshot_rel)
            # The snapshot lives in attacker-influenced state: treat it as
            # untrusted input. It is only restored when its bytes match the
            # fingerprint recorded for this installation at snapshot-capture
            # time and when the same lint and rendered-text validation a fresh
            # install runs accepts those bytes. Unbound legacy snapshots carry
            # no recorded fingerprint and are refused outright.
            recorded_snapshot_fingerprint = current_receipt.get("rollback_snapshot_fingerprint")
            if not isinstance(recorded_snapshot_fingerprint, str) or not recorded_snapshot_fingerprint:
                print(
                    f"error: rollback snapshot carries no recorded fingerprint for {skill_id} on {harness}",
                    file=sys.stderr,
                )
                return 2
            snapshot_skill_md = files.get(("SKILL.md",))
            snapshot_text = snapshot_skill_md.decode("utf-8", errors="replace") if snapshot_skill_md is not None else ""
            if _registry_mod._files_fingerprint(files) != recorded_snapshot_fingerprint:
                print(
                    f"error: rollback snapshot does not match the fingerprint recorded for {skill_id} on {harness}",
                    file=sys.stderr,
                )
                return 2
            with tempfile.TemporaryDirectory(prefix="brigade-rollback-lint-") as staging:
                staged = Path(staging) / "snapshot"
                _registry_mod._write_snapshot_tree(dirs, files, staged)
                snapshot_lint = search_validate._lint_payload(Path(staging), str(staged))
            render_refusals = _registry_mod._rendered_skill_validation(snapshot_text, harness)
            if render_refusals or not snapshot_lint.get("valid"):
                problems = [*render_refusals, *(snapshot_lint.get("errors") or [])]
                detail = "; ".join(problems[:5])
                print(
                    f"error: rollback snapshot failed validation for {skill_id} on {harness}: {detail}",
                    file=sys.stderr,
                )
                return 2
            payload = {
                "workspace": str(workspace),
                "skill_id": skill_id,
                "target": harness,
                "snapshot": snapshot_value,
                "installed_dir": str(dest),
                "rolled_back_at": _now(),
                "restored_receipt": bool(previous_receipt),
                "snapshot_consumed": True,
            }
            if under_state_root:
                _registry_mod._remove_tree_in_anchor(state_anchor, *state_parts)
                _registry_mod._write_collected_tree_into_anchor(dirs, files, state_anchor, *state_parts)
            else:
                if dest.exists():
                    shutil.rmtree(dest)
                dest.mkdir(parents=True, exist_ok=True)
                for parts in dirs:
                    (dest.joinpath(*parts)).mkdir(parents=True, exist_ok=True)
                for parts, data in files.items():
                    (dest.joinpath(*parts)).write_bytes(data)
            if previous_receipt:
                _registry_mod._write_state_file(
                    state_anchor, "skills", "installs", receipt_name, data=_registry_mod._json_bytes(previous_receipt)
                )
            else:
                _registry_mod._unlink_in_anchor(state_anchor, "skills", "installs", receipt_name)
            _registry_mod._write_state_file(
                state_anchor, "skills", "installs", rollback_receipt_name, data=_registry_mod._json_bytes(payload)
            )
            _registry_mod._remove_tree_in_anchor(state_anchor, *snapshot_rel)
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload["receipt_path"] = str(rollback_receipt_path)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill_rollback: {skill_id}")
    print(f"target: {harness}")
    print(f"installed_dir: {dest}")
    return 0


def history(
    *, target: Path, skill: str | None = None, harness: str | None = None, json_output: bool = False, limit: int = 20
) -> int:
    target = target.expanduser().resolve()
    rows = _install_mod._install_history(target, skill_id=skill, harness=harness)[:limit]
    payload = {
        "target": str(target),
        "skill_id": _registry_mod._slug(skill) if skill else None,
        "harness": harness,
        "count": len(rows),
        "history": rows,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill install history: {target}")
    if skill:
        print(f"skill_id: {_registry_mod._slug(skill)}")
    if harness:
        print(f"harness: {harness}")
    for row in rows:
        print(f"- {row.get('installed_at')} {row.get('skill_id')} {row.get('target')} version={row.get('version')}")
    if not rows:
        print("no install history")
    return 0


def diff(*, target: Path, skill: str, harness: str, against: str = "bundled", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    baseline_skill = _registry_mod._resolve_diff_baseline(target, skill, against=against)
    lint_payload = search_validate._lint_payload(target, baseline_skill)
    if not lint_payload["valid"]:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "skill_id": skill, "valid": False, "lint": lint_payload},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: skill lint failed: {skill}", file=sys.stderr)
        return 1
    skill_id = _registry_mod._slug(str(lint_payload.get("skill_id") or skill))
    if harness not in _install_mod._install_targets(target):
        print(f"error: unknown skill install target: {harness}", file=sys.stderr)
        return 2
    source_dir = Path(str(lint_payload["skill_dir"]))
    metadata = raw_metadata if isinstance((raw_metadata := lint_payload.get("metadata")), dict) else {}
    if baseline_skill.startswith("registry:"):
        # The registry side of the diff is served through the state-root
        # anchor, never re-read by pathname after the lint.
        source_text = _registry_mod._anchored_registry_text(
            target, _registry_mod._slug(baseline_skill.removeprefix("registry:")), "SKILL.md"
        )
        source_text = source_text or ""
    else:
        source_text = _registry_mod._skill_md_path(source_dir).read_text(encoding="utf-8", errors="replace")
    rendered = _registry_mod._render_skill_text_for_harness(source_text, metadata, skill_id, harness)
    installed_dir = _install_mod._install_dir(target, harness, skill_id)
    # The installed side is inspected through one anchored snapshot; a
    # symlinked SKILL.md in a state-backed install target contributes
    # absence, never outside file content.
    _snapshot_dirs, snapshot_files = _install_mod._installed_tree_snapshot(target, installed_dir)
    installed_raw = snapshot_files.get(("SKILL.md",))
    installed_present = installed_raw is not None
    installed_text = installed_raw.decode("utf-8", errors="replace") if installed_raw is not None else ""
    installed_skill = _registry_mod._skill_md_path(installed_dir)
    source = raw_source if isinstance((raw_source := lint_payload.get("source")), dict) else {}
    diff_lines = list(
        difflib.unified_diff(
            installed_text.splitlines(),
            rendered.splitlines(),
            fromfile=str(installed_skill),
            tofile=f"{source.get('identity') or f'resolved:{skill_id}'}#{harness}",
            lineterm="",
        )
    )
    drift = _install_mod._drift_payload(
        target=target,
        skill_id=skill_id,
        harness=harness,
        lint_payload=lint_payload,
        rendered=rendered,
        installed_dir=installed_dir,
    )
    payload = {
        "target": str(target),
        "skill_id": skill_id,
        "harness": harness,
        "against": against,
        "baseline_skill": baseline_skill,
        "installed": installed_present,
        "installed_path": str(installed_skill),
        "changed": bool(diff_lines),
        "diff": diff_lines,
        "drift": {
            key: drift[key] for key in ("overall", "source", "render", "local_edit", "receipt_known", "content_changed")
        },
        "source": source,
        "render": drift["current_render"],
        "source_fingerprint": lint_payload.get("fingerprint"),
        "render_fingerprint": _registry_mod._text_fingerprint(rendered),
        "installed_fingerprint": drift["installed_skill_fingerprint"],
        "installed_skill_fingerprint": drift["installed_skill_fingerprint"],
        "installed_bundle_fingerprint": drift["installed_bundle_fingerprint"],
        "receipt": drift["receipt"],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if diff_lines:
        print("\n".join(diff_lines))
    else:
        print("no diff")
    return 0
