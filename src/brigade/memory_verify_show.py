"""Authenticated, terminal-only review of local verify packets. Never writes or decides."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

from . import memory_hub_client as hubc
from . import memory_proposal_io as io
from . import memory_verify_cmd as cmd
from . import memory_verify_evidence as ev
from . import memory_verify_packets as packets
from .memory_verify_run import APPLICABLE_MANIFESTS

MISMATCH = "verify_review_mismatch"


def _fail() -> NoReturn:
    raise cmd.VerifyRunError(MISMATCH, "the local manifest or packet does not match the proposal's hash chain")


def _local(target: Path, proposal_id: str) -> tuple[bytes, bytes]:
    """Read an open or retained terminal pair without creating any files."""
    try:
        with io.directory(target / packets.PACKETS) as fd:
            names = [name for name, _ in packets.listing(fd)]
            choices = [
                name
                for name in names
                if name == f"{proposal_id}.json"
                or ((match := packets.DONE_PACKET.fullmatch(name)) is not None and match[1] == proposal_id)
            ]
            if len(choices) != 1:
                _fail()
            packet_name = choices[0]
            manifest_name = packet_name.removesuffix(".json") + ".manifest.json"
            manifest = packets.read(fd, manifest_name)
            packet = packets.read(fd, packet_name)
            if manifest is None or packet is None:
                _fail()
            assert manifest is not None and packet is not None
            return manifest, packet
    except (OSError, ValueError):
        _fail()
    raise AssertionError("unreachable")


def _file_status(roots: ev.Roots, scope: str, item: dict[str, Any]) -> str | None:
    ref = item["ref"]
    # Bundles and related excerpts have no local file to re-resolve.
    if ev.bundle_ref(ref) is not None or ev._evidence_root(ref) is None:
        return None
    found = ev._locate(roots, scope, ref)
    if found is None:
        return "missing"
    _, raw, _ = found
    digest = item.get("file_sha256")
    # Legacy packets did not retain a full-file digest, so they cannot prove freshness.
    return "current" if isinstance(digest, str) and io.sha256(raw) == digest else "changed"


def show_payload(target: Path, proposal_id: str) -> dict[str, Any]:
    if not hubc.event_id_ms(proposal_id):
        raise cmd.VerifyRunError("verify_review_id", "review needs a valid proposal id")
    _, client = cmd._hub(target)
    shown = client.call("proposal_show", proposal_id=proposal_id)
    listed = shown.get("verifier_manifests")
    if not isinstance(listed, list):
        raise cmd.VerifyRunError("verify_review_forbidden", "review permission on every proposal target is required")
    manifest_bytes, packet_bytes = _local(target, proposal_id)
    digest = io.sha256(manifest_bytes)
    matches = [
        m
        for m in listed
        if isinstance(m, dict)
        and m.get("artifact_kind") == "memory-verify-manifest"
        and m.get("state") in APPLICABLE_MANIFESTS
        and m.get("artifact_sha256") == digest
    ]
    if len(matches) != 1:
        _fail()
    try:
        manifest = json.loads(manifest_bytes)
        if manifest["schema"] != "brigade.memory-verify-manifest.v1" or manifest["packet_sha256"] != io.sha256(
            packet_bytes
        ):
            _fail()
        packet = json.loads(packet_bytes)
        if packet["schema"] != "brigade.memory-verify-packet.v1" or packet["card"] != manifest["card"]:
            _fail()
        scope = packet["card"]["scope"]
        settings = cmd.configured(target)
        if scope not in settings.scopes():
            raise cmd.VerifyRunError("verify_review_scope", "the packet's scope has no bound workspace")
        evidence = []
        with ev.Roots(settings) as roots:
            for item in packet["evidence"]:
                bound = [
                    m
                    for m in manifest["evidence"]
                    if all(m.get(key) == item.get(key) for key in ("ref", "kind", "sha256"))
                ]
                quotes = []
                for source in bound:
                    for start, end in source["ranges"]:
                        quotes.append(item["text"].encode()[start:end].decode())
                evidence.append(
                    {
                        "ref": item["ref"],
                        "kind": item["kind"],
                        "rank": item["rank"],
                        "authenticated": item["authenticated"],
                        "counted": bool(quotes) and item["counted"] is True,
                        "findings": [],
                        "file_status": _file_status(roots, scope, item),
                        "quotes": quotes,
                    }
                )
        return {
            "status": "completed",
            "proposal_id": proposal_id,
            "card": packet["card"],
            "evidence": evidence,
            "findings": packet["findings"],
            "blocks": manifest["blocks"],
        }
    except (KeyError, TypeError, IndexError, AttributeError, UnicodeError, RecursionError):
        _fail()
    raise AssertionError("unreachable")


def print_review(payload: dict[str, Any]) -> None:
    """Headers always precede quotes. Control characters are escaped for the terminal."""
    for finding in payload.get("findings", []):
        print("dropped: " + json.dumps(finding, ensure_ascii=True, sort_keys=True))
    for item in payload.get("evidence", []):
        print(
            f"evidence {json.dumps(item['ref'], ensure_ascii=True)} "
            f"kind={json.dumps(item['kind'], ensure_ascii=True)} rank={json.dumps(item['rank'], ensure_ascii=True)} "
            f"authenticated={json.dumps(item['authenticated'], ensure_ascii=True)} counted={item['counted']} "
            f"findings={json.dumps(item['findings'])} file={item['file_status'] or 'not_local'}"
        )
        for quote in item["quotes"]:
            print("quote: " + json.dumps(quote, ensure_ascii=True)[1:-1].replace('\\"', '"'))
