"""Versioned Claude hook output envelope (issue #735).

Limits count Unicode code points (``len(str)`` in Python 3), not UTF-8 bytes.
The anti-truncation preamble and elision banner both count against ``max_chars``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .. import localio

ENVELOPE_VERSION = 1
DEFAULT_MAX_CHARS = 8_000
DEFAULT_MAX_ITEMS = 32
HOOK_TIMEOUT_SECONDS = 12
MAX_INJECTION_FILES = 64
MAX_LOG_BYTES = 256_000
LOG_NAME = "hook.log"
INJECTIONS_DIRNAME = "injections"
DOCTOR_POINTER = "Brigade hook degraded; run `brigade doctor --target .` for details."
CapName = Literal["items", "chars", "oversized"]

_HOME_PATH_RE = re.compile(re.escape(str(Path.home().expanduser().resolve())))


def empty_envelope(event: str) -> dict[str, Any]:
    """Return the schema-valid empty payload for a Claude hook event.

    Claude Code treats ``{}`` as success with no changes for every managed event.
    Each event pins the same object so tests can assert the contract per hook.
    """
    del event  # event-specific empty shapes are identical under the host schema
    return {}


def degraded_envelope(event: str) -> dict[str, Any]:
    """One-line doctor pointer used when the hook fails open."""
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": DOCTOR_POINTER,
        }
    }


def hooks_state_root(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "work" / "claude-hooks"


def injections_root(target: Path) -> Path:
    return hooks_state_root(target) / INJECTIONS_DIRNAME


def log_path(target: Path) -> Path:
    return hooks_state_root(target) / LOG_NAME


def injection_path(target: Path, *, session_id: str, event: str) -> Path:
    slug = localio.slugify(session_id, fallback="session")[:48]
    suffix = localio.stable_hash(session_id)[:8]
    event_slug = localio.slugify(event, fallback="event")
    return injections_root(target) / f"{slug}-{suffix}-{event_slug}.txt"


def relative_injection_path(target: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(target.expanduser().resolve()).as_posix()
    except ValueError:
        return path.name


def redact_paths(text: str) -> str:
    """Replace the operator home prefix in diagnostics so logs stay shareable."""
    home = str(Path.home().expanduser().resolve())
    redacted = text.replace(home, "~")
    if home != str(Path.home()):
        redacted = redacted.replace(str(Path.home()), "~")
    return _HOME_PATH_RE.sub("~", redacted)


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def write_private_text(path: Path, data: str) -> None:
    """Atomically publish text and enforce mode ``0600`` on the final file."""
    localio.write_text_atomic(path, data)
    _chmod_private(path)


def append_log(target: Path, message: str) -> None:
    """Append one redacted diagnostic line; bound file growth by truncation."""
    root = hooks_state_root(target)
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
    except OSError:
        return
    path = log_path(target)
    line = f"{localio.utc_now_iso()} {redact_paths(message.rstrip())}\n"
    try:
        if path.is_file() and path.stat().st_size + len(line.encode("utf-8")) > MAX_LOG_BYTES:
            existing = path.read_text(encoding="utf-8")
            keep = existing[-MAX_LOG_BYTES // 2 :]
            cut = keep.find("\n")
            if cut >= 0:
                keep = keep[cut + 1 :]
            write_private_text(path, keep + line)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            _chmod_private(path)
    except OSError:
        return


def cleanup_injections(target: Path, *, keep: int = MAX_INJECTION_FILES) -> None:
    root = injections_root(target)
    if not root.is_dir():
        return
    try:
        entries = sorted(root.glob("*.txt"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in entries[max(keep, 0) :]:
        try:
            stale.unlink()
        except OSError:
            continue


def anti_truncation_line(rel_path: str) -> str:
    return f"[Brigade] If this context appears truncated, read the full copy before continuing: cat {rel_path}"


def elision_banner(*, dropped: int, total: int, cap: CapName, rel_path: str) -> str:
    return f"[Brigade] Elided {dropped} of {total} records ({cap} cap); full copy: cat {rel_path}"


def oversized_stub(*, code_points: int, rel_path: str) -> str:
    return f"[Brigade] Record exceeds injection budget ({code_points} code points); full copy: cat {rel_path}"


@dataclass(frozen=True)
class RenderedInjection:
    text: str
    persisted_path: Path
    relative_path: str
    selected_count: int
    dropped_count: int
    cap_fired: CapName | None
    envelope_version: int = ENVELOPE_VERSION


def _join(parts: list[str]) -> str:
    return "\n".join(part for part in parts if part is not None)


def _oversized_body(preamble: str, record: str, rel_path: str, max_chars: int) -> str:
    stub = oversized_stub(code_points=len(record), rel_path=rel_path)
    body = _join([preamble, stub])
    if len(body) > max_chars:
        return body[: max(0, max_chars - 1)] + "…"
    return body


def render_records(
    records: list[str],
    *,
    target: Path,
    session_id: str,
    event: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> RenderedInjection:
    """Persist the full payload, then emit a capped whole-record injection body.

    Character limits are Unicode code points. The preamble and elision banner
    reserve budget before any record is considered. A single record that cannot
    fit emits a bounded metadata stub instead of overflowing the host cap.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if max_items < 1:
        raise ValueError("max_items must be positive")

    normalized = [str(item) for item in records if str(item)]
    path = injection_path(target, session_id=session_id, event=event)
    full_text = "\n".join(normalized)
    write_private_text(path, full_text if full_text.endswith("\n") or not full_text else full_text + "\n")
    cleanup_injections(target)
    rel = relative_injection_path(target, path)
    preamble = anti_truncation_line(rel)

    if not normalized:
        return RenderedInjection(
            text=preamble,
            persisted_path=path,
            relative_path=rel,
            selected_count=0,
            dropped_count=0,
            cap_fired=None,
        )

    total = len(normalized)
    item_limited = normalized[:max_items]
    item_dropped = total - len(item_limited)

    def select(candidates: list[str], *, reserved_extra: int) -> tuple[list[str], CapName | None]:
        budget = max_chars - len(preamble) - (1 if preamble else 0) - reserved_extra
        if budget < 0:
            budget = 0
        chosen: list[str] = []
        fired: CapName | None = None
        for record in candidates:
            extra = len(record) if not chosen else len(record) + 1
            if not chosen and len(record) > budget:
                return [], "oversized"
            if extra > budget:
                fired = "chars"
                break
            chosen.append(record)
            budget -= extra
        return chosen, fired

    selected, first_cap = select(item_limited, reserved_extra=0)
    if first_cap == "oversized":
        return RenderedInjection(
            text=_oversized_body(preamble, normalized[0], rel, max_chars),
            persisted_path=path,
            relative_path=rel,
            selected_count=0,
            dropped_count=total,
            cap_fired="oversized",
        )

    dropped = total - len(selected)
    if dropped == 0:
        return RenderedInjection(
            text=_join([preamble, *selected]),
            persisted_path=path,
            relative_path=rel,
            selected_count=len(selected),
            dropped_count=0,
            cap_fired=None,
        )

    if item_dropped and len(selected) == len(item_limited):
        fired: CapName = "items"
    else:
        fired = "chars"

    for _ in range(5):
        dropped = total - len(selected)
        banner = elision_banner(dropped=dropped, total=total, cap=fired, rel_path=rel)
        body = _join([preamble, banner, *selected])
        if len(body) <= max_chars:
            return RenderedInjection(
                text=body,
                persisted_path=path,
                relative_path=rel,
                selected_count=len(selected),
                dropped_count=dropped,
                cap_fired=fired,
            )
        selected, again = select(item_limited, reserved_extra=len(banner) + 1)
        if not selected or again == "oversized":
            break
        if item_dropped and len(selected) == len(item_limited):
            fired = "items"
        else:
            fired = "chars"

    return RenderedInjection(
        text=_oversized_body(preamble, normalized[0], rel, max_chars),
        persisted_path=path,
        relative_path=rel,
        selected_count=0,
        dropped_count=total,
        cap_fired="oversized",
    )


def additional_context_envelope(
    event: str,
    records: list[str],
    *,
    target: Path,
    session_id: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict[str, Any]:
    rendered = render_records(
        records,
        target=target,
        session_id=session_id,
        event=event,
        max_chars=max_chars,
        max_items=max_items,
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": rendered.text,
        }
    }


def emit_stdout(payload: dict[str, Any]) -> None:
    """Print one compact JSON line; never pretty-print (async hosts parse line-wise)."""
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)


def resolve_caps(
    *,
    max_chars: int | None = None,
    max_items: int | None = None,
) -> tuple[int, int]:
    chars = max_chars if max_chars is not None else _env_int("BRIGADE_HOOK_MAX_CHARS", DEFAULT_MAX_CHARS)
    items = max_items if max_items is not None else _env_int("BRIGADE_HOOK_MAX_ITEMS", DEFAULT_MAX_ITEMS)
    return max(chars, 1), max(items, 1)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
