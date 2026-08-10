"""Human table and JSON formatting for the retrieval eval report."""

from __future__ import annotations

import json
from typing import Any


def _fmt_float(value: float | None, *, digits: int = 3) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _fmt_rank(value: float | None) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _row(
    label: str,
    block: dict[str, Any] | None,
    *,
    skipped: bool = False,
    reason: str | None = None,
) -> str:
    if skipped or block is None:
        note = reason or "skipped"
        return f"{label:<28} {'SKIP':>8} {'':>8} {'':>8} {'':>8}  {note}"
    return (
        f"{label:<28} "
        f"{_fmt_float(block['precision_at_k']):>8} "
        f"{_fmt_float(block['recall_at_k']):>8} "
        f"{_fmt_float(block['hit_rate']):>8} "
        f"{_fmt_rank(block.get('mean_first_gold_rank')):>8}"
    )


def format_table(report: dict[str, Any]) -> str:
    """Render a compact per-adapter / per-category table."""
    k = report["k"]
    corpus = report["corpus"]
    lines = [
        f"memory retrieval eval  k={k}  cards={corpus['card_count']}  queries={corpus['query_count']}",
        f"fixture: {report['fixture_root']}",
        "",
        f"{'adapter / slice':<28} {'P@k':>8} {'R@k':>8} {'hit':>8} {'mean#1':>8}",
        "-" * 68,
    ]

    ceiling = report["ceiling"]["overall"]
    lines.append(_row("ceiling (oracle)", ceiling))

    adapters: dict[str, Any] = report["adapters"]
    for name in adapters:
        payload = adapters[name]
        if payload.get("skipped"):
            lines.append(_row(name, None, skipped=True, reason=str(payload.get("reason") or "skipped")))
            continue
        lines.append(_row(name, payload["overall"]))
        for category, block in sorted(payload["by_category"].items()):
            lines.append(_row(f"  {name}/{category}", block))

    projection = report.get("projection") or {}
    projection_adapters: dict[str, Any] = projection.get("adapters") or {}
    if projection_adapters:
        lines.extend(["", "projection eval (#845 V1)", f"{'adapter':<28} {'pass':>8} {'fail':>8} {'scope!':>8}"])
        lines.append("-" * 68)
        for name, block in projection_adapters.items():
            if block.get("skipped"):
                lines.append(_row(name, None, skipped=True, reason=str(block.get("reason") or "skipped")))
                continue
            summary = block.get("summary") or {}
            lines.append(
                f"{name:<28} {summary.get('passed', 0):>8} {summary.get('failed', 0):>8} "
                f"{summary.get('report_level_failure_count', 0):>8}"
            )

    lines.append("")
    lines.append(
        "Gate reminder: semantic lands only if it beats grep on paraphrase/cross_tag, "
        "not merely on aggregate exact-match ties."
    )
    return "\n".join(lines) + "\n"


def format_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
