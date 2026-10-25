"""Fixed-path diagnostics for bug bundles, isolated from general Doctor hooks.

No directory enumeration or configured source traversal is allowed here. Each
invocation probes at most eight named entries and reads at most 64 KiB. The
collector runs each check in a subprocess with a five-second deadline.
"""

from __future__ import annotations

import json
import stat
import sys
from itertools import islice
from pathlib import Path

from . import bug_io

_BYTE_LIMIT = 64 * 1024
_ENTRY_LIMIT = 8
_BOOTSTRAP_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "MEMORY.md",
    "TOOLS.md",
    "USER.md",
    "SAFETY_RULES.md",
    "INSTALL_FOR_AGENTS.md",
)


def check(target: Path, name: str, *, entry_limit: int = _ENTRY_LIMIT) -> list[dict[str, str]]:
    """Return only fixed names and statuses, never file text or error details."""
    paths = (
        _BOOTSTRAP_FILES
        if name == "bootstrap"
        else ("AGENTS.md",)
        if name == "agents-quality"
        else (".brigade/security.toml",)
    )
    rows: list[dict[str, str]] = []
    for relative in islice(paths, max(0, min(entry_limit, _ENTRY_LIMIT))):
        path = target / relative
        label = (
            f"bootstrap: {relative}"
            if name == "bootstrap"
            else "agents-quality: AGENTS.md"
            if name == "agents-quality"
            else "security: config"
        )
        try:
            regular = stat.S_ISREG(path.lstat().st_mode)
        except FileNotFoundError:
            if name == "bootstrap" and relative == "AGENTS.md":
                rows.append({"status": "FAIL", "name": label})
            continue
        except OSError:
            regular = False
        if name == "agents-quality":
            try:
                text = (
                    bug_io.read_bytes(path, limit=_BYTE_LIMIT).decode("utf-8", errors="replace").lower()
                    if regular
                    else ""
                )
            except (OSError, ValueError):
                text = ""
            if "definition of done" not in text or "handoff" not in text:
                rows.append({"status": "WARN", "name": label})
        elif name == "bootstrap":
            if not regular:
                rows.append({"status": "FAIL" if relative == "AGENTS.md" else "WARN", "name": label})
        else:
            # Presence only. Config validation and scanner/host probes belong to
            # normal Doctor and cannot be claimed by this bounded subset.
            if not regular:
                rows.append({"status": "WARN", "name": label})
    if entry_limit < len(paths):
        rows.append({"status": "WARN", "name": "bug diagnostics: entry budget"})
    return rows


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1]), sys.argv[2], entry_limit=int(sys.argv[3]))))
