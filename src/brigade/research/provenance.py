# src/brigade/research/provenance.py
from __future__ import annotations

import re
from collections.abc import Sequence

from .types import CitationAudit, CitationRecord, Finding

CITATION_PATTERN = re.compile(r"\[source:(src-[a-f0-9]{16})\]")


def citation_token(source_id: str) -> str:
    return f"[source:{source_id}]"


def audit_citations(report: str, findings: Sequence[Finding]) -> CitationAudit:
    known = {source_id for finding in findings for source_id in finding.source_ids}
    seen = tuple(dict.fromkeys(CITATION_PATTERN.findall(report)))
    citations = tuple(
        CitationRecord(
            token=citation_token(source_id),
            source_ids=(source_id,),
            status="accepted" if source_id in known else "unresolved",
        )
        for source_id in seen
    )
    unresolved = tuple(source_id for source_id in seen if source_id not in known)
    return CitationAudit(accepted=bool(seen) and not unresolved, citations=citations, unresolved=unresolved)
