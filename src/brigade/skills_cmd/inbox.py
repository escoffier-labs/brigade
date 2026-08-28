"""Skill inbox proposals and registry issue imports."""
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
from . import search_validate as _search_validate_mod
from . import adapters as _adapters_mod


def _skill_issue_records(target: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for issue in _adapters_mod._skill_health_issues(target):
        issue_type = str(issue.get("issue_type") or issue.get("name") or "skill_issue")
        skill_id = str(issue.get("skill_id") or "registry")
        harness = str(issue.get("harness") or "")
        detail = str(issue.get("detail") or "")
        source_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "skill_id": skill_id,
                    "harness": harness,
                    "issue_type": issue_type,
                    "detail": detail,
                    "fingerprint": issue.get("fingerprint"),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        metadata = {
            "skill_id": skill_id,
            "skill_harness": harness or None,
            "skill_issue_type": issue_type,
            "skill_issue_detail": detail,
            "source_item_key": f"skill-registry:{skill_id}:{issue_type}:{harness}",
            "source_fingerprint": source_fingerprint,
        }
        records.append(
            {
                "text": f"Repair skill registry issue {skill_id}/{issue_type}: {detail}",
                "kind": "task",
                "source": "skill-registry",
                "type": "workflow",
                "priority": "high" if issue.get("status") == _registry_mod.FAIL else "normal",
                "template": "bugfix",
                "acceptance": [f"`brigade skills doctor` no longer reports {skill_id}/{issue_type}."],
                "metadata": metadata,
            }
        )
    return records


def import_issues(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    from .. import work_cmd

    records = _skill_issue_records(target)
    imported, skipped, skipped_dismissed, _rejected = work_cmd._append_import_records(target, records)
    payload = {
        "target": str(target),
        "source": "skill-registry",
        "issue_count": len(records),
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "skipped_dismissed_count": len(skipped_dismissed),
        "imports": imported,
        "skipped": skipped,
        "skipped_dismissed": skipped_dismissed,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skills import-issues: {target}")
    print(f"issues: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"skipped_dismissed: {len(skipped_dismissed)}")
    return 0


def _proposal_meta_path(path: Path) -> Path:
    return path / "proposal.json"


def _proposal_skill_path(path: Path) -> Path:
    return path / "skill"


def _anchored_proposals(target: Path) -> list[tuple[str, dict[str, Any]]]:
    """List inbox proposals through the held state-root anchor.

    Only plain directories carrying a readable ``proposal.json`` contribute.
    A symlinked inbox component refuses the whole listing, a symlinked
    proposal directory is skipped, and a symlinked proposal file refuses that
    entry: outside file contents can never reach the output.
    """
    entries: list[tuple[str, bytes]] = []
    try:
        with _registry_mod._held_state_root(target) as state_anchor:
            if not _registry_mod._HAS_DESCRIPTOR_ANCHOR:
                # lstat-guarded fallback for platforms without anchoring.
                root = _registry_mod._inbox_root(target)
                if root.is_symlink() or _registry_mod._is_reparse_point(root) or not root.is_dir():
                    return []
                for child in sorted(root.iterdir(), key=lambda item: item.name):
                    if child.is_symlink() or _registry_mod._is_reparse_point(child) or not child.is_dir():
                        continue
                    meta = _proposal_meta_path(child)
                    try:
                        st = os.lstat(meta)
                    except OSError:
                        continue
                    if stat_module.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", False):
                        continue
                    if not stat_module.S_ISREG(st.st_mode):
                        continue
                    try:
                        entries.append((child.name, meta.read_bytes()))
                    except OSError:
                        continue
            else:
                try:
                    inbox_fd, opened = _registry_mod._anchor_open_chain(state_anchor, "skills", "inbox")
                except _registry_mod.SkillsStatePathError:
                    return []
                names: list[str] = []
                try:
                    for name in sorted(os.listdir(inbox_fd)):
                        try:
                            st = os.lstat(name, dir_fd=inbox_fd)
                        except FileNotFoundError:
                            continue
                        if stat_module.S_ISDIR(st.st_mode):
                            names.append(name)
                finally:
                    for fd in reversed(opened):
                        os.close(fd)
                for name in names:
                    try:
                        raw = _registry_mod._read_state_file_bytes(
                            state_anchor, "skills", "inbox", name, "proposal.json"
                        )
                    except _registry_mod.SkillsStatePathError:
                        continue
                    if raw is not None:
                        entries.append((name, raw))
    except _registry_mod.SkillsStatePathError:
        return []
    proposals: list[tuple[str, dict[str, Any]]] = []
    for name, raw in entries:
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        meta.setdefault("proposal_id", name)
        meta.setdefault("path", str(_registry_mod._inbox_root(target) / name))
        proposals.append((name, meta))
    return proposals


def _resolve_proposal(target: Path, proposal_id: str) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Resolve one proposal to its validated directory name under the anchor."""
    slug = _registry_mod._slug(proposal_id)
    matches = [(name, meta) for name, meta in _anchored_proposals(target) if name.startswith(slug)]
    if not matches:
        return None, None, f"skill proposal not found: {proposal_id}"
    if len(matches) > 1:
        return None, None, f"skill proposal id is ambiguous: {proposal_id}"
    return matches[0][0], matches[0][1], None


def _load_anchored_proposal(state_anchor: Any, name: str) -> dict[str, Any] | None:
    """Re-read one proposal.json through the held state-root anchor."""
    raw = _registry_mod._read_state_file_bytes(state_anchor, "skills", "inbox", name, "proposal.json")
    if raw is None:
        return None
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    meta.setdefault("proposal_id", name)
    return meta


def inbox_add(
    *,
    target: Path,
    source: Path,
    skill_id: str | None = None,
    summary: str | None = None,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    source_dir, collected_dirs, collected_files, error = _registry_mod._collect_classified_source_tree(target, source)
    if source_dir is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    metadata = _registry_mod._metadata_from_collected(collected_files)
    resolved_skill_id = _registry_mod._slug(skill_id or str(metadata.get("id") or source_dir.name))
    created = _now()
    proposal_id = f"{created[:19].replace(':', '').replace('-', '')}-{resolved_skill_id}-{uuid.uuid4().hex[:8]}"
    proposal_rel = ("skills", "inbox", _registry_mod._slug(proposal_id))
    proposal_dir = target / ".brigade" / "skills" / "inbox" / _registry_mod._slug(proposal_id)
    skill_dest = _proposal_skill_path(proposal_dir)
    try:
        with _registry_mod._held_state_root(target) as state_anchor:
            # Refuse a redirected inbox before anything is copied, replaced,
            # or deleted; a symlinked component never reaches the disk outside.
            _registry_mod._require_plain_state_dirs(state_anchor, "skills", "inbox")
            existing = _registry_mod._state_entry_kind(state_anchor, *proposal_rel)
            if existing == "dir" and not force:
                print(f"error: skill proposal already exists: {proposal_dir}", file=sys.stderr)
                return 2
            if force and existing != "missing":
                _registry_mod._remove_tree_in_anchor(state_anchor, *proposal_rel)
            # The classified collect above is the only source read: the copy,
            # the recorded fingerprint, and the staged lint below all derive
            # from those bytes; the state-root proposal copy is never re-read
            # by pathname.
            _registry_mod._write_collected_tree_into_anchor(
                collected_dirs, collected_files, state_anchor, *proposal_rel, "skill"
            )
            with tempfile.TemporaryDirectory(prefix="brigade-inbox-lint-") as staging:
                staged_skill = Path(staging) / "skill"
                _registry_mod._write_snapshot_tree(collected_dirs, collected_files, staged_skill)
                lint_payload = _search_validate_mod._lint_path_payload(
                    Path(staging), str(staged_skill), mode="lenient", contain=True
                )
                lint_payload["target"] = str(target)
                lint_payload = _search_validate_mod._repoint_staged_lint_paths(lint_payload, skill_dest, staged_skill)
            proposal = {
                "proposal_id": proposal_dir.name,
                "skill_id": resolved_skill_id,
                "status": "pending",
                "summary": summary or "",
                "source": str(source.expanduser().resolve()),
                "created_at": created,
                "path": str(proposal_dir),
                "skill_path": str(skill_dest),
                "fingerprint": _registry_mod._files_fingerprint(collected_files),
                "lint": lint_payload,
            }
            _registry_mod._write_state_file(
                state_anchor, *proposal_rel, "proposal.json", data=_registry_mod._json_bytes(proposal)
            )
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return 0 if lint_payload["valid"] else 1
    print(f"skill_proposal: {proposal['proposal_id']}")
    print(f"skill_id: {resolved_skill_id}")
    print("status: pending")
    return 0 if lint_payload["valid"] else 1


def inbox_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    proposals = [meta for _name, meta in _anchored_proposals(target)]
    payload = {"target": str(target), "proposal_count": len(proposals), "proposals": proposals}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"skill inbox: {target}")
    for proposal in proposals:
        print(f"- {proposal.get('proposal_id')} [{proposal.get('status')}] {proposal.get('skill_id')}")
    if not proposals:
        print("no skill proposals")
    return 0


def inbox_show(*, target: Path, proposal_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _name, proposal, error = _resolve_proposal(target, proposal_id)
    if proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return 0
    print(f"skill proposal: {proposal.get('proposal_id')}")
    print(f"skill_id: {proposal.get('skill_id')}")
    print(f"status: {proposal.get('status')}")
    print(f"fingerprint: {proposal.get('fingerprint')}")
    return 0


def inbox_diff(*, target: Path, proposal_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    name, proposal, error = _resolve_proposal(target, proposal_id)
    if name is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    skill_id = _registry_mod._slug(str(proposal.get("skill_id") or name))
    proposed_display = _registry_mod._inbox_root(target) / name / "skill" / "SKILL.md"
    existing_display = _registry_mod._skill_path(target, skill_id) / "SKILL.md"
    try:
        with _registry_mod._held_state_root(target) as state_anchor:
            # Both sides are read through the held anchor: the proposed tree
            # via descriptor collection and the registry copy via the
            # anchored reader, so symlinked components never drag outside
            # file contents into the diff.
            _proposal_dirs, proposed_files = _registry_mod._read_tree_from_anchor(
                state_anchor, "skills", "inbox", name, "skill"
            )
            existing_raw = _registry_mod._read_state_file_bytes(
                state_anchor, "skills", "registry", skill_id, "SKILL.md"
            )
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    proposed_raw = proposed_files.get(("SKILL.md",))
    before = existing_raw.decode("utf-8", errors="replace").splitlines() if existing_raw is not None else []
    after = proposed_raw.decode("utf-8", errors="replace").splitlines() if proposed_raw is not None else []
    diff = list(
        difflib.unified_diff(before, after, fromfile=str(existing_display), tofile=str(proposed_display), lineterm="")
    )
    payload = {
        "target": str(target),
        "proposal_id": proposal.get("proposal_id"),
        "skill_id": proposal.get("skill_id"),
        "diff": diff,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("\n".join(diff) if diff else "no diff")
    return 0


def inbox_accept(*, target: Path, proposal_id: str, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    name, proposal, error = _resolve_proposal(target, proposal_id)
    if name is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if proposal.get("status") != "pending" and not force:
        print(f"error: skill proposal is not pending: {proposal.get('status')}", file=sys.stderr)
        return 2
    try:
        with _registry_mod._held_state_root(target) as state_anchor:
            current = _load_anchored_proposal(state_anchor, name)
            if current is None:
                print(f"error: skill proposal not found: {proposal_id}", file=sys.stderr)
                return 2
            if current.get("status") != "pending" and not force:
                print(f"error: skill proposal is not pending: {current.get('status')}", file=sys.stderr)
                return 2
            proposal = current
            # The proposal's skill tree is read back through the anchor and
            # staged from those collected bytes; a symlinked proposal
            # component can never redirect the import outside the held state
            # root, and the status write below goes through the anchor too.
            proposal_dirs, proposal_files = _registry_mod._read_tree_from_anchor(
                state_anchor, "skills", "inbox", name, "skill"
            )
            with tempfile.TemporaryDirectory(prefix="brigade-proposal-import-") as staging:
                staged = Path(staging) / "skill"
                _registry_mod._write_snapshot_tree(proposal_dirs, proposal_files, staged)
                payload, import_error, rc = _registry_mod._registry_import_payload(
                    target=target,
                    source=staged,
                    skill_id=str(proposal.get("skill_id") or name),
                    force=force,
                    # Provenance stays the operator-supplied original; the
                    # private staging directory must never be persisted as
                    # the skill's source.
                    source_provenance=str(
                        proposal.get("source") or (_registry_mod._inbox_root(target) / name / "skill")
                    ),
                )
            if payload is None:
                print(f"error: {import_error}", file=sys.stderr)
                return rc
            proposal.update({"status": "accepted", "accepted_at": _now(), "registry": payload})
            _registry_mod._write_state_file(
                state_anchor, "skills", "inbox", name, "proposal.json", data=_registry_mod._json_bytes(proposal)
            )
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return rc
    print(f"skill_proposal: {proposal.get('proposal_id')}")
    print("status: accepted")
    print(f"skill_id: {proposal.get('skill_id')}")
    return rc


def inbox_reject(*, target: Path, proposal_id: str, reason: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    name, proposal, error = _resolve_proposal(target, proposal_id)
    if name is None or proposal is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        with _registry_mod._held_state_root(target) as state_anchor:
            current = _load_anchored_proposal(state_anchor, name)
            if current is None:
                print(f"error: skill proposal not found: {proposal_id}", file=sys.stderr)
                return 2
            if current.get("status") != "pending":
                print(f"error: skill proposal is not pending: {current.get('status')}", file=sys.stderr)
                return 2
            proposal = current
            proposal.update({"status": "rejected", "rejected_at": _now(), "reason": reason})
            _registry_mod._write_state_file(
                state_anchor, "skills", "inbox", name, "proposal.json", data=_registry_mod._json_bytes(proposal)
            )
    except _registry_mod.SkillsStatePathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(proposal, indent=2, sort_keys=True))
        return 0
    print(f"skill_proposal: {proposal.get('proposal_id')}")
    print("status: rejected")
    print(f"reason: {reason}")
    return 0
