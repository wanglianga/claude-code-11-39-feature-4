from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def jsonable(value: Any) -> Any:
    """Convert Decimal/datetime values into JSON-serialisable primitives for JSONB."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def sha256_of(payload: Any) -> str:
    raw = json.dumps(jsonable(payload), sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def grade_rank(grade: str) -> int:
    return {"A": 3, "B": 2, "C": 1}.get(grade, 0)


def min_grade(*grades: str) -> str:
    valid = [g for g in grades if g]
    if not valid:
        return "C"
    return min(valid, key=grade_rank)
