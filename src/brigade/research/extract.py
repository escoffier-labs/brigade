# src/brigade/research/extract.py
from __future__ import annotations
import json
import re
from typing import Optional
from .types import Finding, Trust
from ..untrusted import wrap_untrusted

EXTRACTOR_PROMPT = """\
You extract only the information relevant to a research goal from a source.

{untrusted_block}

Return ONLY a JSON object:
{{"summary": "1-3 sentences answering the goal from this source, or empty if irrelevant",
  "evidence": "the most relevant quoted snippet(s)"}}
"""

_TRUST_KIND = {"local": "retrieved-doc", "web": "web", "cli": "tool-output", "browser": "web"}

_LOW = ("does not contain", "no relevant", "not relevant", "irrelevant", "no information", "cannot find", "n/a")


def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                return None
    return None


def is_low_quality(summary: str) -> bool:
    s = (summary or "").strip().lower()
    return (not s) or any(p in s for p in _LOW)


def extract_finding(
    llm,
    *,
    goal: str,
    source: str,
    title: str,
    content: str,
    trust: Trust,
    max_content_chars: int = 15000,
    timeout: int = 90,
) -> Optional[Finding]:
    block = wrap_untrusted(content, source_kind=_TRUST_KIND[trust], goal=goal, max_chars=max_content_chars)
    prompt = EXTRACTOR_PROMPT.format(untrusted_block=block)
    out = llm.complete([{"role": "user", "content": prompt}], max_tokens=1024, temperature=0.2, timeout=timeout)
    data = _parse_json(out)
    if not data:
        return None
    summary = str(data.get("summary", ""))
    if is_low_quality(summary):
        return None
    return Finding(
        source=source,
        title=title or source,
        summary=summary,
        evidence=str(data.get("evidence", ""))[:3000],
        trust=trust,
    )
