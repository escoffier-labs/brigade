"""Hand-rolled interactive prompt for harness/depth/include selection.

No external deps. Uses stdin line input + numbered toggles, so it works
over any TTY (no raw mode, no curses, no ANSI escape sequences required).
"""
from __future__ import annotations

import sys
from typing import List

from .selection import (
    KNOWN_HARNESSES,
    KNOWN_DEPTHS,
    KNOWN_INCLUDES,
    Selection,
    resolve_owner,
)


class NonInteractiveError(Exception):
    """Raised when prompt_for_selection() runs without a TTY."""


_HARNESS_ORDER = ["claude", "codex", "openclaw", "hermes"]
_DEPTH_ORDER = ["repo", "workspace"]
_INCLUDE_ORDER = ["publisher"]

_HARNESS_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "openclaw": "OpenClaw",
    "hermes": "Hermes (experimental)",
}

_DEPTH_LABELS = {
    "repo": "repo       (handoff flow + publish guard)",
    "workspace": "workspace  (full home: MEMORY.md, TOOLS.md, USER.md, ...)",
}

_INCLUDE_LABELS = {
    "publisher": "publisher  (content-guard policies for blog/social/docs)",
}


def prompt_for_selection() -> Selection:
    if not sys.stdin.isatty():
        raise NonInteractiveError(
            "solo-mise init needs a TTY for the interactive prompt. "
            "Pass --depth and --harnesses (or --profile) for scripting."
        )

    selected_harnesses = _toggle_prompt(
        title="Which harnesses do you use?",
        options=_HARNESS_ORDER,
        labels=_HARNESS_LABELS,
        defaults=["claude"],
    )
    depth = _single_prompt(
        title="Depth?",
        options=_DEPTH_ORDER,
        labels=_DEPTH_LABELS,
        default="repo",
    )
    selected_includes = _toggle_prompt(
        title="Add-ons?",
        options=_INCLUDE_ORDER,
        labels=_INCLUDE_LABELS,
        defaults=[],
    )

    owner = resolve_owner(selected_harnesses)
    return Selection(
        depth=depth,
        harnesses=selected_harnesses,
        owner=owner,
        includes=selected_includes,
    )


def _toggle_prompt(title, options, labels, defaults):
    selected = list(defaults)
    print()
    print(title + " (type numbers separated by space/comma to toggle, enter to confirm)")
    while True:
        for i, opt in enumerate(options, start=1):
            mark = "x" if opt in selected else " "
            print(f"  [{mark}] {i}. {labels.get(opt, opt)}")
        raw = sys.stdin.readline()
        if raw == "":  # EOF
            break
        raw = raw.strip()
        if not raw:
            break
        tokens = [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
        invalid = []
        for t in tokens:
            try:
                idx = int(t)
            except ValueError:
                invalid.append(t)
                continue
            if not 1 <= idx <= len(options):
                invalid.append(t)
                continue
            opt = options[idx - 1]
            if opt in selected:
                selected.remove(opt)
            else:
                selected.append(opt)
        if invalid:
            print(f"  (ignored invalid: {' '.join(invalid)})")
    # Preserve canonical order rather than toggle order.
    return [o for o in options if o in selected]


def _single_prompt(title, options, labels, default):
    print()
    print(title + " (type a number, enter for default)")
    for i, opt in enumerate(options, start=1):
        marker = "*" if opt == default else " "
        print(f"  {marker} {i}. {labels.get(opt, opt)}")
    raw = sys.stdin.readline()
    if raw == "":
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        idx = int(raw)
        if 1 <= idx <= len(options):
            return options[idx - 1]
    except ValueError:
        pass
    print(f"  (invalid; using default {default!r})")
    return default
