"""Read-only filesystem resolver for causal receipt lineage."""

from __future__ import annotations

import json
from itertools import islice
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import causal_receipt, dogfood_cmd, provenance, runs_cmd
from .selection import WRITER_INBOXES

SCHEMA = "brigade.receipts_lineage.v1"
MAX_HOPS = 256
MAX_RESOLUTIONS = MAX_HOPS * 2
MAX_SCAN_ITEMS = 500
MAX_ARTIFACT_BYTES = 1_048_576
MAX_WORKER_RESULTS_BYTES = 64 * MAX_ARTIFACT_BYTES


def _read(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        value = json.loads(raw)
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


def _handoff_roots(target: Path) -> list[Path]:
    roots = {target / inbox for inbox in WRITER_INBOXES.values()}
    roots.add(target / dogfood_cmd.DEFAULT_HANDOFF_INBOX_REL_PATH)
    try:
        config = dogfood_cmd.load_config(target)
    except (OSError, UnicodeError, ValueError):
        config = None
    if config is not None and config.handoff_inbox is not None:
        roots.add(config.handoff_inbox)
    for path in _files(target / ".brigade" / "runs", "*/run.json"):
        run = _read(path)
        inbox = run.get("handoff_inbox") if run else None
        if isinstance(inbox, str) and inbox:
            configured = Path(inbox).expanduser()
            roots.add(configured if configured.is_absolute() else target / configured)
    return sorted(roots)


def _outcome_record(target: Path, artifact_id: str) -> dict[str, Any] | None:
    """Stream the ledger with bounded line reads, retaining only the last match."""
    match = None
    try:
        with (target / "memory" / "outcome" / "records.jsonl").open("rb") as stream:
            while line := stream.readline(MAX_ARTIFACT_BYTES + 1):
                if len(line) > MAX_ARTIFACT_BYTES:
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(MAX_ARTIFACT_BYTES + 1)
                    continue
                try:
                    row = json.loads(line)
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                receipt = _receipt(row)
                subject = receipt.get("subject") if receipt else None
                row_id = subject.get("id") if isinstance(subject, Mapping) else row.get("id")
                # Legacy ledger rows predate subject ids, but retain the writer's identity inputs.
                if row_id is None and isinstance(row.get("evidence_ref"), str):
                    evidence = Path(row["evidence_ref"])
                    if evidence.name in {"receipt.json", "run.json"} and isinstance(row.get("artifact_id"), str):
                        row_id = f"{evidence.parent.name}--{row['artifact_id']}"
                if row_id == artifact_id:
                    match = row
    except OSError:
        return None
    return match


def _resolver(target: Path, *, report: dict[str, Any]):
    runs_root = target / ".brigade" / "runs"
    verify_root = target / ".brigade" / "work" / "verify-runs"
    handoff_roots: list[Path] | None = None

    def resolve(kind: str, artifact_id: str) -> Mapping[str, Any] | None:
        nonlocal handoff_roots
        if not provenance.is_safe_identity_label(artifact_id) or "/" in artifact_id:
            return None
        if kind == causal_receipt.PARENT_MANIFEST_KIND:
            suffix = "-parent-manifest"
            if not artifact_id.endswith(suffix):
                return None
            run_id = artifact_id.removesuffix(suffix)
            manifest = _read(causal_receipt.parent_manifest_path(runs_root / run_id, artifact_id))
            return manifest if manifest is not None and manifest.get("id") == artifact_id else None
        if kind in {"plan", "run", "worker-result", "synthesis"}:
            directory = runs_root / artifact_id.split(":", 1)[0]
            if kind != "worker-result":
                return _receipt(_read(directory / f"{kind}.json"))
            value = _read(directory / "worker-results.json", max_bytes=MAX_WORKER_RESULTS_BYTES)
            results = value.get("results") if value else None
            if not isinstance(results, list):
                return None
            if len(results) > MAX_SCAN_ITEMS:
                report["truncated"] = True
            run_id = artifact_id.split(":", 1)[0]
            for ordinal, result in enumerate(islice(results, MAX_SCAN_ITEMS), start=1):
                if not isinstance(result, Mapping):
                    continue
                worker = result.get("worker")
                if not isinstance(worker, str):
                    continue
                if causal_receipt.worker_result_id(run_id, worker, ordinal) == artifact_id:
                    return _receipt(result) or causal_receipt.build_receipt(
                        subject_kind="worker-result", subject_id=artifact_id
                    )
            return None
        if kind == "verify":
            return _receipt(_read(verify_root / artifact_id / "receipt.json"))
        if kind == "outcome":
            return _receipt(_outcome_record(target, artifact_id))
        if kind == "handoff":
            if handoff_roots is None:
                handoff_roots = _handoff_roots(target)
            for root in handoff_roots:
                value = _read(causal_receipt.handoff_sidecar_path(root / f"{artifact_id}.md"))
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
        return (target / ".brigade" / "work" / "verify-runs" / artifact_id / "receipt.json").is_file()
    if kind == "outcome":
        return _outcome_record(target, artifact_id) is not None
    if kind == "handoff":
        return any((root / f"{artifact_id}.md").is_file() for root in _handoff_roots(target))
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
    target = target.expanduser().resolve()
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "subject": causal_receipt._bound(subject),
        "hops": [],
    }
    if kind == "run":
        run = _read(target / ".brigade" / "runs" / artifact_id / "run.json")
        branch = runs_cmd._lineage(run) if run else None
        if branch is not None and branch.get("kind") == "child":
            result["run_lineage"] = {
                "parent_run_id": causal_receipt._bound(str(branch.get("parent_run_id", ""))),
                "branch_point_event_id": causal_receipt._bound(str(branch.get("branch_point_event_id", ""))),
            }
    raw_resolve = _resolver(target, report=result)
    resolutions = 0

    def resolve(parent_kind: str, parent_id: str) -> Mapping[str, Any] | None:
        nonlocal resolutions
        resolutions += 1
        if resolutions > MAX_RESOLUTIONS:
            return None
        receipt = raw_resolve(parent_kind, parent_id)
        if (
            receipt is not None
            and parent_kind != causal_receipt.PARENT_MANIFEST_KIND
            and receipt.get("subject") != {"kind": parent_kind, "id": parent_id}
        ):
            return None
        return receipt

    start = resolve(kind, artifact_id)
    if start is None:
        if _artifact_exists(target, kind, artifact_id):
            result["message"] = "no lineage recorded"
            return result
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
    result["hops"] = output_hops
    if len(all_hops) > MAX_HOPS or resolutions > MAX_RESOLUTIONS:
        result["truncated"] = True
    if not output_hops:
        result["message"] = "no lineage recorded"
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
        if hop["status"] != "ok" and hop.get("diagnostic"):
            print(f"  {hop['diagnostic']}")
    if result.get("truncated"):
        print("truncated: lineage walk limit reached")
    for key, label in (("parent_run_id", "parent run"), ("branch_point_event_id", "branch point")):
        if key in result.get("run_lineage", {}):
            print(f"{label}: {result['run_lineage'][key]}")
    return 0
