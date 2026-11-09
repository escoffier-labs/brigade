"""Claude hook side of S24: hub recall, brief deduplication and the Stop capture.

Recall itself runs in the killable recall child (``memory_hooks``), with hub
routing and the prompt-load scan enabled for hooks. This module adds:

- Deduplication. Card ids the SessionStart work brief named, a source Brigade
  generated, are kept per session and skipped by every later recall. Prompt
  text never adds exclusions: a pasted ``card_id: x`` must not hide card x.
  Trusted dispatch envelopes can add their ids in S25.
- A per-session ledger of card keys: pushed (shown by a recall hook) and opened
  (read through the Read tool). It holds ids and counters only.
- The Stop capture: one ``capture`` call per batch, through a killable child,
  with a hashed session reference and the keys not yet captured. With the hub
  stopped or hung the batch stays pending and the hook keeps its cap.

The audience comes from ``recall_audience``: ``connector`` unless the operator
opted this host in with ``--recall-audience local``. A connector audience never
gets private-class text (S18).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .recall_audience import AUDIENCE_ENV, REMOTE_ENV, audience

LEDGER_VERSION = 1
LEDGER_KEYS_MAX = 200
CAPTURE_TIMEOUT_SECONDS = 3.0
CAPTURE_RESERVE_SECONDS = 1.0
CAPTURE_MIN_SECONDS = 0.5
# Recall runs after the brief and gets only what the hook budget has left, less a
# reserve for writing the injection. Below the minimum it is skipped.
RECALL_RESERVE_SECONDS = 1.5
RECALL_MIN_SECONDS = 1.0
# A deadline this far past belongs to an earlier event in a long-lived process.
_STALE_DEADLINE_SECONDS = 30.0
# The hub's card size limit (memory_hub_import.MAX_TEXT_BYTES, pinned by a test).
# A Read of anything larger is not recorded, so a huge or sparse file cannot
# spend the hook's budget or memory.
CARD_READ_MAX_BYTES = 512 * 1024
# Below this much hook budget a card read is not observed at all.
OBSERVE_MIN_SECONDS = 0.5
_CARD_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
)
_POSIX = sys.platform != "win32"
_EVENT_DEADLINE: list[float | None] = [None]
_SESSION_REF_LABEL = b"brigade.memory.capture.session.v1\x00"
_CARD_ID = r"[a-z0-9][a-z0-9._-]{0,127}"
# `cards/<id>.md` paths and `card_id: <id>` fields, as the work brief writes them.
_BRIEF_CARD_PATH = re.compile(rf"(?<![A-Za-z0-9._-])cards/({_CARD_ID})\.md(?![A-Za-z0-9_-])")
_BRIEF_CARD_FIELD = re.compile(rf"\bcard_id[\"']?\s*[:=]\s*[\"']?({_CARD_ID})")


__all__ = ["AUDIENCE_ENV", "REMOTE_ENV", "audience", "brief_card_ids", "capture", "recall_text"]


def start_event(budget: float) -> None:
    """Record this hook event's deadline: ``budget`` seconds from now."""
    _EVENT_DEADLINE[0] = time.monotonic() + budget


def recall_budget() -> float | None:
    """Seconds recall may use now, or None outside a timed hook event."""
    deadline = _EVENT_DEADLINE[0]
    if deadline is None:
        return None
    now = time.monotonic()
    if now - deadline > _STALE_DEADLINE_SECONDS:
        return None
    return deadline - now - RECALL_RESERVE_SECONDS


def brief_card_ids(text: str) -> list[str]:
    """Card ids the SessionStart work brief already put in front of the agent. Never call on prompt text."""
    if not isinstance(text, str) or not text:
        return []
    found = [*_BRIEF_CARD_PATH.findall(text), *_BRIEF_CARD_FIELD.findall(text)]
    return list(dict.fromkeys(found))[:LEDGER_KEYS_MAX]


def ledger_path(target: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:40]
    return target / ".brigade" / "work" / "claude-hooks" / "recall" / f"{digest}.json"


def _empty() -> dict[str, Any]:
    return {"v": LEDGER_VERSION, "brief": [], "pushed": [], "opened": [], "captured": [0, 0], "pending": None}


def _keys(value: object) -> list[str]:
    from ..memory_hook_hub import CARD_KEY_RE

    values = value if isinstance(value, list) else []
    return [v for v in values if isinstance(v, str) and CARD_KEY_RE.fullmatch(v)][:LEDGER_KEYS_MAX]


def load(path: Path) -> dict[str, Any]:
    """The ledger, or an empty one when it is missing or unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(raw, dict) or raw.get("v") != LEDGER_VERSION:
        return _empty()
    ledger = _empty()
    for name in ("brief", "pushed", "opened"):
        ledger[name] = _keys(raw.get(name))
    captured = raw.get("captured")
    if isinstance(captured, list) and len(captured) == 2 and all(isinstance(n, int) and n >= 0 for n in captured):
        ledger["captured"] = [min(captured[0], len(ledger["pushed"])), min(captured[1], len(ledger["opened"]))]
    pending = raw.get("pending")
    if isinstance(pending, dict) and isinstance(pending.get("id"), str):
        ends = pending.get("ends")
        if isinstance(ends, list) and len(ends) == 2 and all(isinstance(n, int) and n >= 0 for n in ends):
            ledger["pending"] = {
                "id": pending["id"],
                "pushed": _keys(pending.get("pushed")),
                "opened": _keys(pending.get("opened")),
                "ends": [min(ends[0], len(ledger["pushed"])), min(ends[1], len(ledger["opened"]))],
            }
    return ledger


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize ledger updates between hook processes, best effort."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        os.close(fd)


def update(path: Path, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Apply ``change`` to the ledger and replace the file atomically."""
    with _locked(path):
        ledger = load(path)
        change(ledger)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(ledger, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return ledger


def _append(ledger: dict[str, Any], name: str, keys: list[str]) -> None:
    current = ledger[name]
    for key in _keys(keys):
        if key not in current and len(current) < LEDGER_KEYS_MAX:
            current.append(key)


def _session(payload: dict[str, Any]) -> str | None:
    session_id = payload.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def _cwd(payload: dict[str, Any]) -> Path | None:
    raw = payload.get("cwd")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except OSError:
        return None


def recall_text(target: Path, payload: dict[str, Any], *, prompt: str | None = None, brief: str | None = None) -> str:
    """Bounded recall for a hook, deduplicated against the brief; empty on any failure.

    SessionStart passes the brief it just injected, which resets the session's
    brief ids. UserPromptSubmit passes the prompt as the query only: its text
    never adds an exclusion.
    """
    from .. import memory_hooks

    try:
        budget = recall_budget()
        if budget is not None and budget < RECALL_MIN_SECONDS:
            return ""  # the brief used the budget: keep the brief, skip recall
        session_id = _session(payload)
        path = ledger_path(target, session_id) if session_id else None
        named = brief_card_ids(brief) if brief is not None else []
        exclude = named
        if path is not None:
            try:

                def note_brief(ledger: dict[str, Any]) -> None:
                    ledger["brief"] = []
                    _append(ledger, "brief", named)

                exclude = update(path, note_brief)["brief"] if brief is not None else load(path)["brief"]
            except OSError:
                exclude = named
        text, shown = memory_hooks.recall_for_hook(
            wired_target=target,
            cwd=_cwd(payload),
            prompt=prompt,
            hub=True,
            exclude=list(exclude),
            audience=audience(),
            timeout=budget,
        )
        if shown and path is not None:
            with contextlib.suppress(OSError):
                update(path, lambda ledger: _append(ledger, "pushed", shown))
        return text
    except Exception:  # noqa: BLE001 - recall must never block a hook
        return ""


def _session_ref(memory_root: Path, session_id: str) -> str | None:
    from .. import memory_recall_log

    key = memory_recall_log.load_or_create_key(memory_root)
    if key is None:
        return None
    return hmac.new(key, _SESSION_REF_LABEL + session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _next_batch(ledger: dict[str, Any]) -> None:
    """Freeze the keys not yet captured into a pending batch with its own event id."""
    from .. import memory_hook_hub
    from ..memory_hub_client import event_id

    if ledger["pending"] is not None:
        return
    start_p, start_o = ledger["captured"]
    pushed = ledger["pushed"][start_p : start_p + memory_hook_hub.CAPTURE_LIST_MAX]
    opened = ledger["opened"][start_o : start_o + memory_hook_hub.CAPTURE_LIST_MAX]
    if pushed or opened:
        ledger["pending"] = {
            "id": event_id(),
            "pushed": pushed,
            "opened": opened,
            "ends": [start_p + len(pushed), start_o + len(opened)],
        }


def _settle(ledger: dict[str, Any], batch_id: str) -> None:
    pending = ledger["pending"]
    if pending is not None and pending["id"] == batch_id:
        ledger["captured"] = list(pending["ends"])
        ledger["pending"] = None


def capture(target: Path, session_id: str, *, budget: float | None = None) -> str:
    """Send this session's uncaptured card keys to the hub. Returns a status word, never raises.

    ``recorded`` and ``refused`` settle the batch. ``unavailable`` and
    ``timeout`` keep it pending for the next Stop, with the same event id so
    the hub's idempotency window absorbs a retry of a batch it already took.
    """
    from .. import memory_hooks
    from ..isolated_exec import module_argv, trusted_cwd
    from ..memory_hub_client import hub_mode

    try:
        timeout = (
            CAPTURE_TIMEOUT_SECONDS
            if budget is None
            else min(CAPTURE_TIMEOUT_SECONDS, budget - CAPTURE_RESERVE_SECONDS)
        )
        if timeout < CAPTURE_MIN_SECONDS:
            return "skipped"
        memory_root, status = memory_hooks.resolve_memory_recall_target(target)
        if status != "active" or memory_root is None or hub_mode(memory_root) == "off":
            return "off"
        path = ledger_path(target, session_id)
        if not path.exists():
            return "empty"
        pending = update(path, _next_batch)["pending"]
        if pending is None:
            return "empty"
        session_ref = _session_ref(memory_root, session_id)
        if session_ref is None:
            return "skipped"
        request = json.dumps(
            {
                "target": str(memory_root),
                "session_ref": session_ref,
                "pushed": pending["pushed"],
                "opened": pending["opened"],
                "client_event_id": pending["id"],
                "timeout": max(timeout - CAPTURE_RESERVE_SECONDS / 2, 0.25),
            },
            sort_keys=True,
        )
        try:
            completed = subprocess.run(
                module_argv("brigade.memory_hook_capture_worker"),
                cwd=trusted_cwd(),
                input=request,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "timeout"
        try:
            result = json.loads(completed.stdout.strip() or "{}")
        except ValueError:
            return "error"
        word = result.get("status") if isinstance(result, dict) else None
        if word in {"recorded", "refused"}:
            update(path, lambda ledger: _settle(ledger, pending["id"]))
        return word if isinstance(word, str) else "error"
    except Exception:  # noqa: BLE001 - the capture must never break a Stop hook
        return "error"


def card_under_roots(memory_root: Path, raw: Path) -> tuple[Path, Path, tuple[int, int]] | None:
    """``(card, root, (st_dev, st_ino))`` when ``raw`` is a card search would list, else None.

    Mirrors ``memory_cmd._iter_cards``: any ``.md`` file at any depth under one
    of the memory-care ``card_roots`` that passes its include and exclude
    paths. A symlinked file, a raw ``..`` part and a path whose real location
    leaves the card root are refused. The identity binds the later read to
    the file validated here.
    """
    from ..memory_cmd import MemoryCareConfig, _path_matches, load_config

    if raw.suffix != ".md" or ".." in raw.parts:
        return None
    try:
        root = memory_root.expanduser().resolve()
        if raw.is_symlink() or not raw.is_file():
            return None
        real = raw.resolve()
        info = os.stat(real)
        config = load_config(root) or MemoryCareConfig()
    except (OSError, ValueError):
        return None
    for root_text in config.card_roots:
        try:
            card_root = (root / root_text).resolve()
        except OSError:
            continue
        if not real.is_relative_to(card_root):
            continue
        rel = real.relative_to(root).as_posix()
        if config.include_paths and not _path_matches(rel, config.include_paths):
            return None
        if config.exclude_paths and _path_matches(rel, config.exclude_paths):
            return None
        return real, root, (int(info.st_dev), int(info.st_ino))
    return None


def _between_validate_and_read() -> None:
    """No-op seam between card validation and the bounded read. Tests swap the file here."""


def read_card_bounded(card: Path, identity: tuple[int, int]) -> str | None:
    """The validated card's text, or None when it changed, is not a regular file or is too large.

    Opens with O_NOFOLLOW and O_NONBLOCK, checks the open file's device and
    inode against the validation (POSIX; Windows opens by path and keeps the
    size bound), and reads at most ``CARD_READ_MAX_BYTES``. The text is decoded
    the way ``Path.read_text`` decodes it for card search.
    """
    try:
        fd = os.open(card, _CARD_OPEN_FLAGS)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CARD_READ_MAX_BYTES:
            return None
        if _POSIX and (int(info.st_dev), int(info.st_ino)) != identity:
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= CARD_READ_MAX_BYTES:
            chunk = os.read(fd, CARD_READ_MAX_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > CARD_READ_MAX_BYTES:
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")  # universal newlines, as read_text


def opened_card_id(card: Path, memory_root: Path, identity: tuple[int, int]) -> str | None:
    """The id card search gives this card: a bounded read, then search's own identity scan.

    ``card_identity_from_text`` reads only the scalar identity fields and never
    evaluates a value, so a card with a huge bracketed field costs a line scan.
    """
    from ..memory_cmd import card_identity_from_text

    _between_validate_and_read()
    text = read_card_bounded(card, identity)
    if text is None:
        return None
    card_id = card_identity_from_text(text, card.relative_to(memory_root).as_posix())
    return card_id if _keys([card_id]) else None


def observe_read(target: Path, payload: dict[str, Any]) -> bool:
    """Record a card opened through the Read tool, under its canonical id. Ids only."""
    from .. import memory_hooks

    tool_input = payload.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    session_id = _session(payload)
    if not isinstance(file_path, str) or not file_path.endswith(".md") or session_id is None:
        return False
    memory_root, status = memory_hooks.resolve_memory_recall_target(target)
    if status != "active" or memory_root is None:
        return False
    raw = Path(file_path)
    if not raw.is_absolute():
        raw = (_cwd(payload) or target) / raw
    found = card_under_roots(memory_root, raw)
    if found is None:
        return False
    card_id = opened_card_id(*found)
    if card_id is None:
        return False
    update(ledger_path(target, session_id), lambda ledger: _append(ledger, "opened", [card_id]))
    return True


def after_event(
    event: str, payload: dict[str, Any], target: Callable[[], Path | None], *, budget: float | None = None
) -> None:
    """Hook-side effects that never change the hook's result: Read observation and the Stop capture.

    It runs last in the event, so it also clears the event's recall deadline.
    """
    _EVENT_DEADLINE[0] = None
    try:
        if event == "PostToolUse" and payload.get("tool_name") == "Read":
            if budget is not None and budget < OBSERVE_MIN_SECONDS:
                return
            tool_input = payload.get("tool_input")
            file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
            if not isinstance(file_path, str) or not file_path.endswith(".md"):
                return
            wired = target()
            if wired is not None:
                observe_read(wired, payload)
        elif event == "Stop":
            session_id = _session(payload)
            wired = target() if session_id else None
            if wired is not None and session_id is not None:
                capture(wired, session_id, budget=budget)
    except Exception:  # noqa: BLE001 - observation is best effort
        return
