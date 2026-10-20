"""Read-only filesystem resolver for causal receipt lineage."""

from __future__ import annotations

import json
from itertools import islice
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import causal_receipt, provenance

SCHEMA = "brigade.receipts_lineage.v1"
MAX_HOPS = 256
MAX_RESOLUTIONS = MAX_HOPS * 2
MAX_SCAN_ITEMS = 500
MAX_ARTIFACT_BYTES = 1_048_576


def _read(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _files(root: Path, pattern: str) -> list[Path]:
    try:
        return [path for path in islice(root.glob(pattern), MAX_SCAN_ITEMS) if not path.is_symlink()]
    except OSError:
        return []


def _receipt(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    receipt = causal_receipt.read_causal_receipt(value)
    return receipt if receipt is not None else None


def _resolver(target: Path):
    runs_root = target / ".brigade" / "runs"
    verify_root = target / ".brigade" / "work" / "verify-runs"
    handoff_root = target / "handoffs"
    outcome_path = target / "memory" / "outcome" / "records.jsonl"

    def resolve(kind: str, artifact_id: str) -> Mapping[str, Any] | None:
        if kind == causal_receipt.PARENT_MANIFEST_KIND:
            for run_dir in _files(runs_root, "*"):
                if not run_dir.is_dir():
                    continue
                try:
                    paths = list(islice(run_dir.glob("*-parent-manifest.json"), MAX_SCAN_ITEMS))
                except OSError:
                    continue
                for path in paths:
                    manifest = _read(path)
                    if manifest is not None and manifest.get("id") == artifact_id:
                        return manifest
            return None
        if kind in {"plan", "run", "worker-result", "synthesis"}:
            if not provenance.is_safe_identity_label(artifact_id):
                return None
            directory = runs_root / artifact_id.split(":", 1)[0]
            if kind == "plan":
                value = _read(directory / "plan.json")
                return _receipt(value)
            if kind == "run":
                value = _read(directory / "run.json")
                return _receipt(value)
            if kind == "synthesis":
                value = _read(directory / "synthesis.json")
                return _receipt(value)
            value = _read(directory / "worker-results.json")
            if value is None:
                return None
            results = value.get("results")
            if not isinstance(results, list) or len(results) > MAX_SCAN_ITEMS:
                return None
            run_id = artifact_id.split(":", 1)[0]
            for ordinal, result in enumerate(results, start=1):
                if not isinstance(result, Mapping):
                    continue
                worker = result.get("worker")
                if not isinstance(worker, str):
                    continue
                if causal_receipt.worker_result_id(run_id, worker, ordinal) == artifact_id:
                    return _receipt(result)
            return None
        if kind == "verify":
            for path in _files(verify_root, "*/receipt.json"):
                value = _read(path)
                receipt = _receipt(value)
                subject = receipt.get("subject") if receipt else None
                if isinstance(subject, Mapping) and subject.get("id") == artifact_id:
                    return receipt
            return None
        if kind == "outcome":
            try:
                if outcome_path.stat().st_size > MAX_ARTIFACT_BYTES:
                    return None
                rows = outcome_path.read_text(encoding="utf-8").splitlines()[-MAX_SCAN_ITEMS:]
            except (OSError, UnicodeError):
                return None
            for line in rows:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                receipt = _receipt(value) if isinstance(value, Mapping) else None
                subject = receipt.get("subject") if receipt else None
                if isinstance(subject, Mapping) and subject.get("id") == artifact_id:
                    return receipt
            return None
        if kind == "handoff":
            for path in _files(handoff_root, f"*{causal_receipt.HANDOFF_SIDECAR_SUFFIX}"):
                value = _read(path)
                subject = value.get("subject") if value else None
                if isinstance(subject, Mapping) and subject.get("id") == artifact_id:
                    return value
        return None

    return resolve


def _artifact_exists(target: Path, kind: str, artifact_id: str) -> bool:
    if kind in {"plan", "run", "worker-result", "synthesis"}:
        run_id = artifact_id.split(":", 1)[0]
        directory = target / ".brigade" / "runs" / run_id
        filename = {
            "plan": "plan.json",
            "run": "run.json",
            "worker-result": "worker-results.json",
            "synthesis": "synthesis.json",
        }[kind]
        return (directory / filename).is_file()
    if kind == "verify":
        return any(
            _read(path) is not None for path in _files(target / ".brigade" / "work" / "verify-runs", "*/receipt.json")
        )
    if kind == "outcome":
        return (target / "memory" / "outcome" / "records.jsonl").is_file()
    if kind == "handoff":
        return bool(_files(target / "handoffs", f"*{causal_receipt.HANDOFF_SIDECAR_SUFFIX}"))
    return False


def lineage(*, target: Path, subject: str) -> dict[str, Any]:
    """Resolve one artifact and return a bounded, path-free lineage report."""
    if ":" not in subject:
        raise ValueError("subject must use <kind>:<id>")
    kind, artifact_id = subject.split(":", 1)
    if (
        kind not in causal_receipt.ARTIFACT_KINDS
        or not provenance.is_safe_identity_label(artifact_id)
        or "/" in artifact_id
    ):
        raise ValueError("subject must use a known artifact kind and safe id")
    raw_resolve = _resolver(target.expanduser().resolve())
    resolutions = 0

    def resolve(parent_kind: str, parent_id: str) -> Mapping[str, Any] | None:
        nonlocal resolutions
        resolutions += 1
        if resolutions > MAX_RESOLUTIONS:
            return None
        return raw_resolve(parent_kind, parent_id)

    start = resolve(kind, artifact_id)
    if start is None:
        if _artifact_exists(target.expanduser().resolve(), kind, artifact_id):
            return {
                "schema": SCHEMA,
                "schema_version": 1,
                "subject": causal_receipt._bound(subject),
                "message": "no lineage recorded",
                "hops": [],
            }
        raise FileNotFoundError(f"receipt not found for {causal_receipt._bound(subject)}")
    all_hops = causal_receipt.walk_ancestors(
        start,
        resolve,
        digest_of=causal_receipt.receipt_digest,
    )
    hops = all_hops[:MAX_HOPS]
    output_hops = []
    for hop in hops:
        output_hops.append(
            {
                key: causal_receipt._bound(str(hop[key]))
                for key in ("from", "relation", "kind", "id", "link", "status", "diagnostic")
                if key in hop and hop[key] is not None
            }
        )
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "subject": causal_receipt._bound(subject),
        "hops": output_hops,
    }
    if len(all_hops) > MAX_HOPS or resolutions > MAX_RESOLUTIONS:
        result["truncated"] = True
    if not output_hops:
        result["message"] = "no lineage recorded"
    if kind == "run":
        run = _read(target.expanduser().resolve() / ".brigade" / "runs" / artifact_id / "run.json")
        branch = run.get("lineage") if run else None
        if isinstance(branch, Mapping) and branch.get("kind") == "child":
            result["run_lineage"] = {
                "parent_run_id": causal_receipt._bound(str(branch.get("parent_run_id", ""))),
                "branch_point_event_id": causal_receipt._bound(str(branch.get("branch_point_event_id", ""))),
            }
    return result


def command(*, target: Path, subject: str, json_output: bool = False) -> int:
    try:
        result = lineage(target=target, subject=subject)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {causal_receipt._bound(str(exc))}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(result, sort_keys=True))
        return 0
    print(f"lineage for {result['subject']}")
    if result.get("message"):
        print(result["message"])
    for hop in result["hops"]:
        relation = hop.get("relation", "parent_manifest")
        print(f"{relation} {hop.get('kind', '')}:{hop.get('id', '')} [{hop.get('link', 'recorded')}] {hop['status']}")
    for key, label in (("parent_run_id", "parent run"), ("branch_point_event_id", "branch point")):
        if key in result.get("run_lineage", {}):
            print(f"{label}: {result['run_lineage'][key]}")
    return 0
