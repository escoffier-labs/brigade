"""Local memory card decay scanner."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import toml_compat as tomllib, work_cmd
from .install import apply_gitignore
from .selection import Selection
from .localio import utc_now_iso_z as _utc_iso

CONFIG_REL_PATH = ".brigade/memory-care.toml"
DEFAULT_OUTPUT_PATH = ".brigade/memory-care/decay"
# Pre-0.9.1 scans wrote into the user's card tree. Readers fall back to this
# location when the default is in effect and no new-style output exists yet.
LEGACY_OUTPUT_PATH = "memory/cards/decay"
CHECKS = (
    "stale",
    "expired",
    "undersourced",
    "contradictory",
    "missing-index-link",
    "orphaned-card",
    "oversized-card",
    "missing-frontmatter",
    "missing-reviewed",
    "missing-freshness",
)
CONFIDENCE_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
PLANABLE_METADATA_FIXES = {"missing-reviewed", "missing-freshness"}


@dataclass(frozen=True)
class MemoryCareConfig:
    card_roots: tuple[str, ...] = ("memory/cards",)
    index_paths: tuple[str, ...] = ("MEMORY.md",)
    stale_after_days: int = 90
    expiry_warning_days: int = 0
    minimum_confidence: str = "medium"
    require_evidence: bool = True
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ("memory/cards/decay",)
    output_path: str = DEFAULT_OUTPUT_PATH
    enabled_checks: tuple[str, ...] = CHECKS
    max_card_bytes: int = 12_000


def config_path(target: Path) -> Path:
    return target.expanduser().resolve() / CONFIG_REL_PATH


def _output_dir(target: Path, config: MemoryCareConfig) -> Path:
    return target.expanduser().resolve() / config.output_path


def _read_output_dir(target: Path, config: MemoryCareConfig) -> Path:
    """Resolve the output dir for readers, honoring the legacy location.

    Scans write to `config.output_path`. When the default path is in effect
    and has no scan output yet, fall back to the pre-0.9.1 location so
    existing workspaces keep their queue continuity.
    """
    output = _output_dir(target, config)
    if config.output_path == DEFAULT_OUTPUT_PATH and not (output / "scan-latest.json").is_file():
        legacy = target.expanduser().resolve() / LEGACY_OUTPUT_PATH
        if (legacy / "scan-latest.json").is_file() or (legacy / "refresh-queue.json").is_file():
            return legacy
    return output


def _scan_path(target: Path, config: MemoryCareConfig) -> Path:
    return _read_output_dir(target, config) / "scan-latest.json"


def _queue_path(target: Path, config: MemoryCareConfig) -> Path:
    return _read_output_dir(target, config) / "refresh-queue.json"


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def _format_config(config: MemoryCareConfig | None = None) -> str:
    config = config if config is not None else MemoryCareConfig()
    return "\n".join(
        [
            "# Local memory-care scanner config. Brigade scans and imports issues, but never edits cards automatically.",
            f"card_roots = {_toml_value(list(config.card_roots))}",
            f"index_paths = {_toml_value(list(config.index_paths))}",
            f"stale_after_days = {config.stale_after_days}",
            f"expiry_warning_days = {config.expiry_warning_days}",
            f"minimum_confidence = {_toml_value(config.minimum_confidence)}",
            f"require_evidence = {_toml_value(config.require_evidence)}",
            f"include_paths = {_toml_value(list(config.include_paths))}",
            f"exclude_paths = {_toml_value(list(config.exclude_paths))}",
            f"output_path = {_toml_value(config.output_path)}",
            f"enabled_checks = {_toml_value(list(config.enabled_checks))}",
            f"max_card_bytes = {config.max_card_bytes}",
            "",
        ]
    )


def _string_list(
    raw: object, *, field: str, default: tuple[str, ...], allowed: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field} must be a list of strings")
    values = tuple(item.strip() for item in raw if item.strip())
    if allowed is not None:
        invalid = [item for item in values if item not in allowed]
        if invalid:
            raise ValueError(f"{field} entries must be one of: {', '.join(allowed)}")
    return values


def _positive_int(raw: object, *, field: str, default: int, minimum: int = 0) -> int:
    if raw is None:
        return default
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return raw


def _relative_path(value: str, *, field: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{field} must be a non-empty relative path without '..'")
    return value.strip()


def load_config(target: Path) -> MemoryCareConfig | None:
    path = config_path(target)
    if not path.is_file():
        return None
    if tomllib is None:
        raise ValueError("memory-care config requires Python tomllib support")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:  # type: ignore[union-attr]
        raise ValueError(f"invalid memory-care config: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("memory-care config must be a TOML object")
    card_roots = _string_list(data.get("card_roots"), field="card_roots", default=("memory/cards",))
    index_paths = _string_list(data.get("index_paths"), field="index_paths", default=("MEMORY.md",))
    include_paths = _string_list(data.get("include_paths"), field="include_paths", default=())
    exclude_paths = _string_list(data.get("exclude_paths"), field="exclude_paths", default=("memory/cards/decay",))
    enabled_checks = _string_list(data.get("enabled_checks"), field="enabled_checks", default=CHECKS, allowed=CHECKS)
    minimum_confidence = data.get("minimum_confidence", "medium")
    if not isinstance(minimum_confidence, str) or minimum_confidence not in CONFIDENCE_RANK:
        raise ValueError("minimum_confidence must be one of: unknown, low, medium, high")
    require_evidence = data.get("require_evidence", True)
    if not isinstance(require_evidence, bool):
        raise ValueError("require_evidence must be true or false")
    output_path = data.get("output_path", DEFAULT_OUTPUT_PATH)
    if not isinstance(output_path, str):
        raise ValueError("output_path must be a string")
    for field, paths in (
        ("card_roots", card_roots),
        ("index_paths", index_paths),
        ("include_paths", include_paths),
        ("exclude_paths", exclude_paths),
    ):
        for item in paths:
            _relative_path(item, field=field)
    return MemoryCareConfig(
        card_roots=card_roots,
        index_paths=index_paths,
        stale_after_days=_positive_int(data.get("stale_after_days"), field="stale_after_days", default=90),
        expiry_warning_days=_positive_int(data.get("expiry_warning_days"), field="expiry_warning_days", default=0),
        minimum_confidence=minimum_confidence,
        require_evidence=require_evidence,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        output_path=_relative_path(output_path, field="output_path"),
        enabled_checks=enabled_checks,
        max_card_bytes=_positive_int(data.get("max_card_bytes"), field="max_card_bytes", default=12_000, minimum=1),
    )


def _config_or_default(target: Path) -> MemoryCareConfig:
    return load_config(target) or MemoryCareConfig()


def init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = config_path(target)
    if path.exists() and not force:
        print(f"error: memory-care config already exists: {path}", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_config())
    if update_gitignore:
        apply_gitignore(target, Selection(depth="repo", harnesses=[], owner="this-repo", includes=[]))
    print(f"memory_care_config: {path}")
    print(f"output_path: {DEFAULT_OUTPUT_PATH}")
    print("next_command: brigade memory care scan")
    return 0


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], bool]:
    if not text.startswith("---\n"):
        return {}, False
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, False
    data: dict[str, Any] = {}
    for raw in lines[1:end]:
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                parsed = value
            data[key] = parsed
        elif value.lower() in {"true", "false"}:
            data[key] = value.lower() == "true"
        else:
            data[key] = value.strip("'\"")
    return data, True


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        clean = pattern.replace("\\", "/").strip("/")
        if normalized == clean or normalized.startswith(clean.rstrip("/") + "/"):
            return True
    return False


def _iter_cards(target: Path, config: MemoryCareConfig) -> list[Path]:
    paths: list[Path] = []
    for root_text in config.card_roots:
        root = target / root_text
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(target))
            if config.include_paths and not _path_matches(rel, config.include_paths):
                continue
            if config.exclude_paths and _path_matches(rel, config.exclude_paths):
                continue
            paths.append(path)
    return sorted(set(paths))


def _index_links(target: Path, config: MemoryCareConfig) -> set[str]:
    links: set[str] = set()
    pattern = re.compile(r"\[[^\]]+\]\((?P<path>memory/cards/[^)#\s]+\.md)(?:#[^)]+)?\)")
    for index in config.index_paths:
        path = target / index
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        links.update(match.group("path") for match in pattern.finditer(text))
    return links


def _frontmatter_value(meta: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = meta.get(key)
        if value not in (None, ""):
            return value
    return None


def _has_evidence(meta: dict[str, Any]) -> bool:
    value = _frontmatter_value(meta, "evidence", "sources", "source", "refs", "links")
    if value is None:
        return False
    if isinstance(value, list):
        return bool(value)
    return bool(str(value).strip())


def _safe_summary(text: str) -> str:
    rendered = " ".join(text.split())
    if len(rendered) <= 180:
        return rendered
    return rendered[:177].rstrip() + "..."


def _priority(issue_type: str, severity: str) -> str:
    if severity == "high" or issue_type in {"expired", "missing-index-link"}:
        return "high"
    if issue_type in {
        "missing-frontmatter",
        "missing-reviewed",
        "missing-freshness",
        "oversized-card",
        "contradictory",
    }:
        return "normal"
    return "normal"


def _issue(
    *,
    target: Path,
    card_path: str,
    card_id: str,
    issue_type: str,
    severity: str,
    summary: str,
    evidence: list[str],
    action: str,
) -> dict[str, Any]:
    fingerprint = _stable_hash(
        {
            "card_id": card_id,
            "card_path": card_path,
            "issue_type": issue_type,
            "summary": summary,
            "evidence": evidence,
        }
    )
    acceptance = [
        f"Review `{card_path}` against current source evidence.",
        "Update the memory card through the reviewed memory workflow or document why no change is needed.",
        "`brigade memory care doctor` no longer reports this issue.",
    ]
    record = {
        "id": f"memory-care-{fingerprint}",
        "file": card_path,
        "path": card_path,
        "card_file": card_path,
        "card_id": card_id,
        "issue_type": issue_type,
        "refresh_reason": issue_type,
        "reason": issue_type,
        "severity": severity,
        "priority": _priority(issue_type, severity),
        "type": "docs"
        if issue_type
        in {
            "missing-index-link",
            "orphaned-card",
            "missing-frontmatter",
            "missing-reviewed",
            "missing-freshness",
            "oversized-card",
        }
        else "research",
        "template": "docs"
        if issue_type
        in {
            "missing-index-link",
            "orphaned-card",
            "missing-frontmatter",
            "missing-reviewed",
            "missing-freshness",
            "oversized-card",
        }
        else "bugfix",
        "safe_summary": _safe_summary(summary),
        "evidence_references": evidence,
        "evidence_summary": "; ".join(evidence) if evidence else summary,
        "suggested_refresh_action": action,
        "acceptance": acceptance,
        "source_fingerprint": fingerprint,
        "source_item_key": f"memory-care:{card_id}:{issue_type}",
    }
    if issue_type in PLANABLE_METADATA_FIXES:
        record["safe_autofix_plan"] = {
            "mode": "metadata-only",
            "safe_to_apply_automatically": False,
            "would_write": False,
            "blocked": True,
            "blockers": (
                ["requires-current-evidence-review"]
                if issue_type == "missing-reviewed"
                else ["requires-operator-freshness-date"]
            ),
            "candidate_fields": (["last_reviewed"] if issue_type == "missing-reviewed" else ["fresh_until"]),
        }
    return record


def _scan_payload(target: Path, config: MemoryCareConfig) -> dict[str, Any]:
    target = target.expanduser().resolve()
    today = _today()
    cards = _iter_cards(target, config)
    linked = _index_links(target, config)
    linked_existing = {link for link in linked if (target / link).is_file()}
    issues: list[dict[str, Any]] = []
    card_rows: list[dict[str, Any]] = []
    by_id: dict[str, list[str]] = {}
    enabled = set(config.enabled_checks)

    for path in cards:
        rel = str(path.relative_to(target))
        text = path.read_text(errors="replace")
        meta, has_frontmatter = _parse_frontmatter(text)
        card_id = str(_frontmatter_value(meta, "id", "card_id", "topic") or Path(rel).stem)
        by_id.setdefault(card_id, []).append(rel)
        row = {"file": rel, "card_id": card_id, "has_frontmatter": has_frontmatter, "bytes": path.stat().st_size}
        if "missing-frontmatter" in enabled and not has_frontmatter:
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="missing-frontmatter",
                    severity="medium",
                    summary=f"{rel} has no YAML frontmatter metadata.",
                    evidence=[rel],
                    action="Add reviewed frontmatter with topic, confidence, evidence, and review metadata.",
                )
            )
        reviewed = _parse_date(_frontmatter_value(meta, "last_reviewed", "last_reviewed_at", "reviewed_at"))
        row["last_reviewed"] = reviewed.isoformat() if reviewed else None
        if has_frontmatter and "missing-reviewed" in enabled and reviewed is None:
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="missing-reviewed",
                    severity="medium",
                    summary=f"{rel} has no reviewed date metadata.",
                    evidence=["last_reviewed missing"],
                    action="Add a reviewed date after checking the card against current evidence.",
                )
            )
        if "stale" in enabled and reviewed is not None and (today - reviewed).days > config.stale_after_days:
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="stale",
                    severity="medium",
                    summary=f"{rel} was last reviewed {(today - reviewed).days} days ago.",
                    evidence=[f"last_reviewed={reviewed.isoformat()}"],
                    action="Check current sources and refresh the card or extend its review date with evidence.",
                )
            )
        expiry = _parse_date(_frontmatter_value(meta, "fresh_until", "expires_at", "expires"))
        row["fresh_until"] = expiry.isoformat() if expiry else None
        if has_frontmatter and "missing-freshness" in enabled and expiry is None:
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="missing-freshness",
                    severity="medium",
                    summary=f"{rel} has no freshness date metadata.",
                    evidence=["fresh_until missing"],
                    action="Add a freshness date or document why the card should not expire.",
                )
            )
        if "expired" in enabled and expiry is not None and (expiry - today).days <= config.expiry_warning_days:
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="expired",
                    severity="high",
                    summary=f"{rel} freshness expired on {expiry.isoformat()}.",
                    evidence=[f"fresh_until={expiry.isoformat()}"],
                    action="Refresh the card from current evidence or mark it obsolete.",
                )
            )
        confidence = str(_frontmatter_value(meta, "confidence") or "unknown").lower()
        weak_confidence = CONFIDENCE_RANK.get(confidence, 0) < CONFIDENCE_RANK[config.minimum_confidence]
        has_evidence = _has_evidence(meta)
        missing_evidence = config.require_evidence and not has_evidence
        row["confidence"] = confidence
        row["has_evidence"] = has_evidence
        card_rows.append(row)
        if "undersourced" in enabled and (weak_confidence or missing_evidence):
            evidence = []
            if weak_confidence:
                evidence.append(f"confidence={confidence}")
            if missing_evidence:
                evidence.append("evidence missing")
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="undersourced",
                    severity="medium",
                    summary=f"{rel} has weak confidence or missing evidence metadata.",
                    evidence=evidence,
                    action="Attach current source evidence or lower the card's authority.",
                )
            )
        if "oversized-card" in enabled and path.stat().st_size > config.max_card_bytes:
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="oversized-card",
                    severity="medium",
                    summary=f"{rel} is {path.stat().st_size} bytes, over {config.max_card_bytes}.",
                    evidence=[f"bytes={path.stat().st_size}"],
                    action="Split the card into smaller atomic topics and update MEMORY.md links.",
                )
            )
        if "orphaned-card" in enabled and linked and rel not in linked_existing:
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="orphaned-card",
                    severity="low",
                    summary=f"{rel} exists but is not linked from configured memory indexes.",
                    evidence=list(config.index_paths),
                    action="Add an index link or archive the card.",
                )
            )

    if "missing-index-link" in enabled:
        for missing in sorted(link for link in linked if not (target / link).is_file()):
            issues.append(
                _issue(
                    target=target,
                    card_path=missing,
                    card_id=Path(missing).stem,
                    issue_type="missing-index-link",
                    severity="high",
                    summary=f"Configured memory index links missing card {missing}.",
                    evidence=list(config.index_paths),
                    action="Restore the card or remove the stale MEMORY.md link.",
                )
            )
    if "contradictory" in enabled:
        for card_id, paths in sorted(by_id.items()):
            if len(paths) <= 1:
                continue
            for rel in paths:
                issues.append(
                    _issue(
                        target=target,
                        card_path=rel,
                        card_id=card_id,
                        issue_type="contradictory",
                        severity="medium",
                        summary=f"Card id {card_id} appears in multiple cards.",
                        evidence=paths,
                        action="Merge duplicate card identities or make the card ids unique.",
                    )
                )

    counts: dict[str, int] = {}
    for issue in issues:
        issue_type = str(issue["issue_type"])
        counts[issue_type] = counts.get(issue_type, 0) + 1
    confidence_counts: dict[str, int] = {}
    for row in card_rows:
        confidence = str(row.get("confidence") or "unknown")
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
    metadata_summary = {
        "reviewed_dates": {
            "present": len([row for row in card_rows if row.get("last_reviewed")]),
            "missing": len([row for row in card_rows if row.get("has_frontmatter") and not row.get("last_reviewed")]),
            "stale": counts.get("stale", 0),
        },
        "freshness_dates": {
            "present": len([row for row in card_rows if row.get("fresh_until")]),
            "missing": len([row for row in card_rows if row.get("has_frontmatter") and not row.get("fresh_until")]),
            "expired": counts.get("expired", 0),
        },
        "confidence": dict(sorted(confidence_counts.items())),
        "evidence": {
            "present": len([row for row in card_rows if row.get("has_evidence")]),
            "missing": len([row for row in card_rows if row.get("has_frontmatter") and not row.get("has_evidence")]),
        },
    }
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "generated_at": _utc_iso(),
        "scan_date": today.isoformat(),
        "card_count": len(cards),
        "issue_count": len(issues),
        "refresh_queue_size": len(issues),
        "counts": dict(sorted(counts.items())),
        "metadata": metadata_summary,
        "cards": card_rows,
        "issues": issues,
    }


def _queue_payload(scan_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "scan_date": scan_payload["scan_date"],
        "generated_at": scan_payload["generated_at"],
        "source": "memory-care",
        "cards": scan_payload["issues"],
    }


def _write_scan_outputs(target: Path, config: MemoryCareConfig, payload: dict[str, Any]) -> tuple[Path, Path]:
    output = _output_dir(target, config)
    output.mkdir(parents=True, exist_ok=True)
    scan_path = output / "scan-latest.json"
    queue_path = output / "refresh-queue.json"
    scan_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    queue_path.write_text(json.dumps(_queue_payload(payload), indent=2, sort_keys=True) + "\n")
    return scan_path, queue_path


def _load_scan_payload(target: Path, config: MemoryCareConfig) -> dict[str, Any] | None:
    return _load_json_file(_scan_path(target, config))


def _autofix_plan_item(issue: dict[str, Any], *, target: Path, scan_date: str) -> dict[str, Any]:
    card_file = str(issue.get("file") or issue.get("card_file") or "")
    issue_type = str(issue.get("issue_type") or "")
    source_fingerprint = str(issue.get("source_fingerprint") or "")
    path = target / card_file if card_file else target
    blockers: list[str] = []
    candidate_fields: dict[str, str] = {}
    safe_to_apply = False
    if issue_type == "missing-reviewed":
        candidate_fields["last_reviewed"] = scan_date
        blockers.append("requires-current-evidence-review")
    elif issue_type == "missing-freshness":
        candidate_fields["fresh_until"] = "<operator-selected-date>"
        blockers.append("requires-operator-freshness-date")
    else:
        blockers.append("issue-type-not-supported-for-metadata-plan")
    if not card_file:
        blockers.append("missing-card-path")
    elif not path.is_file():
        blockers.append("card-file-missing")
    else:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            blockers.append("card-file-unreadable")
        else:
            _meta, has_frontmatter = _parse_frontmatter(text)
            if not has_frontmatter:
                blockers.append("card-frontmatter-missing")
    return {
        "id": f"memory-care-fix-{source_fingerprint or _stable_hash({'file': card_file, 'issue_type': issue_type})}",
        "card_file": card_file,
        "card_id": issue.get("card_id"),
        "issue_type": issue_type,
        "source_fingerprint": source_fingerprint,
        "safe_summary": issue.get("safe_summary") or issue.get("summary") or "",
        "candidate_fields": candidate_fields,
        "status": "blocked" if blockers else "planned",
        "safe_to_apply_automatically": safe_to_apply,
        "would_write": False,
        "blockers": blockers,
        "suggested_next_command": "brigade memory care import-issues",
    }


def _autofix_plan_payload(
    target: Path, config: MemoryCareConfig, scan_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    if scan_payload is None:
        scan_payload = _load_scan_payload(target, config)
    scan_date = (
        str(scan_payload.get("scan_date") or _today().isoformat())
        if isinstance(scan_payload, dict)
        else _today().isoformat()
    )
    issues = (
        scan_payload.get("issues")
        if isinstance(scan_payload, dict) and isinstance(scan_payload.get("issues"), list)
        else []
    )
    items = [
        _autofix_plan_item(issue, target=target, scan_date=scan_date)
        for issue in issues
        if isinstance(issue, dict) and str(issue.get("issue_type") or "") in PLANABLE_METADATA_FIXES
    ]
    blocked = [item for item in items if item.get("blockers")]
    return {
        "target": str(target),
        "scan_path": str(_scan_path(target, config)),
        "queue_path": str(_queue_path(target, config)),
        "generated_at": _utc_iso(),
        "valid": scan_payload is not None,
        "would_write": False,
        "plan_count": len(items),
        "blocked_count": len(blocked),
        "items": items,
        "suggested_next_command": "brigade memory care import-issues" if items else "brigade memory care scan",
    }


def scan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = _config_or_default(target)
    except ValueError as exc:
        print(f"error: invalid memory-care config: {exc}", file=sys.stderr)
        return 2
    payload = _scan_payload(target, config)
    scan_path, queue_path = _write_scan_outputs(target, config, payload)
    payload["scan_path"] = str(scan_path)
    payload["queue_path"] = str(queue_path)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory care scan: {target}")
    print(f"scan_path: {scan_path}")
    print(f"queue_path: {queue_path}")
    print(f"cards: {payload['card_count']}")
    print(f"issues: {payload['issue_count']}")
    for issue_type, count in payload["counts"].items():
        print(f"{issue_type}: {count}")
    if payload["issues"]:
        top = payload["issues"][0]
        print(f"top_issue: {top['issue_type']} {top['card_file']} {top['safe_summary']}")
    return 0


def plan_fixes(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = _config_or_default(target)
        scan_payload = _load_scan_payload(target, config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: cannot read memory-care scan: {exc}", file=sys.stderr)
        return 2
    if scan_payload is None:
        print(f"error: memory-care scan not found: {_scan_path(target, config)}", file=sys.stderr)
        return 2
    payload = _autofix_plan_payload(target, config, scan_payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory care fix plan: {target}")
    print(f"scan_path: {payload['scan_path']}")
    print("would_write: false")
    print(f"planned: {payload['plan_count']}")
    print(f"blocked: {payload['blocked_count']}")
    for item in payload["items"]:
        blockers = ",".join(item.get("blockers", [])) or "none"
        print(f"- {item['card_file']} {item['issue_type']} {item['status']} blockers={blockers}")
    print(f"next_command: {payload['suggested_next_command']}")
    return 0


def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _validate_queue(payload: dict[str, Any], *, path: Path) -> list[str]:
    errors: list[str] = []
    cards = payload.get("cards")
    if not isinstance(cards, list):
        return [f"`cards` must be a list: {path}"]
    for index, card in enumerate(cards, start=1):
        label = f"cards[{index}]"
        if not isinstance(card, dict):
            errors.append(f"{label} must be an object")
            continue
        for field in ("file", "issue_type", "safe_summary", "source_fingerprint"):
            if not isinstance(card.get(field), str) or not str(card.get(field)).strip():
                errors.append(f"{label}.{field} must be a non-empty string")
        if card.get("issue_type") not in CHECKS:
            errors.append(f"{label}.issue_type must be one of: {', '.join(CHECKS)}")
    return errors


def health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checks: list[dict[str, Any]] = []
    try:
        config = _config_or_default(target)
    except ValueError as exc:
        config = MemoryCareConfig()
        checks.append({"status": "fail", "name": "memory_care_config", "detail": str(exc)})
    else:
        if config_path(target).is_file():
            checks.append({"status": "ok", "name": "memory_care_config", "detail": str(config_path(target))})
        else:
            checks.append(
                {
                    "status": "warn",
                    "name": "memory_care_config",
                    "detail": f"missing, run `brigade memory care init --target {target}`",
                }
            )
    scan_path = _scan_path(target, config)
    queue_path = _queue_path(target, config)
    scan_payload = None
    queue_payload = None
    try:
        scan_payload = _load_json_file(scan_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        checks.append({"status": "fail", "name": "memory_care_scan", "detail": str(exc)})
    if scan_payload is None:
        checks.append({"status": "warn", "name": "memory_care_scan", "detail": f"missing at {scan_path}"})
    else:
        checks.append(
            {
                "status": "ok",
                "name": "memory_care_scan",
                "detail": f"{scan_path} issues={scan_payload.get('issue_count')}",
            }
        )
    try:
        queue_payload = _load_json_file(queue_path)
        if queue_payload is not None:
            errors = _validate_queue(queue_payload, path=queue_path)
            if errors:
                checks.append({"status": "fail", "name": "memory_care_queue", "detail": "; ".join(errors[:5])})
            else:
                checks.append(
                    {
                        "status": "ok",
                        "name": "memory_care_queue",
                        "detail": f"{queue_path} queued={len(queue_payload.get('cards', []))}",
                    }
                )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        checks.append({"status": "fail", "name": "memory_care_queue", "detail": str(exc)})
    if queue_payload is None:
        checks.append({"status": "warn", "name": "memory_care_queue", "detail": f"missing at {queue_path}"})
    closeout = _latest_closeout(target)
    closed = set(closeout.get("source_fingerprints", [])) if isinstance(closeout, dict) else set()
    top_issue = None
    if isinstance(queue_payload, dict) and isinstance(queue_payload.get("cards"), list) and queue_payload["cards"]:
        open_cards = [
            card
            for card in queue_payload["cards"]
            if isinstance(card, dict) and str(card.get("source_fingerprint") or "") not in closed
        ]
        if open_cards:
            top_issue = open_cards[0]
            checks.append(
                {
                    "status": "warn",
                    "name": "memory_care_open_issues",
                    "detail": f"{len(open_cards)} queued, top={top_issue.get('issue_type')}:{top_issue.get('file')}",
                }
            )
        else:
            checks.append(
                {"status": "ok", "name": "memory_care_open_issues", "detail": "queued issues reviewed or deferred"}
            )
    else:
        checks.append({"status": "ok", "name": "memory_care_open_issues", "detail": "none"})
    issues = [check for check in checks if check["status"] != "ok"]
    metadata = (
        scan_payload.get("metadata")
        if isinstance(scan_payload, dict) and isinstance(scan_payload.get("metadata"), dict)
        else {}
    )
    autofix_plan = (
        _autofix_plan_payload(target, config, scan_payload)
        if isinstance(scan_payload, dict)
        else {
            "plan_count": 0,
            "blocked_count": 0,
            "items": [],
            "would_write": False,
        }
    )
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "scan_path": str(scan_path),
        "queue_path": str(queue_path),
        "valid": not any(check["status"] == "fail" for check in checks),
        "issue_count": len(issues),
        "top_issue": top_issue or (issues[0] if issues else None),
        "checks": checks,
        "metadata": metadata,
        "autofix_plan": {
            "plan_count": autofix_plan.get("plan_count", 0),
            "blocked_count": autofix_plan.get("blocked_count", 0),
            "top_item": autofix_plan["items"][0] if autofix_plan.get("items") else None,
            "suggested_next_command": "brigade memory care plan-fixes" if autofix_plan.get("plan_count") else None,
            "would_write": False,
        },
        "latest_closeout": closeout,
    }


def status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = health(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"memory care status: {target}")
    print(f"config_path: {payload['config_path']}")
    print(f"scan_path: {payload['scan_path']}")
    print(f"queue_path: {payload['queue_path']}")
    issue_count = payload["issue_count"]
    print(f"health: {'ok' if issue_count == 0 else str(issue_count) + ' issue(s)'}")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    reviewed = metadata.get("reviewed_dates") if isinstance(metadata.get("reviewed_dates"), dict) else None
    freshness = metadata.get("freshness_dates") if isinstance(metadata.get("freshness_dates"), dict) else None
    evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), dict) else None
    confidence = metadata.get("confidence") if isinstance(metadata.get("confidence"), dict) else None
    if reviewed:
        print(
            f"reviewed_dates: present={reviewed.get('present', 0)} missing={reviewed.get('missing', 0)} stale={reviewed.get('stale', 0)}"
        )
    if freshness:
        print(
            f"freshness_dates: present={freshness.get('present', 0)} missing={freshness.get('missing', 0)} expired={freshness.get('expired', 0)}"
        )
    if evidence:
        print(f"evidence_metadata: present={evidence.get('present', 0)} missing={evidence.get('missing', 0)}")
    if confidence:
        rendered = ", ".join(f"{key}={confidence[key]}" for key in sorted(confidence))
        print(f"confidence_metadata: {rendered}")
    autofix_plan = payload.get("autofix_plan") if isinstance(payload.get("autofix_plan"), dict) else {}
    if autofix_plan.get("plan_count"):
        print(
            f"autofix_plan: planned={autofix_plan.get('plan_count')} blocked={autofix_plan.get('blocked_count')} would_write=false"
        )
        if autofix_plan.get("suggested_next_command"):
            print(f"autofix_plan_command: {autofix_plan.get('suggested_next_command')}")
    top = payload.get("top_issue") if isinstance(payload.get("top_issue"), dict) else None
    if top:
        print(f"top_issue: {top.get('issue_type') or top.get('name')} {top.get('file') or top.get('detail')}")
    return 0 if payload["valid"] else 1


def doctor(*, target: Path, json_output: bool = False) -> int:
    payload = health(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"memory care doctor: {target.expanduser().resolve()}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0 if payload["valid"] else 1


def _git_last_commit_date(target: Path, rel: str) -> date | None:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(target), "log", "-1", "--format=%cs", "--", rel],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return _parse_date(value) if value else None


def _backfill_candidates(target: Path, config: MemoryCareConfig) -> tuple[list[dict[str, Any]], int]:
    """Cards with frontmatter but missing review/freshness metadata, with derived values.

    The derived `last_reviewed` is the card file's last git commit date (the
    last time anyone touched the fact), falling back to file mtime outside git.
    `fresh_until` is the derived or existing reviewed date plus the configured
    stale window. Existing values are never proposed for change.
    """
    candidates: list[dict[str, Any]] = []
    skipped_no_frontmatter = 0
    for path in _iter_cards(target, config):
        rel = str(path.relative_to(target))
        text = path.read_text(errors="replace")
        meta, has_frontmatter = _parse_frontmatter(text)
        if not has_frontmatter:
            skipped_no_frontmatter += 1
            continue
        reviewed = _parse_date(_frontmatter_value(meta, "last_reviewed", "last_reviewed_at", "reviewed_at"))
        expiry = _parse_date(_frontmatter_value(meta, "fresh_until", "expires_at", "expires"))
        if reviewed is not None and expiry is not None:
            continue
        derived = _git_last_commit_date(target, rel)
        source = "git-history"
        if derived is None:
            from datetime import datetime as _dt, timezone as _tz

            derived = _dt.fromtimestamp(path.stat().st_mtime, tz=_tz.utc).date()
            source = "file-mtime"
        base_reviewed = reviewed or derived
        from datetime import timedelta as _td

        candidate: dict[str, Any] = {
            "file": rel,
            "source": source,
            "fields": [],
        }
        if reviewed is None:
            candidate["last_reviewed"] = derived.isoformat()
            candidate["fields"].append("last_reviewed")
        if expiry is None:
            candidate["fresh_until"] = (base_reviewed + _td(days=config.stale_after_days)).isoformat()
            candidate["fields"].append("fresh_until")
        candidates.append(candidate)
    return candidates, skipped_no_frontmatter


def _backfill_write(target: Path, candidate: dict[str, Any]) -> None:
    path = target / candidate["file"]
    text = path.read_text(errors="replace")
    lines = text.split("\n")
    # _parse_frontmatter guarantees a leading `---` block; insert the new keys
    # just before its closing fence, leaving every other byte untouched.
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    additions = [f"{field}: {candidate[field]}" for field in candidate["fields"]]
    path.write_text("\n".join(lines[:closing] + additions + lines[closing:]))


def backfill(*, target: Path, apply: bool = False, json_output: bool = False) -> int:
    """Plan or apply safe metadata backfill for cards missing review/freshness dates."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = load_config(target) or MemoryCareConfig()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    candidates, skipped_no_frontmatter = _backfill_candidates(target, config)
    written = 0
    receipt_path: Path | None = None
    if apply and candidates:
        for candidate in candidates:
            _backfill_write(target, candidate)
            written += 1
        receipts_dir = target / ".brigade" / "memory-care" / "backfills"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        from .localio import utc_now, write_json

        stamp = utc_now().strftime("%Y%m%dT%H%M%S")
        receipt_path = receipts_dir / f"{stamp}.json"
        write_json(
            receipt_path,
            {
                "target": str(target),
                "generated_at": _utc_iso(),
                "written_count": written,
                "skipped_no_frontmatter": skipped_no_frontmatter,
                "stale_after_days": config.stale_after_days,
                "candidates": candidates,
            },
        )
    payload = {
        "target": str(target),
        "apply": apply,
        "candidate_count": len(candidates),
        "written_count": written,
        "skipped_no_frontmatter": skipped_no_frontmatter,
        "stale_after_days": config.stale_after_days,
        "receipt_path": str(receipt_path) if receipt_path else None,
        "candidates": candidates,
        "next_command": "brigade memory care backfill --apply"
        if candidates and not apply
        else "brigade memory care scan",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory care backfill: {target}")
    print(f"apply: {apply}")
    print(f"candidates: {len(candidates)}")
    print(f"written: {written}")
    print(f"skipped_no_frontmatter: {skipped_no_frontmatter}")
    for candidate in candidates[:10]:
        rendered = ", ".join(f"{field}={candidate[field]}" for field in candidate["fields"])
        print(f"- {candidate['file']} [{candidate['source']}] {rendered}")
    if len(candidates) > 10:
        print(f"- ... {len(candidates) - 10} more")
    if receipt_path:
        print(f"receipt: {receipt_path}")
    print(f"next_command: {payload['next_command']}")
    return 0


def import_issues(*, target: Path, json_output: bool = False, dry_run: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = _config_or_default(target)
    except ValueError as exc:
        print(f"error: invalid memory-care config: {exc}", file=sys.stderr)
        return 2
    return work_cmd.import_memory_care(
        target=target, queue=_queue_path(target, config), dry_run=dry_run, json_output=json_output
    )


def _closeouts_root(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "memory-care" / "closeouts"


def _latest_closeout(target: Path) -> dict[str, Any] | None:
    root = _closeouts_root(target)
    if not root.is_dir():
        return None
    payloads: list[dict[str, Any]] = []
    for path in root.glob("*/closeout.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("path", str(path))
            payloads.append(payload)
    payloads.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return payloads[0] if payloads else None


def closeout(*, target: Path, reason: str | None = None, defer: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = _config_or_default(target)
        queue = _load_json_file(_queue_path(target, config))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: cannot read memory-care queue: {exc}", file=sys.stderr)
        return 2
    cards = queue.get("cards") if isinstance(queue, dict) and isinstance(queue.get("cards"), list) else []
    fingerprints = [
        str(card.get("source_fingerprint"))
        for card in cards
        if isinstance(card, dict) and isinstance(card.get("source_fingerprint"), str)
    ]
    closeout_id = f"{_utc_iso().replace(':', '').replace('+', 'Z')}-memory-care-closeout"
    payload = {
        "closeout_id": closeout_id,
        "created_at": _utc_iso(),
        "status": "deferred" if defer else "reviewed",
        "reason": reason or "",
        "candidate_count": len(cards),
        "source_fingerprints": fingerprints,
        "safe_summary": f"{len(cards)} memory-care candidate(s) {'deferred' if defer else 'reviewed'}",
    }
    work_cmd._write_json(_closeouts_root(target) / closeout_id / "closeout.json", payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory_care_closeout: {closeout_id}")
    print(f"status: {payload['status']}")
    print(f"candidates: {payload['candidate_count']}")
    return 0


# --- memory search (deterministic keyword search over cards) ---


def _card_search_fields(path: Path, target: Path) -> dict[str, Any]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    frontmatter, _ = _parse_frontmatter(text)
    tags = frontmatter.get("tags")
    tags_list = [str(t) for t in tags] if isinstance(tags, list) else ([str(tags)] if tags else [])
    return {
        "rel": str(path.relative_to(target)),
        "title": str(frontmatter.get("title") or path.stem),
        "tags": tags_list,
        "summary": str(frontmatter.get("description") or frontmatter.get("summary") or ""),
        "body": text,
    }


def search_cards_payload(target: Path, query: str, *, limit: int = 20) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config = load_config(target) or MemoryCareConfig()
    terms = [term for term in query.lower().split() if term]
    matches: list[dict[str, Any]] = []
    for path in _iter_cards(target, config):
        fields = _card_search_fields(path, target)
        if not fields:
            continue
        heading = f"{fields['title']} {' '.join(fields['tags'])} {fields['summary']}".lower()
        body = fields["body"].lower()
        score = 0
        for term in terms:
            if term in heading:
                score += 3
            elif term in body:
                score += 1
        if not terms or score == 0:
            continue
        matches.append(
            {
                "path": fields["rel"],
                "title": fields["title"],
                "tags": fields["tags"],
                "summary": fields["summary"],
                "score": score,
            }
        )
    matches.sort(key=lambda item: (-item["score"], item["path"]))
    return {
        "target": str(target),
        "query": query,
        "match_count": len(matches),
        "matches": matches[:limit],
    }


def search(*, target: Path, query: str, limit: int = 20, json_output: bool = False) -> int:
    payload = search_cards_payload(target, query, limit=limit)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory search: {payload['query']} ({payload['match_count']} match(es))")
    for match in payload["matches"]:
        print(f"- {match['path']}  [{match['score']}]  {match['title']}")
    return 0


# --- read-only MCP server exposing memory cards over card:// ---

_CARD_URI_PREFIX = "card://"


def _card_uri(rel: str) -> str:
    return f"{_CARD_URI_PREFIX}{rel}"


def _mcp_card_resources(target: Path, config: MemoryCareConfig) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in _iter_cards(target, config):
        fields = _card_search_fields(path, target)
        if not fields:
            continue
        items.append(
            {
                "uri": _card_uri(fields["rel"]),
                "name": fields["title"],
                "mimeType": "text/markdown",
                "description": fields["summary"],
            }
        )
    return items


def _mcp_read_card(target: Path, config: MemoryCareConfig, uri: str) -> tuple[str, str] | tuple[None, None]:
    ref = uri[len(_CARD_URI_PREFIX) :] if uri.startswith(_CARD_URI_PREFIX) else uri
    candidate = (target / ref).resolve()
    # Only serve files inside the configured card roots; never traverse outside.
    for path in _iter_cards(target, config):
        if path.resolve() == candidate:
            try:
                return path.read_text(errors="replace"), "text/markdown"
            except OSError:
                return None, None
    return None, None


def _mcp_card_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_cards",
            "description": "List memory cards (path and title).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_card",
            "description": "Read one memory card by repo-relative path or card:// uri.",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
        {
            "name": "search_cards",
            "description": "Keyword-search memory cards; returns ranked path/title matches.",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    ]


def _mcp_card_tool_call(
    target: Path, config: MemoryCareConfig, name: str, arguments: dict[str, Any]
) -> tuple[object, bool]:
    if name == "list_cards":
        return (
            [
                {"path": str(p.relative_to(target)), "uri": _card_uri(str(p.relative_to(target)))}
                for p in _iter_cards(target, config)
            ],
            False,
        )
    if name == "get_card":
        ref = str(arguments.get("path") or "")
        text, _ = _mcp_read_card(target, config, ref)
        if text is None:
            return (f"card not found: {ref}", True)
        return (text, False)
    if name == "search_cards":
        return (search_cards_payload(target, str(arguments.get("query") or ""))["matches"], False)
    return (f"unknown tool: {name}", True)


def _mcp_response(
    request_id: object, *, result: object | None = None, error: dict[str, Any] | None = None
) -> dict[str, Any]:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result if result is not None else {}
    return response


def _run_card_mcp_stdio(target: Path) -> int:
    config = load_config(target) or MemoryCareConfig()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_mcp_response(None, error={"code": -32700, "message": "parse error"})), flush=True)
            continue
        if not isinstance(request, dict):
            print(json.dumps(_mcp_response(None, error={"code": -32600, "message": "invalid request"})), flush=True)
            continue
        request_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if request_id is None and method.startswith("notifications/"):
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"resources": {}, "tools": {}},
                "serverInfo": {"name": "brigade-memory-readonly", "version": "0"},
            }
            print(json.dumps(_mcp_response(request_id, result=result), sort_keys=True), flush=True)
        elif method == "resources/list":
            print(
                json.dumps(
                    _mcp_response(request_id, result={"resources": _mcp_card_resources(target, config)}), sort_keys=True
                ),
                flush=True,
            )
        elif method == "resources/read":
            uri = str(params.get("uri") or "")
            text, mime_type = _mcp_read_card(target, config, uri)
            if text is None:
                print(
                    json.dumps(
                        _mcp_response(request_id, error={"code": -32004, "message": f"resource not found: {uri}"}),
                        sort_keys=True,
                    ),
                    flush=True,
                )
            else:
                print(
                    json.dumps(
                        _mcp_response(
                            request_id, result={"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]}
                        ),
                        sort_keys=True,
                    ),
                    flush=True,
                )
        elif method == "tools/list":
            print(
                json.dumps(_mcp_response(request_id, result={"tools": _mcp_card_tool_specs()}), sort_keys=True),
                flush=True,
            )
        elif method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            payload, failed = _mcp_card_tool_call(target, config, name, arguments)
            text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True)
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
            print(json.dumps(_mcp_response(request_id, result=result), sort_keys=True), flush=True)
        else:
            print(
                json.dumps(
                    _mcp_response(request_id, error={"code": -32601, "message": f"method not found: {method}"}),
                    sort_keys=True,
                ),
                flush=True,
            )
    return 0


def serve_mcp(*, target: Path, stdio: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if stdio:
        return _run_card_mcp_stdio(target)
    config = load_config(target) or MemoryCareConfig()
    resources = _mcp_card_resources(target, config)
    payload = {
        "target": str(target),
        "read_only": True,
        "resource_scheme": "card://<repo-relative-path>",
        "tools": [spec["name"] for spec in _mcp_card_tool_specs()],
        "registered_resources": len(resources),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("memory MCP resources: ready read_only=true")
    print("resources: card://<repo-relative-path>")
    print(f"tools: {', '.join(payload['tools'])}")
    print(f"registered_resources: {payload['registered_resources']}")
    print("run the stdio server with: brigade memory serve-mcp --stdio")
    return 0
