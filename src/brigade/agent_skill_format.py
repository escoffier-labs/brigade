"""Dependency-free validation for the observable Agent Skills frontmatter contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

KNOWN_FIELDS = frozenset({"name", "description", "license", "compatibility", "metadata", "allowed-tools"})
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Validation:
    fields: dict[str, object]
    errors: tuple[str, ...]
    diagnostics: tuple[str, ...]


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed if isinstance(parsed, str) else value
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _frontmatter(text: str) -> tuple[list[str] | None, str | None]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None, "SKILL.md must start with exact YAML frontmatter delimiter ---"
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None, "SKILL.md frontmatter is not closed with exact delimiter ---"
    return lines[1:end], None


def validate(skill_dir: Path, *, mode: str) -> Validation:
    if mode not in {"strict", "lenient"}:
        raise ValueError("skill validation mode must be strict or lenient")
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        return Validation({}, (f"SKILL.md not found: {path}",), ())
    lines, framing_error = _frontmatter(path.read_text(errors="replace"))
    if lines is None:
        if mode == "lenient":
            return Validation({}, (), (framing_error or "invalid frontmatter",))
        return Validation({}, (framing_error or "invalid frontmatter",), ())
    fields: dict[str, object] = {}
    unknown: dict[str, str] = {}
    metadata: dict[str, str] = {}
    current_map: str | None = None
    errors: list[str] = []
    for number, line in enumerate(lines, start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace():
            if current_map != "metadata" or ":" not in line:
                errors.append(f"frontmatter line {number} has unsupported nesting")
                continue
            key, raw = line.strip().split(":", 1)
            value = _scalar(raw)
            if not key or not value:
                errors.append(f"metadata line {number} must contain string key and value")
            else:
                metadata[key] = value
            continue
        current_map = None
        if ":" not in line:
            errors.append(f"frontmatter line {number} must be key: value")
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if key not in KNOWN_FIELDS:
            unknown[key] = _scalar(raw)
            continue
        if key == "metadata":
            current_map = key
            if raw.strip() not in {"", "{}"}:
                errors.append("metadata must be a string-to-string mapping")
            continue
        value = _scalar(raw)
        fields[key] = value
    if metadata:
        fields["metadata"] = metadata
    diagnostics = [f"unknown frontmatter field retained: {key}" for key in unknown]
    if mode == "strict":
        errors.extend(f"unknown frontmatter field: {key}" for key in unknown)
    name = fields.get("name")
    if not isinstance(name, str) or not name:
        message = "frontmatter name is required"
        (errors if mode == "strict" else diagnostics).append(message)
    else:
        if len(name) > 64 or NAME_RE.fullmatch(name) is None:
            message = "frontmatter name must be a 1-64 character lowercase identifier"
            (errors if mode == "strict" else diagnostics).append(message)
        if name != skill_dir.name:
            message = f"frontmatter name {name!r} must match directory {skill_dir.name!r}"
            (errors if mode == "strict" else diagnostics).append(message)
    description = fields.get("description")
    if not isinstance(description, str) or not description:
        message = "frontmatter description is required"
        (errors if mode == "strict" else diagnostics).append(message)
    elif len(description) > 1024:
        message = "frontmatter description exceeds 1024 characters"
        (errors if mode == "strict" else diagnostics).append(message)
    for key in ("license", "compatibility"):
        field_value = fields.get(key)
        if field_value is not None and (not isinstance(field_value, str) or not field_value):
            errors.append(f"frontmatter {key} must be a non-empty string")
    compatibility = fields.get("compatibility")
    if isinstance(compatibility, str) and len(compatibility) > 500:
        message = "frontmatter compatibility exceeds 500 characters"
        (errors if mode == "strict" else diagnostics).append(message)
    allowed = fields.get("allowed-tools")
    if isinstance(allowed, str):
        fields["allowed-tools"] = tuple(item for item in re.split(r"[\s,]+", allowed) if item)
    elif allowed is not None:
        errors.append("frontmatter allowed-tools must be a string declaration")
    fields.update(unknown)
    return Validation(fields, tuple(dict.fromkeys(errors)), tuple(diagnostics))
