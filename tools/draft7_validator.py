"""Minimal zero-dependency JSON Schema Draft 7 validator for checked-in schemas."""

from __future__ import annotations

import re
from typing import Any


class ValidationError(Exception):
    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def validate(instance: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    """Return human-readable validation errors for *instance* against *schema*."""
    errors: list[str] = []
    try:
        _validate(instance, schema, path)
    except ValidationError as error:
        errors.append(f"{error.path}: {error.message}")
    return errors


def _validate(instance: Any, schema: dict[str, Any], path: str) -> None:
    if "const" in schema and instance != schema["const"]:
        raise ValidationError(path, f"expected const {schema['const']!r}")

    if "enum" in schema and instance not in schema["enum"]:
        raise ValidationError(path, f"value not in enum {schema['enum']!r}")

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if not any(_matches_type(instance, item) for item in schema_type):
            raise ValidationError(path, f"expected one of types {schema_type!r}")
    elif schema_type is not None and not _matches_type(instance, schema_type):
        raise ValidationError(path, f"expected type {schema_type!r}")

    if schema_type == "string" or (isinstance(schema_type, list) and "string" in schema_type):
        if isinstance(instance, str) and "pattern" in schema:
            if re.fullmatch(schema["pattern"], instance) is None:
                raise ValidationError(path, f"string does not match pattern {schema['pattern']!r}")

    if schema_type == "array" or (isinstance(schema_type, list) and "array" in schema_type):
        if isinstance(instance, list):
            if "minItems" in schema and len(instance) < schema["minItems"]:
                raise ValidationError(path, f"expected at least {schema['minItems']} items")
            if "maxItems" in schema and len(instance) > schema["maxItems"]:
                raise ValidationError(path, f"expected at most {schema['maxItems']} items")
            if schema.get("uniqueItems") and len(instance) != len(set(_freeze(item) for item in instance)):
                raise ValidationError(path, "array items must be unique")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(instance):
                    _validate(item, item_schema, f"{path}[{index}]")

    if schema_type == "object" or (isinstance(schema_type, list) and "object" in schema_type):
        if isinstance(instance, dict):
            required = schema.get("required", [])
            for key in required:
                if key not in instance:
                    raise ValidationError(path, f"missing required property {key!r}")

            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            for key, value in instance.items():
                child_path = f"{path}.{key}"
                if key in properties:
                    _validate(value, properties[key], child_path)
                elif additional is False:
                    raise ValidationError(child_path, "additional property is not allowed")
                elif isinstance(additional, dict):
                    _validate(value, additional, child_path)

    for subschema in schema.get("allOf", []):
        _validate(instance, subschema, path)

    for index, subschema in enumerate(schema.get("anyOf", [])):
        try:
            _validate(instance, subschema, f"{path}|anyOf[{index}]")
            break
        except ValidationError:
            continue
    else:
        if schema.get("anyOf"):
            raise ValidationError(path, "value did not match any anyOf schema")

    if "if" in schema:
        try:
            _validate(instance, schema["if"], path)
            matched = True
        except ValidationError:
            matched = False
        branch = schema.get("then") if matched else schema.get("else")
        if isinstance(branch, dict):
            _validate(instance, branch, path)

    if "not" in schema:
        try:
            _validate(instance, schema["not"], path)
        except ValidationError:
            pass
        else:
            raise ValidationError(path, "value matched forbidden not schema")


def _matches_type(instance: Any, schema_type: str) -> bool:
    if schema_type == "null":
        return instance is None
    if schema_type == "boolean":
        return isinstance(instance, bool)
    if schema_type == "object":
        return isinstance(instance, dict)
    if schema_type == "array":
        return isinstance(instance, list)
    if schema_type == "string":
        return isinstance(instance, str)
    if schema_type == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if schema_type == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    return False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value
