from __future__ import annotations

import json
from dataclasses import asdict

from .types import GuardResult


def to_payload(result: GuardResult) -> dict:
    return {
        "ok": not result.blocked,
        "blocked": result.blocked,
        "changed": result.changed,
        "counts_by_action": result.counts_by_action(),
        "counts_by_category": result.counts_by_category(),
        "findings": [asdict(finding) for finding in result.findings],
    }


def to_json(result: GuardResult) -> str:
    return json.dumps(to_payload(result), indent=2, sort_keys=True)


def to_text(result: GuardResult, *, path: str = "<stdin>") -> str:
    if not result.findings:
        return f"Clean. {path}: no findings."

    lines = [
        f"{path}: {len(result.findings)} finding(s), blocked={str(result.blocked).lower()}, changed={str(result.changed).lower()}"
    ]
    for finding in result.findings:
        allow_note = f" allowed_by={finding.allowed_by}" if finding.allowed_by else ""
        lines.append(
            f"  L{finding.line}:{finding.column} {finding.action.upper()} "
            f"{finding.category}/{finding.rule_id} source={finding.source}{allow_note}: {finding.match!r}"
        )
        if finding.message:
            lines.append(f"    {finding.message}")
    return "\n".join(lines)
