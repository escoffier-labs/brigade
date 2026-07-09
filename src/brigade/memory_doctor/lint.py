"""Lint verb: dead [[wiki-link]] scanner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from brigade.memory_doctor.parsing import extract_wiki_links
from brigade.memory_doctor.paths import PathConfig


@dataclass(frozen=True)
class DeadLink:
    source: Path
    link: str
    suggestion: str | None


def _card_paths(memory_dir: Path) -> list[Path]:
    """Card files under memory_dir, including the Brigade memory/cards/ layout."""
    paths: list[Path] = []
    cards_dir = memory_dir / "cards"
    if cards_dir.is_dir():
        paths.extend(sorted(cards_dir.glob("*.md")))
    paths.extend(sorted(p for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"))
    return paths


def _existing_card_slugs(memory_dir: Path) -> set[str]:
    return {p.stem.lower() for p in _card_paths(memory_dir)}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def suggest_closest(needle: str, pool: list[str], max_distance: int = 3) -> str | None:
    n = needle.lower()
    best: tuple[int, str] | None = None
    for cand in pool:
        d = _levenshtein(n, cand.lower())
        if d <= max_distance and (best is None or d < best[0]):
            best = (d, cand)
    return best[1] if best else None


def scan_dead_links(memory_dir: Path) -> list[DeadLink]:
    slugs = _existing_card_slugs(memory_dir)
    pool = sorted(slugs)
    out: list[DeadLink] = []
    for p in _card_paths(memory_dir):
        text = p.read_text(errors="replace")
        for raw_link in extract_wiki_links(text):
            slug = raw_link.lower().removesuffix(".md")
            # Accept wiki links that include a cards/ prefix.
            slug = slug.removeprefix("cards/")
            if slug in slugs:
                continue
            suggestion = suggest_closest(slug, pool)
            out.append(DeadLink(source=p, link=raw_link, suggestion=suggestion))
    return out


def count_dead_links(memory_dir: Path) -> int:
    return len(scan_dead_links(memory_dir))


def run(cfg: PathConfig) -> int:
    findings = scan_dead_links(cfg.memory_dir)
    if not findings:
        print("brigade memory lint: 0 dead links")
        return 0
    current = None
    for f in findings:
        if f.source != current:
            print(f"\n{f.source.name}")
            current = f.source
        sug = f"  (did you mean {f.suggestion}?)" if f.suggestion else ""
        print(f"  [[{f.link}]] - no card found{sug}")
    print(f"\nbrigade memory lint: {len(findings)} dead link(s)")
    return 1
