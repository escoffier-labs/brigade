#!/usr/bin/env python3
"""Generate or validate the digests of every released text of the files init and care install place.

`--write` walks every `v*` release tag plus the working tree and records, for each
shipped SKILL.md and care runbook, the digest of each distinct text. `--check`
fails when a template changed without regenerating the manifest, when the schema
or the tag provenance is wrong, or when an entry the base ref recorded was dropped.
Runtime use: `src/brigade/shipped_history.py` (an installed copy that equals a
recorded earlier text has no local edits and may be refreshed).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from brigade import localio, shipped_history  # noqa: E402

MANIFEST_REL = "src/brigade/templates/shipped-file-history.json"
SKILLS = ("brigade-work", "ultra-work-scout", "note")
SKILL_REL = "src/brigade/templates/skills/{skill_id}/SKILL.md"
RUNBOOK_TEMPLATE_DIR = "src/brigade/templates/memory-care/runbooks"
RUNBOOK_INSTALL_DIR = ".brigade/memory-care/runbooks"
# Runbooks whose payload is a Python literal rather than a template file:
# (installed relative path, source file, constant name).
PYTHON_PAYLOADS = (
    (
        f"{RUNBOOK_INSTALL_DIR}/daily-observability.json",
        "src/brigade/care_cmd.py",
        "DAILY_OBSERVABILITY_RUNBOOK_PAYLOAD",
    ),
    (f"{RUNBOOK_INSTALL_DIR}/memory-closeout.json", "src/brigade/care_cmd.py", "MEMORY_CLOSEOUT_RUNBOOK_PAYLOAD"),
    (".brigade/runbooks/run-patrol.json", "src/brigade/care_patrol.py", "RUNBOOK_PAYLOAD"),
)
BASE_REF_ENV = "BRIGADE_SHIPPED_HISTORY_BASE_REF"
DEFAULT_BASE_REF = "origin/main"

Reader = Callable[[str], bytes | None]


def _git(*args: str) -> bytes | None:
    """Stdout of a git command, or None when it fails (a missing blob, tag or ref)."""
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def release_tags() -> list[str]:
    """Local ``vX.Y.Z`` tags in version order."""
    out = _git("tag", "--list", "v*") or b""
    tags = [t for t in out.decode().split() if shipped_history.TAG_PATTERN.match(t)]
    return sorted(tags, key=lambda t: tuple(int(p) for p in t[1:].split(".")))


def _working_reader(rel: str) -> bytes | None:
    try:
        return shipped_history.read_regular_file(ROOT / rel, 8 << 20)
    except OSError:
        return None


def _tag_reader(tag: str) -> Reader:
    return lambda rel: _git("cat-file", "blob", f"{tag}:{rel}")


def _runbook_names(ref: str | None) -> list[str]:
    if ref is None:
        folder = ROOT / RUNBOOK_TEMPLATE_DIR
        return sorted(p.name for p in folder.glob("*.json") if p.is_file() and not p.is_symlink())
    out = _git("ls-tree", "--name-only", f"{ref}:{RUNBOOK_TEMPLATE_DIR}") or b""
    return sorted(name for name in out.decode().split("\n") if name.endswith(".json"))


def _extract_payload(source: bytes, constant: str) -> dict[str, object] | None:
    """The literal dict assigned to module-level ``constant``, or None when absent or not a literal."""
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names, value = [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names, value = [node.target.id], node.value
        else:
            continue
        if constant in names:
            try:
                payload = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return None
            return payload if isinstance(payload, dict) else None
    return None


def digests_at(ref: str | None) -> dict[str, str]:
    """Key -> digest of each shipped file present at a release tag (``ref``) or in the working tree (None)."""
    read = _working_reader if ref is None else _tag_reader(ref)
    found: dict[str, str] = {}
    for skill_id in SKILLS:
        data = read(SKILL_REL.format(skill_id=skill_id))
        if data is not None:
            found[shipped_history.skill_key(skill_id)] = shipped_history.skill_digest(data)
    for name in _runbook_names(ref):
        data = read(f"{RUNBOOK_TEMPLATE_DIR}/{name}")
        digest = shipped_history.runbook_bytes_digest(data) if data is not None else None
        if digest is not None:
            found[shipped_history.runbook_key(f"{RUNBOOK_INSTALL_DIR}/{name}")] = digest
    for rel, source_rel, constant in PYTHON_PAYLOADS:
        data = read(source_rel)
        payload = _extract_payload(data, constant) if data is not None else None
        if payload is not None:
            found[shipped_history.runbook_key(rel)] = shipped_history.runbook_payload_digest(payload)
    return found


def first_tags(tags: list[str]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Per key, digest -> first tag that shipped it; plus the same walk for every tag (for checks)."""
    first: dict[str, dict[str, str]] = {}
    per_tag: dict[str, dict[str, str]] = {}
    for tag in tags:
        per_tag[tag] = digests_at(tag)
        for key, digest in per_tag[tag].items():
            first.setdefault(key, {}).setdefault(digest, tag)
    return first, per_tag


def build(existing: dict[str, tuple[shipped_history.Entry, ...]]) -> dict[str, list[dict[str, str | None]]]:
    """New manifest body: tagged texts in release order, then kept unreleased ones, then the working tree's."""
    first, _ = first_tags(release_tags())
    current = digests_at(None)
    files: dict[str, list[dict[str, str | None]]] = {}
    for key in sorted({*first, *current, *existing}):
        rows: dict[str, str | None] = {}
        tagged = sorted(first.get(key, {}).items(), key=lambda item: tuple(int(p) for p in item[1][1:].split(".")))
        for digest, tag in tagged:
            rows[digest] = tag
        for entry in existing.get(key, ()):  # append-only: nothing recorded is ever dropped
            rows.setdefault(entry.sha256, entry.tag)
        if key in current:
            rows.setdefault(current[key], None)
        files[key] = [{"sha256": digest, "tag": tag} for digest, tag in rows.items()]
    return files


def render(files: dict[str, list[dict[str, str | None]]]) -> str:
    return json.dumps({"schema": shipped_history.SCHEMA, "files": files}, indent=2) + "\n"


def _load_committed(path: Path) -> dict[str, tuple[shipped_history.Entry, ...]]:
    try:
        return shipped_history.load(path)
    except (OSError, ValueError):
        return {}


def check(path: Path, *, require_provenance: bool = False) -> list[str]:
    """Every reason the committed manifest is not acceptable (empty when it is).

    Tag provenance is what stops a row from vouching for an operator's edited text, so
    without local ``v*`` tags it is a failure when ``require_provenance`` is set or
    ``GITHUB_ACTIONS`` is in the environment. Only an explicit local run without those
    reports the skip on stderr and passes.
    """
    try:
        manifest = shipped_history.load(path)
    except (OSError, ValueError) as exc:
        return [f"{path}: {exc}; run scripts/shipped_file_history.py --write"]
    problems: list[str] = []
    current = digests_at(None)
    for key, digest in sorted(current.items()):
        if digest not in {e.sha256 for e in manifest.get(key, ())}:
            problems.append(f"{key}: current text is not recorded; run scripts/shipped_file_history.py --write")
    for source_key in [shipped_history.skill_key(s) for s in SKILLS]:
        if source_key not in current:
            problems.append(f"{source_key}: bundled source file is missing")
    tags = release_tags()
    if tags:
        first, _ = first_tags(tags)
        for key, by_digest in sorted(first.items()):
            known = {e.sha256 for e in manifest.get(key, ())}
            for digest, tag in by_digest.items():
                if digest not in known:
                    problems.append(f"{key}: text shipped in {tag} is not recorded; run --write")
        for key, entries in sorted(manifest.items()):
            for entry in entries:
                if entry.tag is not None and first.get(key, {}).get(entry.sha256) != entry.tag:
                    problems.append(f"{key}: {entry.sha256[:12]} claims tag {entry.tag}, which did not ship it")
    elif require_provenance or os.environ.get("GITHUB_ACTIONS"):
        problems.append("no v* tags available, so tag provenance cannot be checked; run git fetch origin --tags")
    else:
        print(
            "shipped file history: no v* tags available; tag provenance SKIPPED (CI and releases use --require-provenance)",
            file=sys.stderr,
        )
    base_ref = os.environ.get(BASE_REF_ENV, DEFAULT_BASE_REF)
    base_raw = _git("show", f"{base_ref}:{MANIFEST_REL}")
    if base_raw is not None:
        try:
            base = shipped_history.parse_manifest(base_raw)
        except ValueError:
            base = {}
        for key, entries in sorted(base.items()):
            kept = {e.sha256 for e in manifest.get(key, ())}
            problems.extend(
                f"{key}: {e.sha256[:12]} recorded on {base_ref} was dropped" for e in entries if e.sha256 not in kept
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write", action="store_true", help="regenerate the manifest from release tags and the working tree"
    )
    mode.add_argument("--check", action="store_true", help="validate the committed manifest")
    parser.add_argument(
        "--require-provenance",
        action="store_true",
        help="with --check: fail instead of skipping tag provenance when no v* tags exist (always on in GitHub Actions)",
    )
    args = parser.parse_args()

    path = ROOT / MANIFEST_REL
    if args.write:
        files = build(_load_committed(path))
        localio.write_text_atomic(path, render(files))
        print(f"shipped file history: wrote {path} ({len(files)} files)")
        return 0
    problems = check(path, require_provenance=args.require_provenance)
    for problem in problems:
        print(f"shipped file history: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"shipped file history: ok ({len(shipped_history.load(path))} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
