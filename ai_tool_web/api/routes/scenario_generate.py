"""
api/routes/scenario_generate.py — Sinh YAML scenario từ NL description (LLM).

Endpoint public (không yêu cầu admin token) — Sup Agent gọi backend-to-backend
như POST /v1/sessions:
  - End-user mô tả task tiếng Việt qua chatbot
  - Sup Agent POST /v1/scenarios/generate {description}
  - Tool-web gọi OpenAI gpt-4o-mini sinh YAML
  - Sup Agent paste YAML vào POST /v1/sessions {scenario_yaml}

Mỗi call tốn OpenAI tokens → Sup Agent nên cache YAML theo fingerprint
description nếu task lặp.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from services.builtin_matcher import match_builtin, load_builtin_yaml
from services.scenario_generator import generate_yaml
from services.yaml_normalizer import normalize_yaml


router = APIRouter(prefix="/v1/scenarios", tags=["scenarios-generate"])


class ScenarioGenerateRequest(BaseModel):
    """Body cho POST /v1/scenarios/generate.

    description: NL tiếng Việt mô tả việc user muốn tool-web tự động hoá.
    site_hint:   optional — domain hoặc URL gợi ý (vd "thuvienphapluat.vn").
    model:       optional override model (default từ config LLM_MODEL).
    """

    description: str
    site_hint: Optional[str] = None
    model: Optional[str] = None


class ScenarioGenerateResponse(BaseModel):
    """Response cho POST /v1/scenarios/generate.

    - ok=True  + yaml: YAML hợp lệ, Sup Agent paste vào POST /v1/sessions.
    - ok=False + errors: LLM call fail HOẶC YAML sinh ra không pass yaml_normalizer.
                          `yaml_raw` chứa raw output để Sup Agent debug.
    """

    ok: bool
    yaml: Optional[str] = None
    yaml_raw: Optional[str] = None
    scenario_id_suggestion: Optional[str] = None
    model_used: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    errors: list[str] = []


@router.post("/generate", response_model=ScenarioGenerateResponse)
async def generate_scenario(req: ScenarioGenerateRequest):
    """Sinh YAML scenario từ NL description.

    Pipeline:
      0. (FAST PATH) Match keyword → builtin scenario → trả YAML, 0 LLM token
      1. (FALLBACK) Gọi OpenAI gpt-4o-mini với system prompt + few-shot QCVN
      2. Strip markdown wrap nếu có
      3. Validate qua yaml_normalizer (parse + Pydantic ScenarioSpec)
      4. Trả YAML hợp lệ HOẶC errors + yaml_raw để Sup Agent debug
    """
    # Fast path: match builtin scenario qua keyword detection
    matched_id = match_builtin(req.description)
    if matched_id:
        builtin_yaml = load_builtin_yaml(matched_id)
        if builtin_yaml:
            norm_bi = normalize_yaml(builtin_yaml)
            if norm_bi.parse_ok and norm_bi.validation_ok:
                return ScenarioGenerateResponse(
                    ok=True,
                    yaml=builtin_yaml,
                    scenario_id_suggestion=matched_id,
                    model_used="builtin-match",
                    tokens_in=0,
                    tokens_out=0,
                )

    gen = generate_yaml(
        description=req.description,
        site_hint=req.site_hint,
        model=req.model,
    )
    if not gen.ok:
        return ScenarioGenerateResponse(
            ok=False,
            model_used=gen.model,
            errors=[gen.error],
        )

    norm = normalize_yaml(gen.yaml)
    if not norm.parse_ok or not norm.validation_ok:
        err_msgs = [f"{e.field}: {e.message}" for e in norm.errors]
        return ScenarioGenerateResponse(
            ok=False,
            yaml_raw=gen.yaml,
            model_used=gen.model,
            tokens_in=gen.tokens_in,
            tokens_out=gen.tokens_out,
            errors=err_msgs or ["YAML không pass validation"],
        )

    spec_id = norm.spec.id if norm.spec else None
    return ScenarioGenerateResponse(
        ok=True,
        yaml=gen.yaml,
        scenario_id_suggestion=spec_id,
        model_used=gen.model,
        tokens_in=gen.tokens_in,
        tokens_out=gen.tokens_out,
    )
