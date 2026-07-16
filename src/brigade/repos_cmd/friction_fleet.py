from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from .. import friction_cmd
from ..localio import read_json_dict as _read_json, utc_now as _now, write_text_atomic
from ..render import emit
from . import constants, fleet


def _friction_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "friction"


def _latest_json_path(target: Path) -> Path:
    return _friction_root(target) / "latest.json"


def _empty_source_families() -> dict[str, dict[str, int]]:
    return {
        name: {"accepted": 0, "rejected": 0, "grouped": 0, "truncated": 0, "skipped": 0}
        for name in friction_cmd.SOURCE_FAMILIES
    }


def _source_families_from_scan(scan: dict[str, Any]) -> dict[str, dict[str, int]]:
    families = _empty_source_families()
    counts = scan.get("counts")
    by_family = counts.get("by_source_family") if isinstance(counts, dict) else None
    if not isinstance(by_family, dict):
        return families
    for name in friction_cmd.SOURCE_FAMILIES:
        values = by_family.get(name)
        if not isinstance(values, dict):
            continue
        for key in ("accepted", "rejected", "grouped", "truncated"):
            raw = values.get(key)
            if isinstance(raw, int):
                families[name][key] = raw
        families[name]["skipped"] = families[name]["rejected"] + families[name]["truncated"]
    return families


def _failed_repo_row(entry: constants.RepoEntry, *, detail: str) -> dict[str, Any]:
    return {
        "repo_id": entry.repo_id,
        "label": entry.label,
        "status": "failed",
        "detail": detail,
        "files_scanned": 0,
        "files_skipped": 0,
        "candidate_count": 0,
        "source_families": _empty_source_families(),
    }


def _completed_repo_row(entry: constants.RepoEntry, scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo_id": entry.repo_id,
        "label": entry.label,
        "status": "completed",
        "files_scanned": int(scan.get("files_scanned") or 0),
        "files_skipped": int(scan.get("files_skipped") or 0),
        "candidate_count": int(scan.get("candidate_count") or 0),
        "source_families": _source_families_from_scan(scan),
    }


def _signature_key(record: dict[str, Any]) -> str:
    recurrence_key = record.get("recurrence_key")
    if isinstance(recurrence_key, str) and recurrence_key.strip():
        return recurrence_key
    record_id = record.get("id")
    if isinstance(record_id, str) and record_id.strip():
        return record_id
    candidate_id = record.get("candidate_id")
    if isinstance(candidate_id, str) and candidate_id.strip():
        return candidate_id
    return "unknown"


def _occurrence_count(candidate: dict[str, Any]) -> int:
    evidence = candidate.get("evidence")
    if isinstance(evidence, dict):
        children = evidence.get("children")
        if isinstance(children, list) and children:
            return len(children)
    return 1


def _safe_summary(candidate: dict[str, Any]) -> str:
    text = str(candidate.get("text") or candidate.get("title") or "").strip()
    if not text:
        evidence = candidate.get("evidence")
        if isinstance(evidence, dict):
            text = str(evidence.get("snippet") or "").strip()
    return friction_cmd._short(text, 180)


def _candidate_mentions_repo(candidate: dict[str, Any], entry: constants.RepoEntry) -> bool:
    haystacks: list[str] = []
    text = candidate.get("text")
    if isinstance(text, str):
        haystacks.append(text)
    evidence = candidate.get("evidence")
    if isinstance(evidence, dict):
        for key in ("path", "snippet"):
            value = evidence.get(key)
            if isinstance(value, str):
                haystacks.append(value)
        children = evidence.get("children")
        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                for key in ("path", "snippet"):
                    value = child.get(key)
                    if isinstance(value, str):
                        haystacks.append(value)
    repo_path = str(entry.path.resolve())
    for haystack in haystacks:
        if _haystack_mentions_repo_path(haystack, repo_path):
            return True
    return False


def _haystack_mentions_repo_path(haystack: str, repo_path: str) -> bool:
    if not repo_path:
        return False
    start = 0
    while True:
        pos = haystack.find(repo_path, start)
        if pos == -1:
            return False
        end = pos + len(repo_path)
        if end == len(haystack):
            return True
        next_char = haystack[end]
        if next_char in "/\\" or not (next_char.isalnum() or next_char in "._-"):
            return True
        start = pos + 1


def _aggregate_signatures(
    repo_results: list[tuple[constants.RepoEntry, dict[str, Any] | None]],
    *,
    agent_scan: dict[str, Any] | None = None,
    entries: list[constants.RepoEntry],
) -> tuple[list[dict[str, Any]], int]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    unassociated = 0

    def _touch(
        candidate: dict[str, Any],
        entry: constants.RepoEntry,
        *,
        generated_at: str | None,
        agent_evidence: bool = False,
        count_signature: bool = True,
    ) -> None:
        key = _signature_key(candidate)
        count = _occurrence_count(candidate)
        if key not in grouped:
            grouped[key] = {
                "friction_type": str(candidate.get("friction_type") or "unknown"),
                "safe_summary": _safe_summary(candidate),
                "repo_ids": set(),
                "occurrence_count": 0,
                "repos": {},
                "latest_evidence_at": generated_at,
                "agent_evidence": False,
            }
            order.append(key)
        bucket = grouped[key]
        if count_signature:
            bucket["occurrence_count"] += count
        bucket["agent_evidence"] = bool(bucket["agent_evidence"] or agent_evidence)
        if generated_at and (bucket.get("latest_evidence_at") is None or generated_at > bucket["latest_evidence_at"]):
            bucket["latest_evidence_at"] = generated_at
        if entry.repo_id not in bucket["repo_ids"]:
            bucket["repo_ids"].add(entry.repo_id)
            bucket["repos"][entry.repo_id] = {
                "repo_id": entry.repo_id,
                "label": entry.label,
                "occurrence_count": 0,
            }
        bucket["repos"][entry.repo_id]["occurrence_count"] += count

    for entry, scan in repo_results:
        if scan is None:
            continue
        generated_at = str(scan.get("generated_at") or "")
        candidates = scan.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            _touch(candidate, entry, generated_at=generated_at or None)

    if agent_scan is not None:
        generated_at = str(agent_scan.get("generated_at") or "")
        candidates = agent_scan.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                matched = [entry for entry in entries if _candidate_mentions_repo(candidate, entry)]
                if not matched:
                    count = _occurrence_count(candidate)
                    unassociated += count
                    key = _signature_key(candidate)
                    if key not in grouped:
                        grouped[key] = {
                            "friction_type": str(candidate.get("friction_type") or "unknown"),
                            "safe_summary": _safe_summary(candidate),
                            "repo_ids": set(),
                            "occurrence_count": 0,
                            "repos": {},
                            "latest_evidence_at": generated_at or None,
                            "agent_evidence": True,
                        }
                        order.append(key)
                    bucket = grouped[key]
                    bucket["occurrence_count"] += count
                    bucket["agent_evidence"] = True
                    if generated_at and (
                        bucket.get("latest_evidence_at") is None or generated_at > bucket["latest_evidence_at"]
                    ):
                        bucket["latest_evidence_at"] = generated_at
                    continue
                for index, entry in enumerate(matched):
                    _touch(
                        candidate,
                        entry,
                        generated_at=generated_at or None,
                        agent_evidence=True,
                        count_signature=index == 0,
                    )

    signatures: list[dict[str, Any]] = []
    for key in sorted(order):
        bucket = grouped[key]
        repos = sorted(bucket["repos"].values(), key=lambda item: str(item.get("repo_id") or ""))
        signatures.append(
            {
                "id": key,
                "friction_type": bucket["friction_type"],
                "safe_summary": bucket["safe_summary"],
                "repo_count": len(bucket["repo_ids"]),
                "occurrence_count": bucket["occurrence_count"],
                "latest_evidence_at": bucket.get("latest_evidence_at"),
                "agent_evidence": bool(bucket["agent_evidence"]),
                "repos": repos,
            }
        )
    return signatures, unassociated


def _previous_signature_index(previous: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    if previous is None:
        return indexed
    previous_signatures = previous.get("signatures")
    if not isinstance(previous_signatures, list):
        return indexed
    for item in previous_signatures:
        if not isinstance(item, dict):
            continue
        key = _signature_key(item)
        if key == "unknown":
            friction_type = str(item.get("friction_type") or "")
            if friction_type:
                key = friction_type
        indexed[key] = item
    return indexed


def _signature_record_fields(signature: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "id": signature.get("id"),
        "friction_type": signature.get("friction_type"),
        "safe_summary": signature.get("safe_summary"),
        "repo_count": signature.get("repo_count"),
        "occurrence_count": signature.get("occurrence_count"),
        "latest_evidence_at": signature.get("latest_evidence_at"),
        "agent_evidence": signature.get("agent_evidence") is True,
    }
    repos = signature.get("repos")
    if isinstance(repos, list):
        fields["repos"] = [
            {
                "repo_id": repo.get("repo_id"),
                "label": repo.get("label"),
                "occurrence_count": repo.get("occurrence_count"),
            }
            for repo in repos
            if isinstance(repo, dict)
        ]
    return fields


def _classify_trends(
    signatures: list[dict[str, Any]],
    *,
    previous: dict[str, Any] | None,
    repo_rows: list[dict[str, Any]],
    agent_logs_status: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    comparison: dict[str, Any] = {
        "previous_report_id": None,
        "cleared": [],
        "unknown": [],
    }
    previous_by_key = _previous_signature_index(previous)
    if previous is None:
        for signature in signatures:
            signature["trend"] = "new"
        return signatures, comparison

    comparison["previous_report_id"] = previous.get("report_id")
    current_keys = {_signature_key(signature) for signature in signatures}
    repo_status = {str(row.get("repo_id") or ""): str(row.get("status") or "") for row in repo_rows}

    for signature in signatures:
        key = _signature_key(signature)
        signature["trend"] = "recurring" if key in previous_by_key else "new"

    for key, previous_signature in previous_by_key.items():
        if key in current_keys:
            continue
        prior_repos = previous_signature.get("repos")
        repo_ids: list[str] = []
        if isinstance(prior_repos, list):
            repo_ids = [str(repo.get("repo_id") or "") for repo in prior_repos if isinstance(repo, dict)]
        bucket = _signature_record_fields(previous_signature)
        if previous_signature.get("agent_evidence") is True and agent_logs_status != "completed":
            comparison["unknown"].append(bucket)
        elif repo_ids:
            unresolved = any(repo_status.get(repo_id) != "completed" for repo_id in repo_ids if repo_id)
            if unresolved:
                comparison["unknown"].append(bucket)
            else:
                comparison["cleared"].append(bucket)
        elif agent_logs_status == "completed":
            comparison["cleared"].append(bucket)
        else:
            comparison["unknown"].append(bucket)

    return signatures, comparison


def _sanitize_config_errors(errors: list[str], target: Path) -> list[str]:
    return [fleet._safe_text(error, target, "repo-fleet", "repo fleet") for error in errors]


def _agent_log_root_replacements() -> list[tuple[str, str]]:
    labels = ("agent-logs/codex", "agent-logs/claude")
    replacements: list[tuple[str, str]] = []
    for agent_dir, label in zip(friction_cmd.DEFAULT_AGENT_LOG_DIRS, labels, strict=True):
        try:
            resolved = str(Path(agent_dir).expanduser().resolve())
        except OSError:
            continue
        replacements.append((resolved, label))
    return replacements


def _path_safe_value(value: object, *, target: Path, entries: list[constants.RepoEntry]) -> object:
    if isinstance(value, dict):
        return {key: _path_safe_value(item, target=target, entries=entries) for key, item in value.items()}
    if isinstance(value, list):
        return [_path_safe_value(item, target=target, entries=entries) for item in value]
    if not isinstance(value, str):
        return value
    rendered = value
    replacements = [(str(target), "."), (str(target.resolve()), ".")]
    for entry in entries:
        replacements.append((str(entry.path), entry.repo_id))
        replacements.append((str(entry.path.resolve()), entry.repo_id))
    replacements.extend(_agent_log_root_replacements())
    for private, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if private:
            rendered = rendered.replace(private, label)
    return rendered


def _path_safe_payload(payload: dict[str, Any], *, target: Path, entries: list[constants.RepoEntry]) -> dict[str, Any]:
    sanitized = _path_safe_value(payload, target=target, entries=entries)
    return sanitized if isinstance(sanitized, dict) else payload


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Brigade Repo Fleet Friction Scan",
        "",
        f"- Report: `{payload.get('report_id')}`",
        f"- Generated: {payload.get('generated_at')}",
        f"- Status: {payload.get('status')}",
        f"- Repos: {payload.get('completed_repo_count')}/{payload.get('repo_count')} completed",
        f"- Signatures: {payload.get('signature_count')}",
        "",
        "## Signatures",
        "",
    ]
    signatures = payload.get("signatures")
    if isinstance(signatures, list) and signatures:
        for signature in signatures:
            if not isinstance(signature, dict):
                continue
            lines.append(
                f"- `{signature.get('friction_type')}` trend={signature.get('trend')} "
                f"repos={signature.get('repo_count')} occurrences={signature.get('occurrence_count')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Repos", ""])
    repos = payload.get("repos")
    if isinstance(repos, list) and repos:
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            lines.append(
                f"- `{repo.get('repo_id')}` ({repo.get('label')}): {repo.get('status')} "
                f"files={repo.get('files_scanned')} candidates={repo.get('candidate_count')}"
            )
    else:
        lines.append("- none")
    comparison = payload.get("comparison")
    if isinstance(comparison, dict):
        lines.extend(["", "## Comparison", ""])
        previous_report_id = comparison.get("previous_report_id")
        if previous_report_id:
            lines.append(f"- Previous report: `{previous_report_id}`")
        for label in ("cleared", "unknown"):
            items = comparison.get(label)
            if isinstance(items, list) and items:
                names = ", ".join(str(item.get("friction_type") or "?") for item in items if isinstance(item, dict))
                lines.append(f"- {label}: {names}")
    return "\n".join(lines).rstrip() + "\n"


def _write_report(target: Path, payload: dict[str, Any]) -> None:
    root = _friction_root(target)
    root.mkdir(parents=True, exist_ok=True)
    report_id = str(payload.get("report_id") or "latest")
    write_text_atomic(root / f"{report_id}.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_text_atomic(root / f"{report_id}.md", _render_markdown(payload))
    write_text_atomic(root / "latest.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_text_atomic(root / "latest.md", _render_markdown(payload))


def _scan_one_repo(
    entry: constants.RepoEntry,
    *,
    days: int,
    max_files: int,
    max_candidates: int,
) -> tuple[constants.RepoEntry, dict[str, Any] | None, dict[str, Any]]:
    try:
        if not entry.path.is_dir():
            return entry, None, _failed_repo_row(entry, detail="repository path is not reachable")
        payload, code = friction_cmd.scan_payload(
            target=entry.path,
            days=days,
            include_agent_logs=False,
            max_files=max_files,
            max_candidates=max_candidates,
        )
        if payload is None or code != 0:
            return entry, None, _failed_repo_row(entry, detail="friction scan failed")
        return entry, payload, _completed_repo_row(entry, payload)
    except Exception:
        return entry, None, _failed_repo_row(entry, detail="friction scan failed")


def friction_scan(
    *,
    target: Path,
    days: int = 30,
    include_agent_logs: bool = False,
    max_files: int = 5000,
    max_candidates: int = 200,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    entries, errors, config_loaded = fleet._load_config(target)
    enabled = [entry for entry in entries if entry.enabled]
    agent_scan: dict[str, Any] | None = None
    agent_logs_meta: dict[str, Any] | None = None
    if include_agent_logs:
        agent_logs_meta = {"status": "failed"}
        try:
            agent_payload, agent_code = friction_cmd.scan_payload(
                target=target,
                days=days,
                include_agent_logs=True,
                agent_logs_only=True,
                max_files=max_files,
                max_candidates=max_candidates,
            )
            if agent_payload is not None and agent_code == 0:
                agent_scan = agent_payload
                agent_logs_meta = {
                    "status": "completed",
                    "candidate_count": int(agent_payload.get("candidate_count") or 0),
                    "files_scanned": int(agent_payload.get("files_scanned") or 0),
                    "files_skipped": int(agent_payload.get("files_skipped") or 0),
                }
        except Exception:
            agent_scan = None

    if len(enabled) <= 1:
        scanned = [
            _scan_one_repo(entry, days=days, max_files=max_files, max_candidates=max_candidates) for entry in enabled
        ]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(enabled))) as executor:
            scanned = list(
                executor.map(
                    lambda entry: _scan_one_repo(
                        entry,
                        days=days,
                        max_files=max_files,
                        max_candidates=max_candidates,
                    ),
                    enabled,
                )
            )

    repo_rows = [row for _entry, _scan, row in scanned]
    repo_results = [(entry, scan) for entry, scan, _row in scanned]
    signatures, unassociated = _aggregate_signatures(repo_results, agent_scan=agent_scan, entries=enabled)
    previous = _read_json(_latest_json_path(target))
    agent_logs_status = str(agent_logs_meta.get("status") or "") if agent_logs_meta is not None else None
    signatures, comparison = _classify_trends(
        signatures,
        previous=previous,
        repo_rows=repo_rows,
        agent_logs_status=agent_logs_status,
    )

    completed_repo_count = sum(1 for row in repo_rows if row.get("status") == "completed")
    failed_repo_count = sum(1 for row in repo_rows if row.get("status") == "failed")
    agent_logs_ok = agent_logs_meta is None or agent_logs_meta.get("status") == "completed"
    status = "complete" if failed_repo_count == 0 and config_loaded and not errors and agent_logs_ok else "partial"
    generated = _now()
    report_id = f"{generated.strftime('%Y%m%d-%H%M%S')}-{generated.strftime('%f')}-repos-friction"

    payload: dict[str, Any] = {
        "report_id": report_id,
        "generated_at": generated.isoformat(),
        "status": status,
        "repo_count": len(enabled),
        "completed_repo_count": completed_repo_count,
        "failed_repo_count": failed_repo_count,
        "signature_count": len(signatures),
        "signatures": signatures,
        "repos": repo_rows,
        "comparison": comparison,
        "config_loaded": config_loaded,
        "config_errors": _sanitize_config_errors(errors, target),
        "days": days,
        "include_agent_logs": include_agent_logs,
        "unassociated_occurrence_count": unassociated,
    }
    if agent_logs_meta is not None:
        payload["agent_logs"] = agent_logs_meta

    for row in payload["repos"]:
        row.pop("detail", None)
    payload = _path_safe_payload(payload, target=target, entries=enabled)
    _write_report(target, payload)

    rc = 0 if status == "complete" else 1
    text_lines = [
        f"repo fleet friction scan: {report_id}",
        f"status: {status}",
        f"repos: {completed_repo_count}/{len(enabled)} completed",
        f"signatures: {len(signatures)}",
        "path: repos/friction",
    ]
    return emit(payload, json_output, text_lines, rc)


def friction_show(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path = _latest_json_path(target)
    payload = _read_json(path)
    if payload is None:
        display_path = fleet._safe_text(path, target, None, ".")
        print(
            f"error: no repo fleet friction scan found at {display_path}; run `brigade repos friction scan` first",
            file=sys.stderr,
        )
        return 2
    entries, _errors, _config_loaded = fleet._load_config(target)
    enabled = [entry for entry in entries if entry.enabled]
    payload = _path_safe_payload(payload, target=target, entries=enabled)
    text_lines = [
        f"repo fleet friction show: {payload.get('report_id')}",
        f"status: {payload.get('status')}",
        f"signatures: {payload.get('signature_count')}",
        "path: repos/friction",
    ]
    return emit(payload, json_output, text_lines, 0)


__all__ = ("friction_scan", "friction_show")
