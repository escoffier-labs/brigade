# src/brigade/research/extract.py
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from ..untrusted import wrap_untrusted
from .types import Finding, SourceEnvelope, Trust

EXTRACTOR_PROMPT = """\
You extract only the information relevant to a research goal from a source.

{untrusted_block}

Return ONLY a JSON object:
{{"summary": "1-3 sentences answering the goal from this source, or empty if irrelevant",
  "evidence": "the most relevant quoted snippet(s)"}}
"""

_TRUST_KIND = {
    "local": "retrieved-doc",
    "web": "web",
    "cli": "tool-output",
    "browser": "web",
    "browser-ai": "web",
}

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


def _source_kind(trust: Trust) -> str:
    return _TRUST_KIND[trust]


def extract_finding(
    llm,
    *,
    goal: str,
    source: SourceEnvelope,
    extraction_lane: str = "luna",
    max_content_chars: int = 15000,
    timeout: int = 90,
) -> Optional[Finding]:
    block = wrap_untrusted(
        source.content,
        source_kind=_source_kind(source.trust),
        goal=goal,
        max_chars=max_content_chars,
    )
    prompt = EXTRACTOR_PROMPT.format(untrusted_block=block)
    out = llm.complete([{"role": "user", "content": prompt}], max_tokens=1024, temperature=0.2, timeout=timeout)
    data = _parse_json(out)
    if not isinstance(data, dict):
        raise ValueError("extractor returned invalid JSON")
    summary = str(data.get("summary", ""))
    if is_low_quality(summary):
        return None
    return Finding(
        source_ids=(source.source_id,),
        title=source.uri,
        summary=summary,
        evidence=str(data.get("evidence", ""))[:3000],
        trust=source.trust,
        extraction_lane=extraction_lane,
        extracted_at=datetime.now(timezone.utc).isoformat(),
        parent_source_ids=source.parent_source_ids,
    )
