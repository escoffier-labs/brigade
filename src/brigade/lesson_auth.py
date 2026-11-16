"""Confined workspace HMAC authority for hook observations."""

from __future__ import annotations

import hmac
import math
from pathlib import Path
from typing import Any

from . import localio, receipt_signing

DOMAIN = "brigade.lesson-observation.v1\n"


def _digest(target: Path, event: dict[str, Any]) -> str:
    return localio.canonical_json_digest(
        {"target": str(target.resolve()), "event": {k: v for k, v in event.items() if k != "authentication"}}
    )


def stamp(target: Path, event: dict[str, Any]) -> None:
    """Use exactly the fixed-path, descriptor-confined receipt signing reader."""
    key = receipt_signing.load_workspace_key(target)
    if key is not None:
        event["authentication"] = {
            "key_id": key[1],
            "signature": receipt_signing.sign(DOMAIN + _digest(target, event), key[0]),
        }


def authentic(target: Path, event: dict[str, Any]) -> bool:
    key = receipt_signing.load_workspace_key(target)
    auth = event.get("authentication")
    if key is None or not isinstance(auth, dict):
        return False
    signature = auth.get("signature")
    return (
        type(event.get("id")) is int
        and event["id"] > 0
        and type(event.get("append_sequence")) is int
        and event["append_sequence"] > 0
        and all(
            type(event.get(field)) in (int, float) and math.isfinite(event[field])
            for field in ("at", "earliest_unreviewed_at")
        )
        and auth.get("key_id") == key[1]
        and isinstance(signature, str)
        and signature.isascii()
        and hmac.compare_digest(signature, receipt_signing.sign(DOMAIN + _digest(target, event), key[0]))
    )


def prompt_matches(target: Path, event: dict[str, Any], prompt: dict[str, Any]) -> bool:
    """A saved id list cannot stand in for the signed event's exact bytes."""
    return (
        event.get("kind") == "UserPromptSubmit"
        and event.get("prompt_id") == prompt["prompt_id"]
        and event.get("prompt_digest") == localio.canonical_json_digest(prompt["prompt"])
        and event.get("text") == prompt["prompt"]
        and event.get("at") == prompt["at"]
        and authentic(target, event)
    )


def seal_pending(target: Path, pending: dict[str, Any]) -> None:
    key = receipt_signing.load_workspace_key(target)
    if key is None:
        raise ValueError("lesson_review_needs_key")
    pending["authentication"] = {
        "key_id": key[1],
        "signature": receipt_signing.sign("brigade.lesson-publication.v1\n" + _digest(target, pending), key[0]),
    }


def authentic_pending(target: Path, pending: dict[str, Any]) -> bool:
    if not isinstance(pending, dict):
        return False
    key = receipt_signing.load_workspace_key(target)
    auth = pending.get("authentication")
    if key is None or not isinstance(auth, dict):
        return False
    signature = auth.get("signature")
    return (
        auth.get("key_id") == key[1]
        and isinstance(signature, str)
        and signature.isascii()
        and hmac.compare_digest(
            signature, receipt_signing.sign("brigade.lesson-publication.v1\n" + _digest(target, pending), key[0])
        )
    )
