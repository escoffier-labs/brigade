"""Antigravity (agy) --help capability probes for run_agent.

Split out of agents.py to keep it under the module size ceiling. agents.py
re-exports every name here, so callers and tests keep using ``agents.<name>``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import OrderedDict
from typing import Callable

from . import proc

_ANTIGRAVITY_PRINT_TIMEOUT_HELP = "--print-timeout"
_ANTIGRAVITY_PRINT_TIMEOUT_CACHE_LIMIT = 16
_ANTIGRAVITY_PRINT_TIMEOUT_SUPPORTED: OrderedDict[tuple[str, str], bool] = OrderedDict()
# ``--mode`` must not match ``--model``; plan mode is the value the read-only
# argv needs, so a build that lists --mode without plan does not count.
_ANTIGRAVITY_MODE_FLAG_RE = re.compile(r"(?<![\w-])--mode(?![\w-])")
_ANTIGRAVITY_MODE_PLAN_RE = re.compile(r"(?<![\w-])plan(?![\w-])")
_ANTIGRAVITY_MODE_SUPPORTED: OrderedDict[tuple[str, str], bool] = OrderedDict()
_ANTIGRAVITY_PLAN_MODE_WARNED: set[str] = set()


def _antigravity_env_fingerprint(env: dict[str, str] | None) -> str:
    """Return a non-reversible fingerprint for an environment dict.

    The fingerprint is a SHA-256 hex digest of a deterministic JSON encoding of
    the sorted env items.  No env keys or values are retained in the returned
    value, and ``env=None`` is treated as an empty environment.
    """
    items = sorted(env.items()) if env is not None else []
    return hashlib.sha256(json.dumps(items, separators=(",", ":")).encode("utf-8")).hexdigest()


def _antigravity_help_supports(
    cache: OrderedDict[tuple[str, str], bool],
    check: Callable[[str], bool],
    executable: str,
    env: dict[str, str] | None,
    process_registry: proc.ProcessRegistry | None,
) -> bool:
    """Run ``<executable> --help`` once per (executable, env fingerprint) and cache ``check(help)``.

    Only a successful probe is cached; a non-zero exit or timeout returns False uncached.

    The cache is bounded to ``_ANTIGRAVITY_PRINT_TIMEOUT_CACHE_LIMIT`` entries
    and stores only the executable string and a SHA-256 hex digest of the
    environment; it never retains env keys or values.
    """
    cache_key = (executable, _antigravity_env_fingerprint(env))
    if cache_key in cache:
        return cache[cache_key]
    result = proc.run(
        [executable, "--help"],
        timeout=5.0,
        env=env,
        process_registry=process_registry,
    )
    if result.code != 0:
        # A failed or timed-out probe says nothing about the build: answer False
        # for this call but do not cache it, so a transient failure cannot pin a
        # capable agy to the degraded argv for the rest of the process.
        return False
    supported = check("\n".join((result.stdout, result.stderr)))
    if len(cache) >= _ANTIGRAVITY_PRINT_TIMEOUT_CACHE_LIMIT:
        cache.popitem(last=False)
    cache[cache_key] = supported
    return supported


def _antigravity_supports_print_timeout(
    executable: str = "agy",
    *,
    env: dict[str, str] | None = None,
    process_registry: proc.ProcessRegistry | None = None,
) -> bool:
    """Whether this Antigravity executable advertises --print-timeout in agy --help."""
    return _antigravity_help_supports(
        _ANTIGRAVITY_PRINT_TIMEOUT_SUPPORTED,
        lambda text: _ANTIGRAVITY_PRINT_TIMEOUT_HELP in text,
        executable,
        env,
        process_registry,
    )


def _antigravity_help_lists_plan_mode(text: str) -> bool:
    """True when a help line that names ``--mode`` also lists ``plan`` as a value.

    Matching the two separately would accept a build whose --mode lacks plan
    but whose help mentions "plan" elsewhere, and that argv would then fail.
    """
    return any(
        _ANTIGRAVITY_MODE_FLAG_RE.search(line) and _ANTIGRAVITY_MODE_PLAN_RE.search(line) for line in text.splitlines()
    )


def _antigravity_supports_mode(
    executable: str = "agy",
    *,
    env: dict[str, str] | None = None,
    process_registry: proc.ProcessRegistry | None = None,
) -> bool:
    """Whether this Antigravity executable advertises ``--mode`` with a ``plan`` value in agy --help.

    Older builds (for example 1.4.1) lack it, and passing an unknown flag would
    fail the seat, so read-only runs only add ``--mode plan`` when detected.
    """
    return _antigravity_help_supports(
        _ANTIGRAVITY_MODE_SUPPORTED,
        _antigravity_help_lists_plan_mode,
        executable,
        env,
        process_registry,
    )


def _clear_antigravity_help_caches() -> None:
    _ANTIGRAVITY_PRINT_TIMEOUT_SUPPORTED.clear()
    _ANTIGRAVITY_MODE_SUPPORTED.clear()
    _ANTIGRAVITY_PLAN_MODE_WARNED.clear()


# Old name kept so existing callers and tests keep working.
_clear_antigravity_print_timeout_cache = _clear_antigravity_help_caches

_antigravity_supports_print_timeout.cache_clear = _clear_antigravity_help_caches  # type: ignore[attr-defined]
_antigravity_supports_mode.cache_clear = _clear_antigravity_help_caches  # type: ignore[attr-defined]


def _warn_antigravity_plan_mode_unsupported(executable: str) -> None:
    """Say once per executable that a read-only agy seat is read-only by prompt alone on this build."""
    if executable in _ANTIGRAVITY_PLAN_MODE_WARNED:
        return
    _ANTIGRAVITY_PLAN_MODE_WARNED.add(executable)
    print(
        "warning: this agy build does not list `--mode plan`, so the read-only Antigravity seat "
        "is read-only by prompt only (--sandbox restricts the terminal, not file edits). "
        "Upgrade agy to enforce read-only.",
        file=sys.stderr,
    )


_ANTIGRAVITY_NO_OUTPUT_MARKER = "jetski: no output produced"


def _antigravity_no_output_detail(stdout: str, stderr: str = "") -> str | None:
    """Return agy's own reason when a headless run exited 0 without doing the work.

    agy auto-denies a tool permission it cannot prompt for and still exits 0.
    agy 1.5.12 leaves stdout empty and writes one stderr line that starts with
    ``jetski: no output produced`` and names the denied permission. The caller
    turns that into a failed run carrying the reason. Any other stdout line is
    real output, so the run is left alone.
    """
    out_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if any(not line.startswith(_ANTIGRAVITY_NO_OUTPUT_MARKER) for line in out_lines):
        return None
    for stream in (stderr, stdout):
        for raw in stream.splitlines():
            line = raw.strip()
            if line.startswith(_ANTIGRAVITY_NO_OUTPUT_MARKER):
                return " ".join(line.split())
    return None


def _antigravity_no_output_kind(detail: str) -> str:
    """Classify agy's no-output line by the reason it gives.

    Only an auto-denied tool permission is ``permission-denied``. Any other
    no-output reason stays the generic ``empty-output`` failure.
    """
    lowered = detail.lower()
    return "permission-denied" if "permission" in lowered or "auto-denied" in lowered else "empty-output"
