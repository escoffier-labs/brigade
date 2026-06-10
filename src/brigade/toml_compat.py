"""Small TOML compatibility layer for Python 3.10."""

from __future__ import annotations

import ast
from typing import Any

try:  # Python 3.11+
    import tomllib as _stdlib_tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10.
    _stdlib_tomllib = None  # type: ignore[assignment]


class TOMLDecodeError(ValueError):
    """Raised when the fallback TOML reader cannot parse local config."""


def loads(text: str) -> dict[str, Any]:
    if _stdlib_tomllib is not None:
        try:
            return _stdlib_tomllib.loads(text)
        except _stdlib_tomllib.TOMLDecodeError as exc:
            raise TOMLDecodeError(str(exc)) from exc
    return _fallback_loads(text)


def _fallback_loads(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current: dict[str, Any] = data

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            current = _array_table(data, line[2:-2], line_number)
            continue
        if line.startswith("[") and line.endswith("]"):
            current = _table(data, line[1:-1], line_number)
            continue
        if "=" not in line:
            raise TOMLDecodeError(f"invalid TOML assignment on line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise TOMLDecodeError(f"invalid TOML key on line {line_number}")
        current[key] = _parse_value(raw_value.strip(), line_number)

    return data


def _strip_comment(line: str) -> str:
    in_string = False
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {"'", '"'}:
            in_string = True
            quote = char
            continue
        if char == "#":
            return line[:index]
    return line


def _path(value: str, line_number: int) -> list[str]:
    parts = [part.strip() for part in value.split(".") if part.strip()]
    if not parts:
        raise TOMLDecodeError(f"invalid TOML table on line {line_number}")
    return parts


def _table(data: dict[str, Any], raw_path: str, line_number: int) -> dict[str, Any]:
    current = data
    for part in _path(raw_path, line_number):
        next_value = current.setdefault(part, {})
        if isinstance(next_value, list):
            if not next_value or not isinstance(next_value[-1], dict):
                raise TOMLDecodeError(f"invalid TOML table on line {line_number}")
            current = next_value[-1]
            continue
        if not isinstance(next_value, dict):
            raise TOMLDecodeError(f"invalid TOML table on line {line_number}")
        current = next_value
    return current


def _array_table(data: dict[str, Any], raw_path: str, line_number: int) -> dict[str, Any]:
    parts = _path(raw_path, line_number)
    current = data
    for part in parts[:-1]:
        next_value = current.setdefault(part, {})
        if isinstance(next_value, list):
            if not next_value or not isinstance(next_value[-1], dict):
                raise TOMLDecodeError(f"invalid TOML table on line {line_number}")
            current = next_value[-1]
            continue
        if not isinstance(next_value, dict):
            raise TOMLDecodeError(f"invalid TOML table on line {line_number}")
        current = next_value
    table_name = parts[-1]
    raw_table = current.setdefault(table_name, [])
    if not isinstance(raw_table, list):
        raise TOMLDecodeError(f"invalid TOML array table on line {line_number}")
    new_table: dict[str, Any] = {}
    raw_table.append(new_table)
    return new_table


def _parse_value(value: str, line_number: int) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        return [_parse_value(part.strip(), line_number) for part in _split_top_level(value[1:-1]) if part.strip()]
    if value.startswith("{") and value.endswith("}"):
        return _parse_inline_table(value[1:-1], line_number)
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        pass
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError as exc:
        raise TOMLDecodeError(f"unsupported TOML value on line {line_number}") from exc


def _parse_inline_table(value: str, line_number: int) -> dict[str, Any]:
    table: dict[str, Any] = {}
    for item in _split_top_level(value):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise TOMLDecodeError(f"invalid TOML inline table on line {line_number}")
        raw_key, raw_value = item.split("=", 1)
        key = _parse_key(raw_key.strip(), line_number)
        table[key] = _parse_value(raw_value.strip(), line_number)
    return table


def _parse_key(value: str, line_number: int) -> str:
    if not value:
        raise TOMLDecodeError(f"invalid TOML key on line {line_number}")
    if value[0] in {"'", '"'}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise TOMLDecodeError(f"invalid TOML key on line {line_number}") from exc
        if not isinstance(parsed, str) or not parsed:
            raise TOMLDecodeError(f"invalid TOML key on line {line_number}")
        return parsed
    return value


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    square_depth = 0
    brace_depth = 0
    in_string = False
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {"'", '"'}:
            in_string = True
            quote = char
            continue
        if char == "[":
            square_depth += 1
        elif char == "]":
            square_depth -= 1
        elif char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
        elif char == "," and square_depth == 0 and brace_depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts
