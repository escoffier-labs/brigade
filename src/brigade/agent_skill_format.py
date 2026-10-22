"""Dependency-free validation for the observable Agent Skills frontmatter contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

KNOWN_FIELDS = frozenset({"name", "description", "license", "compatibility", "metadata", "allowed-tools"})
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BLOCK_HEADER_RE = re.compile(r"([>|])([+-]?[1-9]?|[1-9][+-]?)(?:[ \t]+#.*)?$")


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


def _block_scalar(lines: list[str], start: int, style: str, modifiers: str) -> tuple[str, int, list[str]]:
    """Consume one top-level block, retaining content indentation and line breaks."""
    explicit_indent = next((int(char) for char in modifiers if char.isdigit()), None)
    indent = explicit_indent or 1
    if explicit_indent is None:
        for index in range(start, len(lines)):
            line = lines[index]
            spaces = len(line) - len(line.lstrip(" "))
            indent = max(indent, spaces)
            if line.strip(" "):
                indent = spaces or indent
                break
    content: list[str] = []
    errors: list[str] = []
    end = start
    leading = True
    while end < len(lines):
        line = lines[end]
        spaces = len(line) - len(line.lstrip(" "))
        if line.strip(" "):
            if spaces < indent:
                break
            leading = False
        elif leading and explicit_indent is None and spaces > indent:
            errors.append(f"frontmatter line {end + 2} has invalid block scalar indentation")
        content.append(line[indent:])
        end += 1

    value = "".join(line + "\n" for line in content)
    if style == ">":
        parts: list[str] = []
        previous: int | None = None
        for index, line in enumerate(content):
            if not line:
                continue
            if previous is None:
                parts.append("\n" * index)
            else:
                breaks = index - previous
                if content[previous][0] not in " \t" and line[0] not in " \t":
                    # Fold ordinary lines, but retain one LF per empty paragraph line.
                    parts.append(" " if breaks == 1 else "\n" * (breaks - 1))
                else:
                    parts.append("\n" * breaks)
            parts.append(line)
            previous = index
        parts.append("\n" * (len(content) if previous is None else len(content) - previous))
        value = "".join(parts)
    if "-" in modifiers:
        value = value.rstrip("\n")
    elif "+" not in modifiers:
        value = value.rstrip("\n") + ("\n" if any(content) else "")
    return value, end, errors


def _split_allowed_tools(allowed: str) -> tuple[str, ...]:
    """Split comma/whitespace tool lists while preserving parenthesized scopes.

    Claude-style entries such as ``Bash(brigade handoff list:*)`` must stay one
    token. A naive ``re.split(r"[\\s,]+")`` shreds them into nonsense.
    """
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in allowed:
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch in ", \t\r\n" and depth == 0:
            if buf:
                item = "".join(buf).strip()
                if item:
                    items.append(item)
                buf = []
            continue
        buf.append(ch)
    if buf:
        item = "".join(buf).strip()
        if item:
            items.append(item)
    return tuple(items)


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
    lines, framing_error = _frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    if lines is None:
        if mode == "lenient":
            return Validation({}, (), (framing_error or "invalid frontmatter",))
        return Validation({}, (framing_error or "invalid frontmatter",), ())
    fields: dict[str, object] = {}
    unknown: dict[str, str] = {}
    metadata: dict[str, str] = {}
    current_map: str | None = None
    errors: list[str] = []
    index = 0
    while index < len(lines):
        number, line = index + 2, lines[index]
        index += 1
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
        if key == "metadata":
            current_map = key
            if raw.strip() not in {"", "{}"}:
                errors.append("metadata must be a string-to-string mapping")
            continue
        if raw.strip().startswith((">", "|")):
            header = BLOCK_HEADER_RE.fullmatch(raw.strip())
            if header is None:
                errors.append(f"frontmatter line {number} has invalid block scalar header")
                continue
            value, index, block_errors = _block_scalar(lines, index, *header.groups())
            errors.extend(block_errors)
        else:
            value = _scalar(raw)
        if key in KNOWN_FIELDS:
            fields[key] = value
        else:
            unknown[key] = value
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
        fields["allowed-tools"] = _split_allowed_tools(allowed)
    elif allowed is not None:
        errors.append("frontmatter allowed-tools must be a string declaration")
    fields.update(unknown)
    return Validation(fields, tuple(dict.fromkeys(errors)), tuple(diagnostics))
