"""Install-time Cursor capability detection. Never invoke detection from a hook."""

from __future__ import annotations

import os
import re
import tempfile
import subprocess
from pathlib import Path

# Verified in the installed Agent bundles: 2026.09.18-9a7762b, 2026.09.28-64d2043,
# and 2026.10.01-e373342 enumerate postToolUseFailure. Unknown events invalidate configs.
CURSOR_FAILURE_HOOK_MIN_VERSION = "2026.09.18-9a7762b"
CURSOR_HOOK_PACKAGE_VERSION = "1.1.0"
FAILURE_EVENT = "postToolUseFailure"


def detect_version() -> str | None:
    try:
        # Even --version writes CLI configuration. Keep probes out of the user's profile.
        with tempfile.TemporaryDirectory(prefix="brigade-version-") as profile:
            env = dict(os.environ)
            env.update(
                dict.fromkeys(
                    ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "APPDATA", "LOCALAPPDATA"), profile
                )
            )
            env["NODE_DISABLE_COMPILE_CACHE"] = "1"
            result = subprocess.run(
                ["agent", "--version"], capture_output=True, text=True, timeout=2, check=False, env=env
            )
            return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, TypeError, AttributeError):
        return None


def supports_failure_hook(version: str | None) -> bool:
    if not isinstance(version, str):
        return False
    match = re.fullmatch(r"(\d{4}\.\d{2}\.\d{2})-[a-f0-9]{7,40}", version or "")
    if not match:
        return False
    from datetime import datetime

    try:
        datetime.strptime(match[1], "%Y.%m.%d")
    except ValueError:
        return False
    floor_date = CURSOR_FAILURE_HOOK_MIN_VERSION.split("-")[0]
    return match[1] > floor_date or version == CURSOR_FAILURE_HOOK_MIN_VERSION


def recording_available(target: Path) -> bool:
    """Read installed coverage only. No version subprocess in the capture path."""
    from . import cursor_user_cmd

    if cursor_user_cmd.project_presence_hook_installed(target, FAILURE_EVENT):
        return True
    try:
        doc, error = cursor_user_cmd._read_json_object(Path.home() / ".cursor" / "hooks.json")
        return (
            not error
            and isinstance(doc, dict)
            and any(
                cursor_user_cmd._same_command_hook(entry, cursor_user_cmd._hook_entry())
                for entry in doc.get("hooks", {}).get(FAILURE_EVENT, [])
            )
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def doctor_checks(target: Path) -> list[tuple[str, str, str]]:
    from . import config, work_mode

    try:
        cfg = config.load_config(target)
        selected = cfg is not None and "cursor" in cfg.selection.harnesses
    except (OSError, ValueError, TypeError, AttributeError):
        selected = False
    if selected and work_mode.is_observe(target) and not recording_available(target):
        return [
            (
                "WARN",
                "Cursor check recording",
                f"Cursor is too old or undetected for check recording. Install Cursor Agent {CURSOR_FAILURE_HOOK_MIN_VERSION} or newer and reinstall Brigade hooks.",
            )
        ]
    return []


def render_hooks(document: object, previous: str) -> str:
    """Keep each pre-existing hook entry's JSON bytes while merging the document."""
    import json
    from collections import defaultdict, deque

    old: dict[str, deque[str]] = defaultdict(deque)
    for start, end, value in _entry_spans(previous):
        old[json.dumps(value, sort_keys=True)].append(previous[start:end])
    text = json.dumps(document, indent=2, sort_keys=False) + "\n"
    replacements = []
    for start, end, value in _entry_spans(text):
        prior = old[json.dumps(value, sort_keys=True)]
        if prior:
            replacements.append((start, end, prior.popleft()))
    for start, end, raw in reversed(replacements):
        text = text[:start] + raw + text[end:]
    return text


def _entry_spans(text: str):
    import json

    decoder = json.JSONDecoder()

    def skip(index):
        while index < len(text) and text[index].isspace():
            index += 1
        return index

    def members(index):
        index = skip(index) + 1
        while (index := skip(index)) < len(text) and text[index] != "}":
            key, index = decoder.raw_decode(text, index)
            start = skip(skip(index) + 1)
            value, end = decoder.raw_decode(text, start)
            yield key, start, end, value
            index = skip(end)
            if text[index] != ",":
                return
            index += 1

    try:
        for key, start, _, _ in members(0):
            if key != "hooks":
                continue
            for _, array, _, entries in members(start):
                if not isinstance(entries, list):
                    continue
                index = skip(array + 1)
                for _ in entries:
                    value, end = decoder.raw_decode(text, index)
                    yield index, end, value
                    index = skip(end)
                    if index < len(text) and text[index] == ",":
                        index = skip(index + 1)
    except (ValueError, IndexError, TypeError):
        return
