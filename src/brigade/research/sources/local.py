# src/brigade/research/sources/local.py
from __future__ import annotations
import glob
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List

_WORD = re.compile(r"[a-z0-9]+")


def _tok(text: str) -> List[str]:
    return _WORD.findall(text.lower())


def _read_text(path: Path) -> str:
    if path.suffix.lower() in {".md", ".txt", ""}:
        try:
            return path.read_text(errors="ignore")
        except OSError:
            return ""
    return ""  # other types (e.g. pdf) skipped here; logged by caller


def _chunk(text: str, chunk_chars: int) -> List[str]:
    if len(text) <= chunk_chars:
        return [text]
    parts: List[str] = []
    cur: List[str] = []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) > chunk_chars and cur:
            parts.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(para)
        size += len(para)
    if cur:
        parts.append("\n\n".join(cur))
    return parts


class LexicalIndex:
    def __init__(self, chunks: List[Dict[str, str]]):
        self._chunks = chunks
        self._tokens = [_tok(c["text"]) for c in chunks]
        self._tf = [Counter(t) for t in self._tokens]
        self._len = [len(t) or 1 for t in self._tokens]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter = Counter()
        for toks in self._tokens:
            for w in set(toks):
                df[w] += 1
        n = len(chunks) or 1
        self._idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    @property
    def num_chunks(self) -> int:
        return len(self._chunks)

    def search(self, query: str, limit: int = 5, k1: float = 1.5, b: float = 0.75) -> List[Dict[str, str]]:
        q = _tok(query)
        scored = []
        for i, tf in enumerate(self._tf):
            score = 0.0
            for w in q:
                if w not in tf:
                    continue
                idf = self._idf.get(w, 0.0)
                freq = tf[w]
                denom = freq + k1 * (1 - b + b * self._len[i] / self._avg)
                score += idf * (freq * (k1 + 1)) / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [
            {
                "source": self._chunks[i]["source"],
                "title": self._chunks[i]["title"],
                "text": self._chunks[i]["text"],
                "trust": "local",
            }
            for _, i in scored[:limit]
        ]


def build_index(patterns: List[str], chunk_chars: int = 4000) -> LexicalIndex:
    chunks: List[Dict[str, str]] = []
    seen = set()
    for pat in patterns:
        for fp in glob.glob(os.path.expanduser(pat), recursive=True):
            p = Path(fp)
            if not p.is_file() or fp in seen:
                continue
            seen.add(fp)
            text = _read_text(p)
            if not text.strip():
                continue
            for chunk in _chunk(text, chunk_chars):
                chunks.append({"source": fp, "title": p.name, "text": chunk})
    return LexicalIndex(chunks)
