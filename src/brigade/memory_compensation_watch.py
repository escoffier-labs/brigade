"""Scheduled watch transport. The hub proves and applies every compensation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_cmd
from . import memory_evidence_log as evidence
from . import memory_hub_client as hubc
from . import memory_proposal_io as io

CAPABILITY = "automatic_compensation"


def negative_signals(target: Path, heads: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    """Forward only intact failed receipt bindings for the exact replicated bytes."""
    rows, warnings, unsupported = evidence.read_evidence(target)
    if warnings or unsupported:
        raise ValueError("compensation watch evidence log is incomplete")
    selected = {h["card_id"]: h for h in heads if h["scope"] == scope}
    signals = []
    for row in rows:
        head = selected.get(row["card_id"])
        if row["kind"] != "negative-outcome" or head is None:
            continue
        path = target / io.relative(row["card_path"])
        text = io.text(path, limit=io.MAX_CARD_BYTES)
        if text is None or io.sha256(text.encode()) != head["content_hash"]:
            continue
        meta, valid = memory_cmd._parse_frontmatter(text.replace("\r\n", "\n"))
        if not valid or meta.get("scope", "repo") != scope:
            continue
        if (
            evidence.negative_outcome_binding(
                target, card_path=row["card_path"], card_text=text, ref=row["evidence_id"], timestamp=row["ts"]
            )
            is None
        ):
            continue
        stamp = evidence._timestamp(row["ts"])
        assert stamp is not None
        signals.append(
            {"card_id": row["card_id"], "observed_ms": int(stamp.timestamp() * 1000), "evidence_id": row["evidence_id"]}
        )
    if len(signals) > 64:
        raise ValueError("compensation watch negative signal bound exceeded")
    return signals


def run(
    target: Path,
    client: hubc.MemoryHubClient,
    scopes: tuple[str, ...],
    heads: list[dict[str, Any]],
    epoch: str,
) -> list[dict[str, Any]]:
    status = client.call("status")
    if CAPABILITY not in status.get("capabilities", []):
        return []
    results = []
    for scope in scopes:
        fields: dict[str, Any] = {
            "scope": scope,
            "base_epoch": epoch,
            "negative_outcomes": negative_signals(target, heads, scope),
        }
        seen = set()
        for _ in range(32):
            response = client.call("compensation_watch", client_event_id=hubc.event_id(), **fields)
            results.extend(response["results"])
            after = response.get("next_after")
            if after is None:
                break
            if not isinstance(after, str) or hubc.event_id_ms(after) is None or after in seen:
                raise ValueError("invalid compensation watch cursor")
            seen.add(after)
            fields["after"] = after
        else:
            raise ValueError("compensation watch page bound exceeded")
    return results
