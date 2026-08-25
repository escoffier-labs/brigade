"""Local memory card decay scanner."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import mcp_server, runbook_cmd, toml_compat as tomllib, work_cmd
from .budgets import MEMORY_CARD_BUDGET_BYTES
from .install import apply_gitignore
from .selection import Selection
from .localio import (
    read_jsonl_dicts as _read_jsonl_dicts,
    write_json as _write_json,
    write_text_atomic as _write_text_atomic,
    utc_now_iso_z as _utc_iso,
)
from .memory_doctor.safety import atomic_write_text
from .card_identity import (
    CardIdentity,
    IdentityIndex,
    card_identity,
    identity_collision_aliases,
    mapping_old_id,
    mint_claimed_card_id,
    valid_card_id,
)
from .templates import template_root

CONFIG_REL_PATH = ".brigade/memory-care.toml"
DEFAULT_OUTPUT_PATH = ".brigade/memory-care/decay"
MEMORY_CARE_RUNBOOKS_REL = ".brigade/memory-care/runbooks"
MEMORY_CARE_RUNBOOK_NAMES = (
    "daily-care-pass.json",
    "ingest-sweep.json",
    "weekly-outcome-ratchet.json",
)
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
    "missing-evidence-ref",
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
    max_card_bytes: int = MEMORY_CARD_BUDGET_BYTES


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


def memory_care_producer_artifacts(target: Path, config: MemoryCareConfig) -> list[tuple[Path, str]]:
    """Return the absolute artifact paths produced by the Brigade memory-care scanner.

    Uses the write location (`_output_dir`), not the reader fallback, so
    producer-collision checks compare actual write destinations.
    """
    output = _output_dir(target, config)
    return [
        (output / "scan-latest.json", "brigade memory-care"),
        (output / "refresh-queue.json", "brigade memory-care"),
    ]


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
        max_card_bytes=_positive_int(
            data.get("max_card_bytes"), field="max_card_bytes", default=MEMORY_CARD_BUDGET_BYTES, minimum=1
        ),
    )


def _config_or_default(target: Path) -> MemoryCareConfig:
    return load_config(target) or MemoryCareConfig()


def _memory_care_runbooks_dir(target: Path) -> Path:
    return target / MEMORY_CARE_RUNBOOKS_REL


def _write_memory_care_runbooks(target: Path) -> int:
    """Copy pinned mechanical memory-care runbook templates into the target.

    Templates ship without pins; pins are materialized from the resolved
    ``brigade`` binary at write time so ``brigade runbook run --approved``
    can enforce the binary hash without a separate pin step.

    Writes into a sibling temp directory first so pin-resolution failures do
    not leave a partial runbook tree behind.
    """
    template_dir = template_root() / "memory-care" / "runbooks"
    dest_dir = _memory_care_runbooks_dir(target)
    temp_dir = dest_dir.parent / f".{dest_dir.name}.init-tmp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    try:
        for name in MEMORY_CARE_RUNBOOK_NAMES:
            src = template_dir / name
            if not src.is_file():
                print(f"error: memory-care runbook template missing: {src}", file=sys.stderr)
                return 4
            try:
                payload = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                print(f"error: invalid memory-care runbook template {src}: {exc}", file=sys.stderr)
                return 2
            if not isinstance(payload, dict):
                print(f"error: memory-care runbook template must be a JSON object: {src}", file=sys.stderr)
                return 2
            # Validate shape through the same reader runbook execution uses, then
            # materialize pins without echoing pin() stdout into care init output.
            dest = temp_dir / name
            _write_json(dest, payload)
            validated, error = runbook_cmd._read_runbook(dest, require_pin_hashes=False)
            if validated is None:
                print(f"error: {error}", file=sys.stderr)
                return 2
            pins, pin_error = runbook_cmd._pin_payload_from_runbook(target, validated)
            if pins is None:
                print(f"error: {pin_error}", file=sys.stderr)
                return 2
            validated["pins"] = pins
            _write_json(dest, validated)
        # Narrow crash window: after rmtree the old tree is gone; if replace fails
        # before publishing temp_dir, neither directory remains at dest_dir.
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        temp_dir.replace(dest_dir)
        return 0
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def init(
    *,
    target: Path,
    force: bool = False,
    update_gitignore: bool = True,
    with_runbooks: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = config_path(target)
    if path.exists() and not force:
        print(f"error: memory-care config already exists: {path}", file=sys.stderr)
        return 1
    if with_runbooks:
        rc = _write_memory_care_runbooks(target)
        if rc != 0:
            return rc
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _format_config())
    if update_gitignore:
        apply_gitignore(target, Selection(depth="repo", harnesses=[], owner="this-repo", includes=[]))
    print(f"memory_care_config: {path}")
    print(f"output_path: {DEFAULT_OUTPUT_PATH}")
    if with_runbooks:
        print(f"memory_care_runbooks: {_memory_care_runbooks_dir(target)}")
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
            if path.is_symlink():
                continue
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
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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


def _evidence_refs(meta: dict[str, Any]) -> list[str]:
    value = _frontmatter_value(meta, "evidence", "sources", "source", "refs", "links")
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    rendered = str(value).strip()
    return [rendered] if rendered else []


def _miseledger_evidence_id(ref: str) -> str | None:
    if ref.startswith("miseledger://evidence/"):
        return ref.removeprefix("miseledger://evidence/").strip()
    if ref.startswith("miseledger:"):
        return ref.removeprefix("miseledger:").strip()
    return None


def _miseledger_evidence_path(bundle_id: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", bundle_id):
        return None
    cache_home = Path.home() / ".cache"
    if raw := os.environ.get("XDG_CACHE_HOME"):
        cache_home = Path(raw)
    return cache_home / "miseledger" / "evidence" / f"{bundle_id}.json"


def _tracked_evidence_ref_missing(target: Path, ref: str) -> str | None:
    bundle_id = _miseledger_evidence_id(ref)
    if bundle_id is not None:
        path = _miseledger_evidence_path(bundle_id)
        if path is None:
            return f"invalid MiseLedger evidence id: {ref}"
        return None if path.is_file() else f"missing MiseLedger evidence bundle: {ref}"

    receipt_ref = ref.removeprefix("receipt:").strip()
    if not (receipt_ref.endswith("receipt.json") or receipt_ref.startswith(".brigade/") or "/.brigade/" in receipt_ref):
        return None
    path = Path(receipt_ref)
    if path.is_absolute():
        try:
            path.resolve().relative_to(target.resolve())
        except ValueError:
            return f"receipt path escapes target: {ref}"
    else:
        path = target / path
    return None if path.is_file() else f"missing receipt reference: {ref}"


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
    record: dict[str, Any] = {
        "id": f"memory-care-{fingerprint}",
        "file": card_path,
        "path": card_path,
        "card_file": card_path,
        "card_id": card_id,
        "card_aliases": [card_path, Path(card_path).stem],
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
    identities: list[tuple[str, CardIdentity]] = []
    identities_by_path: dict[str, CardIdentity] = {}
    enabled = set(config.enabled_checks)

    for path in cards:
        rel = str(path.relative_to(target))
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, has_frontmatter = _parse_frontmatter(text)
        identity = card_identity(meta, rel)
        card_id = identity.card_id
        by_id.setdefault(card_id, []).append(rel)
        identities.append((rel, identity))
        identities_by_path[rel] = identity
        row = {
            "file": rel,
            "card_id": card_id,
            "card_aliases": list(identity.aliases),
            "has_frontmatter": has_frontmatter,
            "bytes": path.stat().st_size,
        }
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
        evidence_refs = _evidence_refs(meta)
        missing_evidence_refs = [
            detail for ref in evidence_refs if (detail := _tracked_evidence_ref_missing(target, ref)) is not None
        ]
        missing_evidence = config.require_evidence and not has_evidence
        row["confidence"] = confidence
        row["has_evidence"] = has_evidence
        row["evidence_pointers"] = evidence_refs
        row["evidence_ref_count"] = len(evidence_refs)
        row["missing_evidence_ref_count"] = len(missing_evidence_refs)
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
        if "missing-evidence-ref" in enabled and missing_evidence_refs:
            issues.append(
                _issue(
                    target=target,
                    card_path=rel,
                    card_id=card_id,
                    issue_type="missing-evidence-ref",
                    severity="high",
                    summary=f"{rel} cites evidence references that no longer resolve.",
                    evidence=missing_evidence_refs,
                    action="Regenerate the evidence bundle, restore the receipt, or remove the stale citation.",
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

        for alias, alias_paths in identity_collision_aliases(identities).items():
            for rel in alias_paths:
                identity = identities_by_path[rel]
                issues.append(
                    _issue(
                        target=target,
                        card_path=rel,
                        card_id=identity.card_id,
                        issue_type="contradictory",
                        severity="medium",
                        summary=f"Card identity alias {alias!r} appears in multiple cards.",
                        evidence=list(alias_paths),
                        action="Resolve the alias collision before applying a reviewed card rewrite.",
                    )
                )

    for issue in issues:
        issue_identity = identities_by_path.get(str(issue.get("card_file") or ""))
        if issue_identity is not None:
            issue["card_aliases"] = list(issue_identity.aliases)

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
        "evidence_refs": {
            "present": sum(int(row.get("evidence_ref_count") or 0) for row in card_rows),
            "missing": sum(int(row.get("missing_evidence_ref_count") or 0) for row in card_rows),
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
        "identity_collisions": identity_collision_aliases(identities),
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
    scan_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    queue_path.write_text(json.dumps(_queue_payload(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return scan_path, queue_path


def _load_scan_payload(target: Path, config: MemoryCareConfig) -> dict[str, Any] | None:
    return _load_json_file(_scan_path(target, config))


def _archive_candidates_from_scan(scan_payload: dict[str, Any] | None, config: MemoryCareConfig) -> dict[str, Any]:
    """Derive a read-only archive-candidate report from a saved scan payload.

    Never walks the card tree. Cards are candidates only when last_reviewed is
    parseable and age_days > 2 * stale_after_days (strict). Missing, invalid, or
    future reviewed dates are unassessable and never inferred from mtime/git.
    A saved scan without a parseable scan_date is fully unassessable; today is
    never substituted for the saved-scan anchor.
    """
    ttl_days = int(config.stale_after_days)
    candidate_after_days = ttl_days * 2
    scan_date_raw = scan_payload.get("scan_date") if isinstance(scan_payload, dict) else None
    scan_date = _parse_date(scan_date_raw) if scan_date_raw else None
    cards_value = scan_payload.get("cards") if isinstance(scan_payload, dict) else None
    cards = cards_value if isinstance(cards_value, list) else []
    candidates: list[dict[str, Any]] = []
    unassessable_count = 0
    for card in cards:
        if not isinstance(card, dict):
            continue
        reviewed = _parse_date(card.get("last_reviewed"))
        if scan_date is None or reviewed is None:
            unassessable_count += 1
            continue
        age_days = (scan_date - reviewed).days
        if age_days < 0:
            unassessable_count += 1
            continue
        if age_days <= candidate_after_days:
            continue
        evidence_value = card.get("evidence_pointers")
        if isinstance(evidence_value, list):
            evidence_pointers = [str(item).strip() for item in evidence_value if str(item).strip()]
        else:
            evidence_pointers = []
        fresh_until = card.get("fresh_until")
        candidates.append(
            {
                "card_id": str(card.get("card_id") or ""),
                "file": str(card.get("file") or ""),
                "age_days": age_days,
                "last_reviewed": reviewed.isoformat(),
                "fresh_until": fresh_until if isinstance(fresh_until, str) or fresh_until is None else str(fresh_until),
                "evidence_pointers": evidence_pointers,
                "reason": "review_age_exceeds_2x_ttl",
                "approval_required": True,
            }
        )
    return {
        "schema_version": 1,
        "read_only": True,
        "approval_required": True,
        "would_archive": False,
        "scan_date": scan_date.isoformat() if scan_date is not None else None,
        "threshold": {
            "ttl_days": ttl_days,
            "candidate_after_days": candidate_after_days,
            "comparison": "age_days > candidate_after_days",
        },
        "candidate_count": len(candidates),
        "unassessable_count": unassessable_count,
        "candidates": candidates,
    }


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
            text = path.read_text(encoding="utf-8", errors="replace")
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
    issues_value = scan_payload.get("issues") if isinstance(scan_payload, dict) else None
    issues = issues_value if isinstance(issues_value, list) else []
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
    closed, closeout_ignored_reason = _closeout_quiet_set(closeout)
    top_issue = None
    queue_count = 0
    if isinstance(queue_payload, dict) and isinstance(queue_payload.get("cards"), list) and queue_payload["cards"]:
        open_cards = [
            card
            for card in queue_payload["cards"]
            if isinstance(card, dict) and str(card.get("source_fingerprint") or "") not in closed
        ]
        queue_count = len(open_cards)
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
    scan_cards = scan_payload.get("cards") if isinstance(scan_payload, dict) else None
    if isinstance(scan_cards, list):
        explicit_ids = 0
        fallback_ids = 0
        for row in scan_cards:
            if not isinstance(row, dict) or not row.get("has_frontmatter"):
                continue
            if valid_card_id(row.get("card_id")):
                explicit_ids += 1
            else:
                fallback_ids += 1
        if fallback_ids:
            checks.append(
                {
                    "status": "warn",
                    "name": "memory_card_ids",
                    "detail": (
                        f"{fallback_ids} cards use path-fallback identity; "
                        "missing IDs stay a warning until audited coverage is 100 percent"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "status": "ok",
                    "name": "memory_card_ids",
                    "detail": (f"{explicit_ids} cards have explicit IDs" if explicit_ids else "no cards"),
                }
            )
    issues = [check for check in checks if check["status"] != "ok"]
    metadata = (
        scan_payload.get("metadata")
        if isinstance(scan_payload, dict) and isinstance(scan_payload.get("metadata"), dict)
        else {}
    )
    if isinstance(scan_payload, dict) and isinstance(scan_payload.get("issue_count"), int):
        scan_issue_count: int | None = int(scan_payload["issue_count"])
    elif isinstance(scan_payload, dict) and isinstance(scan_payload.get("issues"), list):
        scan_issue_count = len(scan_payload["issues"])
    else:
        scan_issue_count = None
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
    autofix_items_value = autofix_plan.get("items")
    autofix_items = autofix_items_value if isinstance(autofix_items_value, list) else []
    archive_candidates = _archive_candidates_from_scan(scan_payload if isinstance(scan_payload, dict) else None, config)
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "scan_path": str(scan_path),
        "queue_path": str(queue_path),
        "valid": not any(check["status"] == "fail" for check in checks),
        "issue_count": len(issues),
        "scan_issue_count": scan_issue_count,
        "queue_count": queue_count,
        "top_issue": top_issue or (issues[0] if issues else None),
        "checks": checks,
        "metadata": metadata,
        "autofix_plan": {
            "plan_count": autofix_plan.get("plan_count", 0),
            "blocked_count": autofix_plan.get("blocked_count", 0),
            "top_item": autofix_items[0] if autofix_items else None,
            "suggested_next_command": "brigade memory care plan-fixes" if autofix_plan.get("plan_count") else None,
            "would_write": False,
        },
        "archive_candidates": archive_candidates,
        "latest_closeout": closeout,
        "latest_closeout_ignored_reason": closeout_ignored_reason,
        "search_recall": search_recall_status(target),
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
    metadata_value = payload.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    reviewed_value = metadata.get("reviewed_dates")
    reviewed = reviewed_value if isinstance(reviewed_value, dict) else None
    freshness_value = metadata.get("freshness_dates")
    freshness = freshness_value if isinstance(freshness_value, dict) else None
    evidence_value = metadata.get("evidence")
    evidence = evidence_value if isinstance(evidence_value, dict) else None
    evidence_refs_value = metadata.get("evidence_refs")
    evidence_refs = evidence_refs_value if isinstance(evidence_refs_value, dict) else None
    confidence_value = metadata.get("confidence")
    confidence = confidence_value if isinstance(confidence_value, dict) else None
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
    if evidence_refs:
        print(f"evidence_refs: present={evidence_refs.get('present', 0)} missing={evidence_refs.get('missing', 0)}")
    if confidence:
        rendered = ", ".join(f"{key}={confidence[key]}" for key in sorted(confidence))
        print(f"confidence_metadata: {rendered}")
    autofix_plan_value = payload.get("autofix_plan")
    autofix_plan = autofix_plan_value if isinstance(autofix_plan_value, dict) else {}
    if autofix_plan.get("plan_count"):
        print(
            f"autofix_plan: planned={autofix_plan.get('plan_count')} blocked={autofix_plan.get('blocked_count')} would_write=false"
        )
        if autofix_plan.get("suggested_next_command"):
            print(f"autofix_plan_command: {autofix_plan.get('suggested_next_command')}")
    top = payload.get("top_issue") if isinstance(payload.get("top_issue"), dict) else None
    if top:
        print(f"top_issue: {top.get('issue_type') or top.get('name')} {top.get('file') or top.get('detail')}")
    archive_value = payload.get("archive_candidates")
    archive = archive_value if isinstance(archive_value, dict) else {}
    threshold_value = archive.get("threshold")
    threshold = threshold_value if isinstance(threshold_value, dict) else {}
    print(
        "archive_candidates: "
        f"count={archive.get('candidate_count', 0)} "
        f"unassessable={archive.get('unassessable_count', 0)} "
        f"threshold=age_days>{threshold.get('candidate_after_days', 0)} "
        f"approval_required={archive.get('approval_required', True)} "
        f"would_archive={archive.get('would_archive', False)}"
    )
    candidates_value = archive.get("candidates")
    candidates = candidates_value if isinstance(candidates_value, list) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        pointers_value = candidate.get("evidence_pointers")
        pointers = pointers_value if isinstance(pointers_value, list) else []
        print(
            f"  archive_candidate: {candidate.get('file')} "
            f"age_days={candidate.get('age_days')} "
            f"last_reviewed={candidate.get('last_reviewed')} "
            f"evidence={','.join(str(item) for item in pointers) if pointers else '-'}"
        )
    recall_value = payload.get("search_recall")
    recall = recall_value if isinstance(recall_value, dict) else {}
    print(
        "search_recall: "
        f"searches={recall.get('searches', 0)} "
        f"followup_rate={recall.get('followup_rate', 0.0)} "
        f"(second-class; informs, never gates)"
    )
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
    """Cards with frontmatter but missing review/freshness/fingerprint/id metadata.

    The derived `last_reviewed` is the card file's last git commit date (the
    last time anyone touched the fact), falling back to file mtime outside git.
    `fresh_until` is the derived or existing reviewed date plus the configured
    stale window. `fingerprint` is a stable hash of normalized card content.
    Existing values are never proposed for change. A complete care card with no
    valid explicit ID is still a candidate so coverage can reach 100 percent.
    """
    from .card_fingerprint import content_fingerprint

    candidates: list[dict[str, Any]] = []
    skipped_no_frontmatter = 0
    from .card_fingerprint import read_text_nofollow

    claimed_ids: IdentityIndex[str] = IdentityIndex()
    pending: list[tuple[Path, str, str, dict[str, Any]]] = []
    for path in _iter_cards(target, config):
        rel = str(path.relative_to(target))
        try:
            text = read_text_nofollow(path)
        except OSError:
            continue
        meta, has_frontmatter = _parse_frontmatter(text)
        if not has_frontmatter:
            skipped_no_frontmatter += 1
            continue
        identity = card_identity(meta, rel)
        if identity.explicit:
            claimed_ids.claim(identity.card_id, rel)
        pending.append((path, rel, text, meta))

    for path, rel, text, meta in pending:
        reviewed = _parse_date(_frontmatter_value(meta, "last_reviewed", "last_reviewed_at", "reviewed_at"))
        expiry = _parse_date(_frontmatter_value(meta, "fresh_until", "expires_at", "expires"))
        has_fingerprint = bool(str(meta.get("fingerprint") or "").strip())
        identity = card_identity(meta, rel)
        care_complete = reviewed is not None and expiry is not None and has_fingerprint
        if care_complete and identity.explicit:
            continue
        candidate: dict[str, Any] = {
            "file": rel,
            "source": "identity",
            "fields": [],
        }
        if not care_complete:
            derived = _git_last_commit_date(target, rel)
            source = "git-history"
            if derived is None:
                from datetime import datetime as _dt, timezone as _tz

                derived = _dt.fromtimestamp(path.stat().st_mtime, tz=_tz.utc).date()
                source = "file-mtime"
            base_reviewed = reviewed or derived
            from datetime import timedelta as _td

            candidate["source"] = source
            if reviewed is None:
                candidate["last_reviewed"] = derived.isoformat()
                candidate["fields"].append("last_reviewed")
            if expiry is None:
                candidate["fresh_until"] = (base_reviewed + _td(days=config.stale_after_days)).isoformat()
                candidate["fields"].append("fresh_until")
            if not has_fingerprint:
                candidate["fingerprint"] = content_fingerprint(text)
                candidate["fields"].append("fingerprint")
                if reviewed is not None and expiry is not None:
                    candidate["source"] = "content-hash"
        if not identity.explicit:
            candidate["id"] = mint_claimed_card_id(claimed_ids, rel, seed=f"path:{rel}")
            candidate["fields"].append("id")
            candidate["old_id"] = mapping_old_id(identity, rel)
        candidates.append(candidate)
    return candidates, skipped_no_frontmatter


def _backfill_write(target: Path, candidate: dict[str, Any]) -> None:
    from .card_fingerprint import read_text_nofollow, write_text_nofollow_atomic

    path = target / candidate["file"]
    text = read_text_nofollow(path)
    lines = text.split("\n")
    # _parse_frontmatter guarantees a leading `---` block; insert the new keys
    # just before its closing fence, leaving every other byte untouched.
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    additions = [f"{field}: {candidate[field]}" for field in candidate["fields"]]
    write_text_nofollow_atomic(
        path,
        "\n".join(lines[:closing] + additions + lines[closing:]),
    )


def _identity_coverage(target: Path, identity_collisions: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """Consume #844's read-only audit plus alias collisions. No card bodies."""
    from . import memory_identity_audit

    audit = memory_identity_audit.audit_workspaces([target])
    summary = audit["summary"]
    return {
        "explicit_ids": int(summary["explicit_ids"]),
        "path_fallbacks": int(summary["path_fallbacks"]),
        "malformed_ids": int(summary["malformed_ids"]),
        "duplicate_ids": int(summary["duplicate_ids_within_namespace"]),
        "alias_collisions": len(identity_collisions),
    }


def _identity_mapping_receipt(
    *,
    candidates: list[dict[str, Any]],
    coverage: dict[str, int],
    identity_collisions: dict[str, tuple[str, ...]],
    dry_run: bool,
    written_count: int,
    skipped_no_frontmatter: int,
    stale_after_days: int,
) -> dict[str, Any]:
    mappings = [
        {
            "path": str(candidate["file"]),
            "old_id": str(candidate.get("old_id") or f"path:{candidate['file']}"),
            "new_id": candidate.get("id"),
        }
        for candidate in sorted(candidates, key=lambda item: str(item["file"]))
        if candidate.get("id")
    ]
    return {
        "schema": "brigade.memory-identity-mapping.v1",
        "dry_run": dry_run,
        "generated_at": _utc_iso(),
        "written_count": written_count,
        "skipped_no_frontmatter": skipped_no_frontmatter,
        "stale_after_days": stale_after_days,
        "coverage": coverage,
        "alias_collisions": {key: list(paths) for key, paths in identity_collisions.items()},
        "missing_ids_validation": "enabled" if coverage["path_fallbacks"] == 0 else "warning",
        "identity_mapping": [{"file": item["path"], "card_id": item["new_id"]} for item in mappings],
        "mappings": mappings,
    }


def backfill(*, target: Path, apply: bool = False, json_output: bool = False) -> int:
    """Plan or apply safe metadata backfill for cards missing review/freshness/fingerprint."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = load_config(target) or MemoryCareConfig()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    identity_collisions = _scan_payload(target, config)["identity_collisions"]
    coverage = _identity_coverage(target, identity_collisions)
    blocking = bool(identity_collisions) or coverage["duplicate_ids"] > 0
    if apply and blocking:
        collision_payload = {
            "target": str(target),
            "apply": apply,
            "written_count": 0,
            "identity_collisions": identity_collisions,
            "identity_receipt": _identity_mapping_receipt(
                candidates=[],
                coverage=coverage,
                identity_collisions=identity_collisions,
                dry_run=True,
                written_count=0,
                skipped_no_frontmatter=0,
                stale_after_days=config.stale_after_days,
            ),
            "error": "identity alias collisions must be resolved before reviewed backfill",
        }
        if json_output:
            print(json.dumps(collision_payload, indent=2, sort_keys=True))
        else:
            print(f"error: {collision_payload['error']}", file=sys.stderr)
        return 2
    candidates, skipped_no_frontmatter = _backfill_candidates(target, config)
    written = 0
    receipt_path: Path | None = None
    receipts_dir = target / ".brigade" / "memory-care" / "backfills"
    identity_receipt = _identity_mapping_receipt(
        candidates=candidates,
        coverage=coverage,
        identity_collisions=identity_collisions,
        dry_run=not apply,
        written_count=0,
        skipped_no_frontmatter=skipped_no_frontmatter,
        stale_after_days=config.stale_after_days,
    )
    if apply and candidates:
        for candidate in candidates:
            _backfill_write(target, candidate)
            written += 1
        identity_receipt["dry_run"] = False
        identity_receipt["written_count"] = written
        receipts_dir.mkdir(parents=True, exist_ok=True)
        from .localio import utc_now, write_json

        stamp = utc_now().strftime("%Y%m%dT%H%M%S")
        receipt_path = receipts_dir / f"{stamp}.json"
        write_json(receipt_path, identity_receipt)
    elif not apply and candidates:
        receipts_dir.mkdir(parents=True, exist_ok=True)
        from .localio import write_json

        receipt_path = receipts_dir / "dry-run-mapping.json"
        write_json(receipt_path, identity_receipt)
    payload: dict[str, Any] = {
        "target": str(target),
        "apply": apply,
        "candidate_count": len(candidates),
        "written_count": written,
        "skipped_no_frontmatter": skipped_no_frontmatter,
        "stale_after_days": config.stale_after_days,
        "receipt_path": str(receipt_path) if receipt_path else None,
        "candidates": candidates,
        "identity_receipt": identity_receipt,
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


def _closeout_quiet_set(closeout: dict[str, Any] | None) -> tuple[set[str], str | None]:
    """Return (fingerprints to quiet, reason the receipt was ignored).

    Before 15d54523, closeouts could write status=reviewed with unresolved
    candidates still in the queue. Such legacy receipts are not honored:
    only a reviewed closeout with no candidates/fingerprints, or a deferred
    closeout with a nonblank reason, may quiet matching queue fingerprints.
    """
    if not isinstance(closeout, dict):
        return set(), None
    status = str(closeout.get("status") or "")
    fingerprints = {
        item for item in (closeout.get("source_fingerprints") or []) if isinstance(item, str) and item.strip()
    }
    try:
        candidate_count = int(closeout.get("candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    if status == "deferred":
        if str(closeout.get("reason") or "").strip():
            return fingerprints, None
        return set(), "deferred closeout has a blank reason; not quieting queue fingerprints"
    if status == "reviewed" and candidate_count == 0 and not fingerprints:
        return set(), None
    if status == "reviewed":
        return (
            set(),
            (
                "legacy reviewed closeout carries "
                f"{candidate_count} candidate(s) and {len(fingerprints)} fingerprint(s); not quieting"
            ),
        )
    return set(), None


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
    cards_value = queue.get("cards") if isinstance(queue, dict) else None
    cards = cards_value if isinstance(cards_value, list) else []
    reason_text = (reason or "").strip()
    if defer and not reason_text:
        message = "error: deferred memory-care closeout requires a nonblank --reason"
        if json_output:
            print(
                json.dumps(
                    {"status": "blocked", "error": message, "candidate_count": len(cards)}, indent=2, sort_keys=True
                )
            )
        else:
            print(message, file=sys.stderr)
        return 1
    if not defer and cards:
        message = (
            f"error: memory-care refresh queue has {len(cards)} unresolved candidate(s); "
            "closeout cannot mark them reviewed. Use --defer with a nonblank --reason to "
            "defer explicitly, or resolve the queue first."
        )
        if json_output:
            print(
                json.dumps(
                    {"status": "blocked", "error": message, "candidate_count": len(cards)},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(message, file=sys.stderr)
        return 1
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
        "reason": reason_text,
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

# Label-free recall signal (#723): local search log + follow-up rate.
# Path is host-local under .brigade/; queries and top-K card ids only (no body).
SEARCH_LOG_REL = ".brigade/memory/search-log.jsonl"
# Follow-up window. Too short and legitimate rephrases are missed; too long and
# unrelated successive searches inflate the miss rate. 45s sits in the 30–60s
# band from the issue.
SEARCH_FOLLOWUP_WINDOW_S = 45
# Top-K card ids logged and compared for disjointness. Aligned with the
# retrieval eval harness DEFAULT_K=5 so failed queries in this log are directly
# usable as fixture material for evals/memory-retrieval/.
SEARCH_LOG_TOP_K = 5
# Cap the rolling log; drop oldest beyond N. Personal corpora stay small.
SEARCH_LOG_MAX_ENTRIES = 500
# Dashboard list-all hack uses query ":" (YAML key separators hit everything).
# Logging that would drown the recall signal.
_SEARCH_LOG_SKIP_QUERIES = frozenset({":"})


def _normalize_search_query(query: str) -> str:
    return " ".join(str(query or "").lower().split())


def _search_log_path(target: Path) -> Path:
    return target.expanduser().resolve() / SEARCH_LOG_REL


def _parse_search_log_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _search_log_card_ids(matches: list[dict[str, Any]], *, top_k: int = SEARCH_LOG_TOP_K) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for match in matches[:top_k]:
        if not isinstance(match, dict):
            continue
        card_id = str(match.get("card_id") or match.get("path") or "")
        if not card_id or card_id in seen:
            continue
        seen.add(card_id)
        ids.append(card_id)
    return ids


def _search_log_aliases(matches: list[dict[str, Any]], *, top_k: int = SEARCH_LOG_TOP_K) -> list[str]:
    aliases: list[str] = []
    for match in matches[:top_k]:
        if not isinstance(match, dict):
            continue
        value = match.get("card_aliases")
        if isinstance(value, list):
            aliases.extend(str(alias) for alias in value if str(alias))
    return list(dict.fromkeys(aliases))


def _entry_identity_keys(entry: dict[str, Any]) -> set[str]:
    keys = {str(value) for value in entry.get("card_ids", []) if value}
    keys.update(str(value) for value in entry.get("card_aliases", []) if value)
    return keys


def _should_log_search(query: str) -> bool:
    normalized = _normalize_search_query(query)
    return bool(normalized) and normalized not in _SEARCH_LOG_SKIP_QUERIES


def followup_rate_from_entries(
    entries: list[dict[str, Any]],
    *,
    window_s: int = SEARCH_FOLLOWUP_WINDOW_S,
) -> dict[str, Any]:
    """Compute the rolling follow-up (miss) rate from search-log entries.

    A follow-up within ``window_s`` whose top-K card ids share nothing with the
    prior search counts as a miss. Overlap, or no follow-up within the window,
    counts the prior search as satisfied. Outside-window successors start a
    fresh event. Second-class evidence: informs retrieval investment, never
    gates doctor/valid.
    """
    searches = 0
    misses = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        ts = _parse_search_log_ts(entry.get("ts"))
        if ts is None:
            continue
        card_ids_value = entry.get("card_ids")
        if not isinstance(card_ids_value, list):
            continue
        searches += 1
        followup: dict[str, Any] | None = None
        if index + 1 < len(entries):
            nxt = entries[index + 1]
            if isinstance(nxt, dict):
                next_ts = _parse_search_log_ts(nxt.get("ts"))
                if next_ts is not None and (next_ts - ts).total_seconds() <= window_s:
                    followup = nxt
        if followup is None:
            continue
        next_ids_value = followup.get("card_ids")
        if not isinstance(next_ids_value, list):
            continue
        prior_ids = _entry_identity_keys(entry)
        next_ids = _entry_identity_keys(followup)
        if prior_ids.isdisjoint(next_ids):
            misses += 1
    rate = round(misses / searches, 4) if searches else 0.0
    return {
        "searches": searches,
        "followups": misses,
        "followup_rate": rate,
        "window_s": window_s,
        "top_k": SEARCH_LOG_TOP_K,
    }


def search_recall_status(target: Path) -> dict[str, Any]:
    """Read the local search log and return the rolling follow-up rate payload."""
    path = _search_log_path(target)
    try:
        entries = _read_jsonl_dicts(path)
    except OSError:
        entries = []
    stats = followup_rate_from_entries(entries)
    # Do not emit log_path here: center report build harvests path-like strings
    # from health payloads as receipt references, and a missing (or non-receipt)
    # search log would false-alarm as operator_report_missing_receipt.
    stats["evidence_class"] = "second_class"
    return stats


def _record_search_log(
    target: Path,
    *,
    query: str,
    matches: list[dict[str, Any]],
    ts: str | None = None,
) -> None:
    """Append one search event; fail-open so logging never breaks search."""
    if not _should_log_search(query):
        return
    path = _search_log_path(target)
    entry = {
        "ts": ts or _utc_iso(),
        "query": _normalize_search_query(query),
        "card_ids": _search_log_card_ids(matches if isinstance(matches, list) else []),
        "card_aliases": _search_log_aliases(matches if isinstance(matches, list) else []),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_jsonl_dicts(path)
        existing.append(entry)
        if len(existing) > SEARCH_LOG_MAX_ENTRIES:
            existing = existing[-SEARCH_LOG_MAX_ENTRIES:]
        rendered = "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in existing)
        _write_text_atomic(path, rendered)
    except OSError:
        return


def _card_search_fields(path: Path, target: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    frontmatter, _ = _parse_frontmatter(text)
    identity = card_identity(frontmatter, str(path.relative_to(target)))
    tags = frontmatter.get("tags")
    tags_list = [str(t) for t in tags] if isinstance(tags, list) else ([str(tags)] if tags else [])
    return {
        "rel": str(path.relative_to(target)),
        "card_id": identity.card_id,
        "card_aliases": list(identity.aliases),
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
                "card_id": fields["card_id"],
                "card_aliases": fields["card_aliases"],
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
    try:
        payload = search_cards_payload(target, query, limit=limit)
    except ValueError as exc:
        print(f"error: invalid memory-care config: {exc}", file=sys.stderr)
        return 2
    # Side-effect only on the CLI path so search_cards_payload (and the
    # retrieval eval harness that calls it) stay pure.
    _record_search_log(target, query=query, matches=payload.get("matches") or [])
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory search: {payload['query']} ({payload['match_count']} match(es))")
    for match in payload["matches"]:
        print(f"- {match['path']}  [{match['score']}]  {match['title']}")
    return 0


def recall(*, target: Path, cwd: Path, limit: int = 5, json_output: bool = False) -> int:
    """Bounded session-start recall over deterministic card search (#466 Slice 1)."""
    from . import memory_hooks

    return memory_hooks.recall(target=target, cwd=cwd, limit=limit, json_output=json_output)


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
    target_resolved = target.expanduser().resolve()
    candidate = (target_resolved / ref).resolve()
    # Only serve files whose real path stays inside the workspace; a `.md` symlink
    # pointing outside a card root must not become a read primitive.
    if not candidate.is_relative_to(target_resolved):
        return None, None
    for path in _iter_cards(target, config):
        if path.is_symlink():
            continue
        if path.resolve() == candidate:
            try:
                return path.read_text(encoding="utf-8", errors="replace"), "text/markdown"
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


def _run_card_mcp_stdio(target: Path) -> int:
    config = load_config(target) or MemoryCareConfig()
    return mcp_server.serve_stdio(
        server_name="brigade-memory-readonly",
        list_resources=lambda: _mcp_card_resources(target, config),
        read_resource=lambda uri: _mcp_read_card(target, config, uri),
        list_tools=_mcp_card_tool_specs,
        call_tool=lambda name, arguments: _mcp_card_tool_call(target, config, name, arguments),
    )


def serve_mcp(*, target: Path, stdio: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    try:
        config = load_config(target) or MemoryCareConfig()
    except ValueError as exc:
        print(f"error: invalid memory-care config: {exc}", file=sys.stderr)
        return 2
    if stdio:
        return _run_card_mcp_stdio(target)
    resources = _mcp_card_resources(target, config)
    tools = [str(spec["name"]) for spec in _mcp_card_tool_specs()]
    payload: dict[str, Any] = {
        "target": str(target),
        "read_only": True,
        "resource_scheme": "card://<repo-relative-path>",
        "tools": tools,
        "registered_resources": len(resources),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("memory MCP resources: ready read_only=true")
    print("resources: card://<repo-relative-path>")
    print(f"tools: {', '.join(tools)}")
    print(f"registered_resources: {payload['registered_resources']}")
    print("run the stdio server with: brigade memory serve-mcp --stdio")
    return 0
