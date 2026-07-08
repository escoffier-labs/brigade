"""Manage the allow_values list in a private policy file.

This is the CLI counterpart to the pre-push hook's remedy text: when a
known-public literal trips a scan (including history scans that inline allow
comments cannot reach), the sanctioned fix is adding the exact matched string
to allow_values in the private policy. `content-guard allow add` makes that a
one-command operation instead of hand-editing JSON.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_PRIVATE_POLICY = Path.home() / ".config" / "content-guard" / "internal.json"
NOTES_KEY = "_allow_values_notes"


def resolve_policy_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("CONTENT_GUARD_PRIVATE_POLICY")
    if env:
        return Path(env)
    return DEFAULT_PRIVATE_POLICY


def add_values(policy_path: Path, values: list[str], note: str | None = None) -> tuple[list[str], list[str]]:
    """Append literal values to allow_values, skipping duplicates.

    Returns (added, skipped). Raises FileNotFoundError or ValueError on a
    missing or malformed policy file; never creates a policy file from
    scratch, since the private policy carries more than the allowlist.
    """
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{policy_path}: policy root must be a JSON object")

    existing = raw.get("allow_values")
    if existing is None:
        existing = []
        raw["allow_values"] = existing
    if not isinstance(existing, list):
        raise ValueError(f"{policy_path}: allow_values must be a list")

    added: list[str] = []
    skipped: list[str] = []
    for value in values:
        if value in existing or value in added:
            skipped.append(value)
        else:
            existing.append(value)
            added.append(value)

    if note and added:
        notes = raw.get(NOTES_KEY)
        if not isinstance(notes, dict):
            notes = {}
            raw[NOTES_KEY] = notes
        for value in added:
            notes[value] = note

    if added or (note and not skipped):
        policy_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return added, skipped


def list_values(policy_path: Path) -> list[str]:
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    values = raw.get("allow_values", [])
    if not isinstance(values, list):
        raise ValueError(f"{policy_path}: allow_values must be a list")
    return [str(v) for v in values]


def run_add(policy_arg: str | None, values: list[str], note: str | None) -> int:
    policy_path = resolve_policy_path(policy_arg)
    try:
        added, skipped = add_values(policy_path, values, note)
    except FileNotFoundError:
        print(f"allow add: policy file not found: {policy_path}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"allow add: {exc}", file=sys.stderr)
        return 2

    for value in added:
        print(f"added: {value}")
    for value in skipped:
        print(f"already allowed: {value}")
    print(f"policy: {policy_path} ({len(list_values(policy_path))} allow_values)")
    return 0


def run_list(policy_arg: str | None) -> int:
    policy_path = resolve_policy_path(policy_arg)
    try:
        values = list_values(policy_path)
    except FileNotFoundError:
        print(f"allow list: policy file not found: {policy_path}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"allow list: {exc}", file=sys.stderr)
        return 2

    for value in values:
        print(value)
    return 0
