"""
services/inputs_validator.py — Validate user-provided context inputs tại runtime (G5).

Khi caller gửi POST /v1/sessions với `inputs: {...}`, so với spec.inputs[]
(chỉ field có source='context'), check required + coerce type, trả dict đã normalize.
Raise InputValidationError nếu fail.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_LLM_BASE = Path(__file__).resolve().parent.parent.parent / "LLM_base"
if str(_LLM_BASE) not in sys.path:
    sys.path.append(str(_LLM_BASE))

from scenarios.spec import ScenarioSpec  # noqa: E402


class InputValidationError(Exception):
    """Thrown khi context không match spec.inputs[] requirements."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"Input validation failed: {errors}")


@dataclass
class ValidatedInputs:
    """Kết quả sau validate + coerce."""
    context: dict                    # field cần truyền vào runtime
    ask_user_fields: list[str]       # field source='ask_user' — runtime sẽ hỏi


def validate_inputs(spec: ScenarioSpec, user_inputs: dict | None) -> ValidatedInputs:
    """Validate user_inputs dict against spec.inputs[].

    Rules:
    - Fields với source='context' → lấy từ user_inputs, check required, coerce type.
    - Fields với source='ask_user' → bỏ qua (runtime sẽ hỏi).
    - Field ngoài spec trong user_inputs → silently drop (không raise).
    - Required field thiếu + không có default → error.

    Args:
        spec: ScenarioSpec đã load từ revision.
        user_inputs: dict từ request body {name: value, ...} hoặc None.

    Returns:
        ValidatedInputs với context đã coerce type.

    Raises:
        InputValidationError: nếu có required thiếu hoặc type không coerce được.
    """
    user_inputs = user_inputs or {}
    errors: list[dict] = []
    validated: dict[str, Any] = {}
    ask_user_fields: list[str] = []

    for inp in spec.inputs:
        if inp.source == "ask_user":
            ask_user_fields.append(inp.name)
            continue

        raw = user_inputs.get(inp.name)
        missing = raw is None or (isinstance(raw, str) and raw.strip() == "")

        if missing:
            if inp.default is not None:
                raw = inp.default
            elif inp.required:
                errors.append({
                    "field": inp.name,
                    "message": f"Required field '{inp.name}' missing",
                })
                continue
            else:
                continue  # optional, no default, no value → skip

        # Coerce type
        try:
            coerced = _coerce(inp.type, raw)
            validated[inp.name] = coerced
        except (ValueError, TypeError) as e:
            errors.append({
                "field": inp.name,
                "message": f"Type '{inp.type}' expected, got {type(raw).__name__}: {e}",
            })

    if errors:
        raise InputValidationError(errors)

    return ValidatedInputs(context=validated, ask_user_fields=ask_user_fields)


def _coerce(type_name: str, raw: Any) -> Any:
    """Coerce raw value sang target type.

    Types (match InputField.type):
    - string → str(raw)
    - secret → str(raw)  (giữ nguyên, masking là UI/log concern)
    - number → float(raw); raise ValueError nếu không parse được
    - bool   → truthy theo Python rule + strings "true"/"false"/"1"/"0"
    """
    if type_name == "number":
        if isinstance(raw, bool):
            # bool is subclass of int in Python — chặn để tránh ambiguity
            raise ValueError("bool không hợp lệ cho type=number")
        return float(raw)

    if type_name == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            s = raw.strip().lower()
            if s in ("true", "1", "yes", "y"):
                return True
            if s in ("false", "0", "no", "n", ""):
                return False
            raise ValueError(f"Không parse được '{raw}' thành bool")
        raise ValueError(f"Không support {type(raw).__name__} cho type=bool")

    # string / secret / default
    return str(raw)
