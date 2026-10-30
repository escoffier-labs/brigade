"""Adapt bounded project preview evidence without discovering instruction files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from . import context_preview
from .bug_diagnose import _safe
from .bug_redaction import Redactor, assert_clean, assert_terminal_safe

_SETTING_KEYS = ("project_doc_max_bytes", "fallback_filenames", "root_markers", "trust", "read_access")
_CONTRIBUTIONS = {
    "loaded": "Selected file contributes in chain order. Ancestor contributions remain.",
    "empty": "Selected file has no non-whitespace contribution within its read budget.",
    "truncated": "Selected file contributes partially within the supplied project byte cap.",
    "cap_exhausted": "Earlier contributions exhausted the project byte cap. This selected file contributes no bytes.",
    "unknown": "The producer could not establish this file's contribution.",
}


def _label(value: str) -> None:
    assert_terminal_safe(value)
    if (
        not value
        or len(value) > 4096
        or "\\" in value
        or Path(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("invalid relative label")


def _filenames(raw: str | None, *, fallback: bool) -> list[str] | None:
    if raw is None:
        return None
    # Bound the raw argument before parsing, including whitespace-only attacks.
    if len(raw) > 16384:
        raise ValueError("setting resource limit")
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) > context_preview.RESOURCE_MAX_LIST_ENTRIES:
        raise ValueError("invalid filename list")
    for value in values:
        if not isinstance(value, str):
            raise ValueError("invalid filename")
        name = value.strip(context_preview.RUST_WHITESPACE) if fallback else value
        _label(name)
        if "/" in name:
            raise ValueError("basename required")
    return values


def compare(args: argparse.Namespace) -> dict[str, Any]:
    """Model only explicitly supplied project scope. Actual session is unknown."""
    if args.harness != "codex" or args.name is not None:
        raise ValueError("Codex project instructions required")
    for value in (args.target, args.cwd):
        if value is None or not value.is_absolute():
            raise ValueError("explicit absolute scope required")
        assert_terminal_safe(str(value))
        if any(ord(char) < 32 for char in str(value)):
            raise ValueError("unsafe scope")
    if any(getattr(args, key) is None for key in ("codex_version", "trust", "read_access")):
        raise ValueError("explicit settings required")
    expected = args.instruction_path
    if expected is not None:
        _label(expected)
    cap = args.project_doc_max_bytes
    if cap is not None:
        if len(cap) > 16:
            raise ValueError("setting resource limit")
        cap = int(cap)
    fallbacks = _filenames(args.fallback_filenames, fallback=True)
    markers = _filenames(args.root_markers, fallback=False)
    evidence = context_preview.preview(
        target=args.target,
        cwd=args.cwd,
        codex_version=args.codex_version,
        trust=args.trust,
        read_access=args.read_access,
        assume_codex_defaults=args.assume_codex_defaults,
        project_doc_max_bytes=cap,
        fallback_filenames=fallbacks,
        root_markers=markers,
    )
    redactor = Redactor(args.target)

    def safe_label(value: str) -> str:
        return "/".join(_safe(redactor, part) for part in value.split("/"))

    # Explicit allowlist: never serialize producer extras, file bodies, global
    # scope, raw settings, or raw-file digests. Hash only this public projection.
    settings = {}
    for key in _SETTING_KEYS:
        item = evidence["settings"][key]
        value = item["value"]
        if isinstance(value, list):
            value = [_safe(redactor, name) for name in value]
        settings[key] = {"value": value, "source": item["source"]}
    complete = evidence["status"] == "complete"
    findings: list[dict[str, Any]] = []
    if not complete:
        findings.append(
            {
                "status": "not_evaluated",
                "expected": "A complete modeled project instruction chain.",
                "observed": "Unsupported assumptions or incomplete inspection. Instruction selection remains unknown.",
            }
        )
    elif settings["trust"]["value"] == "untrusted" or settings["project_doc_max_bytes"]["value"] == 0:
        findings.append(
            {
                "status": "disabled",
                "expected": "Project loading according to supplied settings.",
                "observed": "Supplied untrusted trust or zero byte cap excludes project instruction loading.",
            }
        )
    else:
        for row in evidence["files"]:
            if row["scope"] != "project":
                continue
            contribution = row["contribution"]
            findings.append(
                {
                    "path": safe_label(row["path"]),
                    "status": contribution,
                    "expected": "One selected filename per directory, in ancestor-to-cwd chain order.",
                    "observed": _CONTRIBUTIONS[contribution],
                    **{
                        key: row[key]
                        for key in (
                            "selection_order",
                            "size_bytes",
                            "consumed_raw_bytes",
                            "rendered_utf8_bytes",
                            "cumulative_selected_bytes",
                        )
                    },
                }
            )
        if not findings:
            findings.append(
                {
                    "status": "no_selected_files",
                    "expected": "A modeled project instruction chain, if selected.",
                    "observed": "Complete modeled inspection selected no project instruction files.",
                }
            )
    if complete and expected is not None:
        selected = any(row["scope"] == "project" and row["path"] == expected for row in evidence["files"])
        if not selected:
            findings.append(
                {
                    "path": safe_label(expected),
                    "status": "not_selected",
                    "expected": "Caller expected this project-relative label in the selected chain.",
                    "observed": "Expected label is not selected. Existence was not checked separately and no cause is established.",
                }
            )
    projection = {
        "component": "instructions",
        "harness": "codex",
        "codex_version": evidence["consumer"]["version"],
        "settings": settings,
        "findings": findings,
    }
    digest = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    symptom = _safe(redactor, args.summary)
    uncertainty = (
        "Supplied configuration and fresh-disk modeling only. Actual session loading and model behavior unknown. "
        "No atomic snapshot. Access times may change."
    )
    next_check = (
        "Confirm the selected project, explicit cwd, caller-supplied Codex version and effective settings locally. "
        "Then use brigade context preview with those same explicit project settings."
    )
    text = f"Symptom: {symptom}\nHarness: codex\nComponent: instructions\nEvidence digest: {digest}\n"
    text += "Expected: AGENTS.override.md precedes AGENTS.md, then supplied fallbacks, in each selected directory.\n"
    text += "Modeled settings: " + json.dumps(settings, sort_keys=True) + "\n"
    text += "\n".join(
        f"Instruction: {finding.get('path', '(chain)')} | {finding['status']}\nObserved: {finding['observed']}"
        for finding in findings
    )
    text += f"\nUncertainty: {uncertainty}\nNext check (preview only): {next_check}"
    text += "\nactual_session_observed: false\nruntime: unknown\nverification: not-run"
    result = {
        **projection,
        "status": "available" if complete else "unavailable",
        "symptom": symptom,
        "evidence_digest": digest,
        "actual_session_observed": False,
        "runtime": "unknown",
        "verification": "not-run",
        "uncertainty": uncertainty,
        "next_check": next_check,
        "text": text,
    }
    assert_terminal_safe(result)
    assert_clean(json.dumps(result, ensure_ascii=False))
    return result
