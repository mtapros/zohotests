from __future__ import annotations

import json
from typing import Any, Dict


def flatten_json(data: Any, prefix: str = "") -> Dict[str, Any]:
    flat: Dict[str, Any] = {}

    if isinstance(data, dict):
        if not data and prefix:
            flat[prefix] = data
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            flat.update(flatten_json(value, path))
    elif isinstance(data, list):
        if not data and prefix:
            flat[prefix] = data
        for idx, value in enumerate(data):
            path = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            flat.update(flatten_json(value, path))
    else:
        flat[prefix] = data

    return flat


def value_to_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"
