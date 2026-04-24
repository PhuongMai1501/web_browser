"""
services/yaml_normalizer.py — Parse YAML → ScenarioSpec → normalized JSON + errors.

Steps:
  1. sha256 raw_yaml → yaml_hash
  2. yaml.safe_load() → dict (parse errors → hard fail)
  3. ScenarioSpec.model_validate(data) → pydantic errors → soft fail
  4. Security checks (hook whitelist, password field default) → soft fail
  5. Normalized JSON = spec.model_dump(mode='json')

"Hard fail" = không tạo revision (400).
"Soft fail" = tạo revision với static_validation_status='failed' + errors.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import ValidationError


# LLM_base không nằm trong PYTHONPATH khi API chạy
_LLM_BASE = Path(__file__).resolve().parent.parent.parent / "LLM_base"
if str(_LLM_BASE) not in sys.path:
    sys.path.append(str(_LLM_BASE))

from scenarios.spec import ScenarioSpec  # noqa: E402
from scenarios.hooks_registry import HOOK_REGISTRY  # noqa: E402


_log = logging.getLogger(__name__)


# Secret-detection regex cho input default — warn (không block) nếu match
_SECRET_PATTERNS = [
    re.compile(r"^AKIA[0-9A-Z]{16}$"),                   # AWS key
    re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),   # JWT prefix
    re.compile(r"^Bearer\s+"),                           # Bearer token
]


# ── Result ───────────────────────────────────────────────────────────────────

@dataclass
class ValidationError_:
    """1 lỗi validation — location + message."""
    field: str      # dotted path vd "inputs.0.name"
    message: str
    severity: str = "error"    # "error" | "warning"


@dataclass
class NormalizeResult:
    """Kết quả normalize.

    - parse_ok=False → YAML syntax error, không có spec, không lưu revision
    - validation_ok=False → spec tạo được nhưng có lỗi semantic → vẫn lưu được (badge FAILED)
    - validation_ok=True + warnings → spec OK, có cảnh báo non-blocking
    """
    parse_ok: bool
    validation_ok: bool
    yaml_hash: str
    spec: Optional[ScenarioSpec] = None
    normalized_json: Optional[dict] = None
    errors: list[ValidationError_] = field(default_factory=list)
    warnings: list[ValidationError_] = field(default_factory=list)

    @property
    def all_issues(self) -> list[dict]:
        """Dạng JSON để lưu vào static_validation_errors."""
        return [
            {"field": e.field, "message": e.message, "severity": e.severity}
            for e in self.errors + self.warnings
        ]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _pydantic_errors_to_list(ve: ValidationError) -> list[ValidationError_]:
    out = []
    for err in ve.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        out.append(ValidationError_(field=loc or "<root>", message=err.get("msg", "invalid")))
    return out


def _check_hooks(spec: ScenarioSpec) -> list[ValidationError_]:
    """Hooks phải có trong HOOK_REGISTRY (whitelist security §3.2)."""
    out = []
    for field_name in ("pre_check", "post_step", "final_capture"):
        hook_name = getattr(spec.hooks, field_name)
        if hook_name and hook_name not in HOOK_REGISTRY:
            out.append(ValidationError_(
                field=f"hooks.{field_name}",
                message=f"Hook '{hook_name}' chưa register. "
                        f"Các hook hợp lệ: {sorted(HOOK_REGISTRY) or '(none)'}",
            ))
    return out


def _check_credentials(spec: ScenarioSpec) -> tuple[list, list]:
    """§3.5 credential protection.
    - password/secret field: default PHẢI rỗng → hard error
    - field khác: default match secret pattern → warning
    """
    errors, warnings = [], []
    for i, inp in enumerate(spec.inputs):
        is_secret = inp.type == "secret" or re.search(
            r"(password|pwd|secret|token|api[_-]?key)", inp.name, re.I
        )
        default = inp.default

        if is_secret and default not in (None, ""):
            errors.append(ValidationError_(
                field=f"inputs.{i}.default",
                message=f"Field '{inp.name}' là secret/password — "
                        f"default phải rỗng, không được chứa giá trị.",
            ))
        elif isinstance(default, str):
            for pat in _SECRET_PATTERNS:
                if pat.search(default):
                    warnings.append(ValidationError_(
                        field=f"inputs.{i}.default",
                        message=f"Field '{inp.name}' có default giống secret/token. "
                                f"Kiểm tra không commit credential thật.",
                        severity="warning",
                    ))
                    break
    return errors, warnings


# ── Main entry ───────────────────────────────────────────────────────────────

def normalize_yaml(
    raw_yaml: str,
    force_id: Optional[str] = None,
    force_builtin: bool = False,
) -> NormalizeResult:
    """Parse + validate YAML, trả NormalizeResult.

    Args:
        raw_yaml: raw YAML text user submit.
        force_id: override id trong YAML (user scenario auto-prefixed bởi service).
        force_builtin: True nếu đang seed builtin. Bỏ qua 1 số check non-critical.

    Returns:
        NormalizeResult. Check .parse_ok trước khi dùng .spec/.normalized_json.
    """
    yaml_hash = _sha256(raw_yaml)

    # 1. Parse YAML
    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as e:
        return NormalizeResult(
            parse_ok=False,
            validation_ok=False,
            yaml_hash=yaml_hash,
            errors=[ValidationError_(
                field="<yaml>",
                message=f"YAML parse error: {e}",
            )],
        )

    if not isinstance(data, dict):
        return NormalizeResult(
            parse_ok=False,
            validation_ok=False,
            yaml_hash=yaml_hash,
            errors=[ValidationError_(
                field="<yaml>",
                message=f"YAML phải là mapping (dict), got {type(data).__name__}",
            )],
        )

    # 2. Force id override (user scenario service compute id từ user+slug)
    if force_id is not None:
        data["id"] = force_id
    if force_builtin:
        data["builtin"] = True

    # 3. Pydantic validate
    try:
        spec = ScenarioSpec.model_validate(data)
    except ValidationError as ve:
        return NormalizeResult(
            parse_ok=True,
            validation_ok=False,
            yaml_hash=yaml_hash,
            errors=_pydantic_errors_to_list(ve),
        )

    # 4. Security checks (không run cho builtin force, builtin trusted)
    errors: list[ValidationError_] = []
    warnings: list[ValidationError_] = []
    if not force_builtin:
        errors.extend(_check_hooks(spec))
        cred_errors, cred_warnings = _check_credentials(spec)
        errors.extend(cred_errors)
        warnings.extend(cred_warnings)

    validation_ok = len(errors) == 0

    return NormalizeResult(
        parse_ok=True,
        validation_ok=validation_ok,
        yaml_hash=yaml_hash,
        spec=spec,
        normalized_json=spec.model_dump(mode="json") if validation_ok else None,
        errors=errors,
        warnings=warnings,
    )
