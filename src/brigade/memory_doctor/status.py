"""Status verb: read-only summary of memory health."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from brigade.memory_doctor.paths import PathConfig
from brigade.memory_doctor.lint import count_dead_links


@dataclass(frozen=True)
class Status:
    memory_dir: str
    handoffs_dir: str
    cards: int
    memory_index_lines: int
    memory_index_bytes: int
    pending_handoffs: int
    processed_handoffs: int
    dead_links: int
    oldest_pending_age_days: float | None
    over_threshold: bool
    max_lines: int
    over_bytes: bool
    max_bytes: int


def _count_cards(memory_dir: Path) -> int:
    from brigade.memory_doctor.lint import _card_paths

    return len(_card_paths(memory_dir))


def _index_stats(memory_dir: Path) -> tuple[int, int]:
    # MEMORY.md may live at memory/MEMORY.md (memory-doctor layout) or repo root.
    candidates = [memory_dir / "MEMORY.md", memory_dir.parent / "MEMORY.md"]
    p = next((c for c in candidates if c.is_file()), None)
    if p is None:
        return 0, 0
    raw = p.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.count("\n") if text and not text.endswith("\n") else text.count("\n")
    if text and not text.endswith("\n"):
        lines += 1
    return lines, len(raw)


def _handoff_counts(handoffs_dir: Path) -> tuple[int, int, float | None]:
    if not handoffs_dir.is_dir():
        return 0, 0, None
    pending = [p for p in handoffs_dir.glob("*.md")]
    processed = list((handoffs_dir / "processed").glob("*.md")) if (handoffs_dir / "processed").is_dir() else []
    oldest_age: float | None = None
    if pending:
        now = time.time()
        oldest_mtime = min(p.stat().st_mtime for p in pending)
        oldest_age = (now - oldest_mtime) / 86400.0
    return len(pending), len(processed), oldest_age


def collect_status(cfg: PathConfig) -> Status:
    cards = _count_cards(cfg.memory_dir)
    index_lines, index_bytes = _index_stats(cfg.memory_dir)
    pending, processed, oldest = _handoff_counts(cfg.handoffs_dir)
    dead = count_dead_links(cfg.memory_dir)
    return Status(
        memory_dir=str(cfg.memory_dir),
        handoffs_dir=str(cfg.handoffs_dir),
        cards=cards,
        memory_index_lines=index_lines,
        memory_index_bytes=index_bytes,
        pending_handoffs=pending,
        processed_handoffs=processed,
        dead_links=dead,
        oldest_pending_age_days=oldest,
        over_threshold=index_lines > cfg.max_lines,
        max_lines=cfg.max_lines,
        over_bytes=index_bytes > cfg.max_bytes,
        max_bytes=cfg.max_bytes,
    )


def format_status_human(s: Status) -> str:
    lines = [
        f"memory dir:       {s.memory_dir}",
        f"  cards:          {s.cards}",
        f"  MEMORY.md:      {s.memory_index_lines} lines, {s.memory_index_bytes} bytes",
        f"  lines:          {s.memory_index_lines} / {s.max_lines} ({'OVER' if s.over_threshold else 'ok'})",
        f"  bytes:          {s.memory_index_bytes} / {s.max_bytes} ({'OVER' if s.over_bytes else 'ok'})",
        f"  dead links:     {s.dead_links}",
        "",
        f"handoffs dir:     {s.handoffs_dir}",
        f"  pending:        {s.pending_handoffs}",
        f"  processed:      {s.processed_handoffs}",
    ]
    if s.oldest_pending_age_days is not None:
        lines.append(f"  oldest pending: {s.oldest_pending_age_days:.1f} days")
    return "\n".join(lines)


def format_status_json(s: Status) -> str:
    return json.dumps(asdict(s), indent=2)


def run(cfg: PathConfig, *, as_json: bool = False) -> int:
    s = collect_status(cfg)
    print(format_status_json(s) if as_json else format_status_human(s))
    return 0
