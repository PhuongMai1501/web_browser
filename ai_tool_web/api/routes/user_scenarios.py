"""
api/routes/user_scenarios.py — CRUD endpoints cho user-configurable scenarios.

Prefix: /v1/scenarios
Auth: header X-User-Id (qua MockAuthProvider — Phase 1)

Endpoints:
  POST   /v1/scenarios/validate                       → dry-run validate YAML
  POST   /v1/scenarios                                → create scenario + rev 1
  POST   /v1/scenarios/clone                          → clone từ scenario khác
  GET    /v1/scenarios                                → list (filter)
  GET    /v1/scenarios/{id}                           → detail (metadata + latest + published)
  PUT    /v1/scenarios/{id}                           → update (create rev mới)
  DELETE /v1/scenarios/{id}                           → archive (soft delete)
  GET    /v1/scenarios/{id}/revisions                 → list revisions
  GET    /v1/scenarios/{id}/revisions/{rev_id}        → full revision content
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, get_scenario_service
from auth.providers import AuthenticatedUser
from services.user_scenario_service import UserScenarioService


router = APIRouter(prefix="/v1/scenarios", tags=["scenarios-v2"])


# ── Request/Response models ──────────────────────────────────────────────────

class ValidateRequest(BaseModel):
    raw_yaml: str = Field(..., description="YAML scenario text")


class ValidateResponse(BaseModel):
    parse_ok: bool
    validation_ok: bool
    yaml_hash: str
    errors: list[dict] = Field(default_factory=list)


class CreateScenarioRequest(BaseModel):
    raw_yaml: str
    display_name: Optional[str] = Field(
        None,
        description="Override display_name từ YAML. Nếu None, lấy từ spec.display_name."
    )


class UpdateScenarioRequest(BaseModel):
    raw_yaml: str


class CloneScenarioRequest(BaseModel):
    from_scenario_id: str
    new_display_name: Optional[str] = None


class ScenarioDefinitionResponse(BaseModel):
    id: str
    name: str
    owner_id: Optional[str]
    source_type: str
    visibility: str
    published_revision_id: Optional[int]
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class RevisionSummaryResponse(BaseModel):
    id: int
    version_no: int
    yaml_hash: str
    static_validation_status: str
    parent_revision_id: Optional[int]
    clone_source_revision_id: Optional[int]
    last_test_run_status: Optional[str]
    created_by: str
    created_at: datetime


class RevisionFullResponse(RevisionSummaryResponse):
    raw_yaml: str
    normalized_spec_json: dict
    static_validation_errors: Optional[list]


class ScenarioDetailResponse(BaseModel):
    definition: ScenarioDefinitionResponse
    latest_revision: Optional[RevisionSummaryResponse]
    published_revision: Optional[RevisionSummaryResponse]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _defn_to_resp(d) -> ScenarioDefinitionResponse:
    return ScenarioDefinitionResponse(
        id=d.id, name=d.name, owner_id=d.owner_id,
        source_type=d.source_type, visibility=d.visibility,
        published_revision_id=d.published_revision_id,
        is_archived=d.is_archived,
        created_at=d.created_at, updated_at=d.updated_at,
    )


def _rev_summary(r) -> Optional[RevisionSummaryResponse]:
    if r is None:
        return None
    return RevisionSummaryResponse(
        id=r.id, version_no=r.version_no, yaml_hash=r.yaml_hash,
        static_validation_status=r.static_validation_status,
        parent_revision_id=r.parent_revision_id,
        clone_source_revision_id=r.clone_source_revision_id,
        last_test_run_status=r.last_test_run_status,
        created_by=r.created_by, created_at=r.created_at,
    )


def _rev_full(r) -> RevisionFullResponse:
    return RevisionFullResponse(
        id=r.id, version_no=r.version_no, yaml_hash=r.yaml_hash,
        static_validation_status=r.static_validation_status,
        parent_revision_id=r.parent_revision_id,
        clone_source_revision_id=r.clone_source_revision_id,
        last_test_run_status=r.last_test_run_status,
        created_by=r.created_by, created_at=r.created_at,
        raw_yaml=r.raw_yaml,
        normalized_spec_json=r.normalized_spec_json,
        static_validation_errors=r.static_validation_errors,
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/validate", response_model=ValidateResponse)
async def validate_scenario(
    req: ValidateRequest,
    _user: AuthenticatedUser = Depends(get_current_user),   # auth required
    service: UserScenarioService = Depends(get_scenario_service),
):
    """Dry-run validate — parse + pydantic + security checks. Không lưu DB."""
    r = await service.validate(req.raw_yaml)
    return ValidateResponse(
        parse_ok=r.parse_ok,
        validation_ok=r.validation_ok,
        yaml_hash=r.yaml_hash,
        errors=r.all_issues,
    )


@router.post("", response_model=ScenarioDefinitionResponse, status_code=201)
async def create_scenario(
    req: CreateScenarioRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    service: UserScenarioService = Depends(get_scenario_service),
):
    """Tạo scenario + revision 1. id sinh tự động từ owner + slug(display_name)."""
    defn = await service.create(req.raw_yaml, user, display_name_override=req.display_name)
    return _defn_to_resp(defn)


@router.post("/clone", response_model=ScenarioDefinitionResponse, status_code=201)
async def clone_scenario(
    req: CloneScenarioRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    service: UserScenarioService = Depends(get_scenario_service),
):
    """Clone scenario từ source (builtin/public/own) thành scenario mới `source_type=cloned`."""
    defn = await service.clone(
        from_scenario_id=req.from_scenario_id,
        user=user,
        new_display_name=req.new_display_name,
    )
    return _defn_to_resp(defn)


@router.get("", response_model=list[ScenarioDefinitionResponse])
async def list_scenarios(
    source_type: Optional[str] = Query(None, pattern="^(builtin|user|cloned)$"),
    is_archived: Optional[bool] = Query(False),
    limit: int = Query(100, ge=1, le=500),
    user: AuthenticatedUser = Depends(get_current_user),
    service: UserScenarioService = Depends(get_scenario_service),
):
    """List scenarios visible cho user (builtin + own + public)."""
    items = await service.list_for_user(
        user, source_type=source_type, is_archived=is_archived, limit=limit,
    )
    return [_defn_to_resp(d) for d in items]


@router.get("/{scenario_id}", response_model=ScenarioDetailResponse)
async def get_scenario_detail(
    scenario_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    service: UserScenarioService = Depends(get_scenario_service),
):
    """Full metadata + latest revision summary + published revision summary."""
    detail = await service.get_detail(scenario_id, user)
    return ScenarioDetailResponse(
        definition=_defn_to_resp(detail.definition),
        latest_revision=_rev_summary(detail.latest_revision),
        published_revision=_rev_summary(detail.published_revision),
    )


@router.put("/{scenario_id}", response_model=RevisionSummaryResponse)
async def update_scenario(
    scenario_id: str,
    req: UpdateScenarioRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    service: UserScenarioService = Depends(get_scenario_service),
):
    """Update = tạo revision mới. Hash trùng → 409 NO_CHANGE."""
    rev = await service.update(scenario_id, req.raw_yaml, user)
    return _rev_summary(rev)


@router.delete("/{scenario_id}", status_code=204)
async def archive_scenario(
    scenario_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    service: UserScenarioService = Depends(get_scenario_service),
):
    """Soft delete. Builtin không archive được qua API."""
    await service.archive(scenario_id, user)


@router.get("/{scenario_id}/revisions", response_model=list[RevisionSummaryResponse])
async def list_revisions(
    scenario_id: str,
    limit: int = Query(20, ge=1, le=100),
    before_id: Optional[int] = Query(None, description="Cursor — id của rev cuối trang trước"),
    user: AuthenticatedUser = Depends(get_current_user),
    service: UserScenarioService = Depends(get_scenario_service),
):
    """List revisions newest-first. Pagination qua before_id."""
    revs = await service.list_revisions(scenario_id, user, limit=limit, before_id=before_id)
    return [_rev_summary(r) for r in revs]


@router.get(
    "/{scenario_id}/revisions/{rev_id}",
    response_model=RevisionFullResponse,
)
async def get_revision_full(
    scenario_id: str,
    rev_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
    service: UserScenarioService = Depends(get_scenario_service),
):
    """Full revision content (raw_yaml + normalized_spec_json)."""
    rev = await service.get_revision(scenario_id, rev_id, user)
    return _rev_full(rev)
