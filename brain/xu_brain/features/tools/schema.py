"""Small dependency-free JSON Schema validator for model/tool contracts."""
from __future__ import annotations

from typing import Any


def validate(value: Any, schema: dict[str, Any], path: str = "$", errors: list[str] | None = None) -> list[str]:
    errors = errors if errors is not None else []
    typ = schema.get("type")
    ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if typ in ok and not ok[typ]:
        errors.append(f"{path}: expected {typ}, got {type(value).__name__}")
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required property missing")
        for key, child in schema.get("properties", {}).items():
            if key in value and isinstance(child, dict):
                validate(value[key], child, f"{path}.{key}", errors)
        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {}))
            for key in value:
                if key not in allowed:
                    errors.append(f"{path}.{key}: additional property not allowed")
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]", errors)
    return errors


def validation_error(value: Any, schema: dict[str, Any]) -> str | None:
    errors = validate(value, schema)
    return "; ".join(errors[:8]) if errors else None
