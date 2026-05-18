"""
api/routes/input_fields.py — REST API cho input fields (Phase 1 Sprint 2).

Prefix: /v1/scenarios/{scenario_code}/revisions/{version_no}/input-fields
Auth: header X-User-Id (qua MockAuthProvider — Phase 1)

Endpoints:
  GET    /v1/scenarios/{code}/revisions/{n}/input-fields            → list
  POST   /v1/scenarios/{code}/revisions/{n}/input-fields            → create (1 field)
  PUT    /v1/scenarios/{code}/revisions/{n}/input-fields/{id}       → update
  DELETE /v1/scenarios/{code}/revisions/{n}/input-fields/{id}       → delete
  POST   /v1/scenarios/{code}/revisions/{n}/input-fields/reorder    → reorder
  POST   /v1/scenarios/{code}/revisions/{n}/input-fields/bulk       → replace all

Hybrid revision model:
- DRAFT revision (rev.id != defn.published_revision_id): mutate được
- PUBLISHED revision: trả 409 với message hướng dẫn user tạo rev mới

Side effects mỗi mutate endpoint:
  1. CRUD scenario_input_fields row
  2. yaml_sync.regenerate_revision_yaml(rev_id) → update raw_yaml + normalized
  3. (TODO Phase 2.5) Sync Redis cache scenario qua scenario_service

Pydantic models inline trong file (theo pattern scenario_images.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, get_repo
from auth.providers import AuthenticatedUser
from services.user_scenario_service import (
    ScenarioForbidden,
    ScenarioNotFound,
)
from services.yaml_sync import (
    YamlSyncForbidden,
    YamlSyncNotFound,
    YamlSyncService,
)
from store.mysql_scenario_input_field_repo import (
    MysqlScenarioInputFieldRepo,
    ScenarioInputField,
)
from store.scenario_repo import ScenarioRepository


router = APIRouter(
    prefix="/v1/scenarios/{scenario_code}/revisions/{version_no}/input-fields",
    tags=["scenario-input-fields"],
)


# ── Request/Response models ──────────────────────────────────────────────────

class ValidationRules(BaseModel):
    """JSON Schema 7 subset cho validation."""
    minLength: Optional[int] = None
    maxLength: Optional[int] = None
    pattern: Optional[str] = None
    enum: Optional[list[Any]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None


class InputFieldBase(BaseModel):
    """Shared fields giữa Create/Update/Response."""
    name: str = Field(..., max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    display_label: str = Field(..., max_length=255)
    field_type: str = Field(..., pattern=r"^(string|secret|number|bool)$")
    is_required: bool = False
    source: str = Field(default="context", pattern=r"^(context|ask_user)$")
    default_value: Optional[str] = None
    description: Optional[str] = None
    validation_rules: Optional[ValidationRules] = None
    placeholder: Optional[str] = Field(default=None, max_length=255)
    help_text: Optional[str] = None
    display_order: int = 0
    # Phase 2 reserved (UI Phase 1 chưa expose)
    category: str = Field(
        default="user_input",
        pattern=r"^(user_input|credential|config|system)$",
    )
    secret_ref: Optional[str] = Field(default=None, max_length=255)
    extraction_hint: Optional[str] = None


class InputFieldCreateRequest(InputFieldBase):
    pass


class InputFieldUpdateRequest(InputFieldBase):
    pass


class InputFieldResponse(InputFieldBase):
    id: int
    revision_id: int
    scenario_id: int
    template_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class InputFieldListResponse(BaseModel):
    fields: list[InputFieldResponse]
    revision_is_draft: bool


class ReorderRequest(BaseModel):
    ordered_ids: list[int] = Field(..., min_length=1)


class BulkReplaceRequest(BaseModel):
    fields: list[InputFieldBase]


class DeleteResponse(BaseModel):
    deleted: bool


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_response(f: ScenarioInputField) -> InputFieldResponse:
    return InputFieldResponse(
        id=f.id or 0,
        revision_id=f.revision_id,
        scenario_id=f.scenario_id,
        name=f.name,
        display_label=f.display_label,
        field_type=f.field_type,
        is_required=f.is_required,
        source=f.source,
        default_value=f.default_value,
        description=f.description,
        validation_rules=(
            ValidationRules(**f.validation_rules)
            if f.validation_rules else None
        ),
        placeholder=f.placeholder,
        help_text=f.help_text,
        display_order=f.display_order,
        category=f.category,
        secret_ref=f.secret_ref,
        extraction_hint=f.extraction_hint,
        template_id=f.template_id,
        created_at=f.created_at,
        updated_at=f.updated_at,
    )


def _base_to_field(
    base: InputFieldBase,
    revision_id: int,
    scenario_id: int,
    field_id: Optional[int] = None,
) -> ScenarioInputField:
    return ScenarioInputField(
        id=field_id,
        revision_id=revision_id,
        scenario_id=scenario_id,
        name=base.name,
        display_label=base.display_label,
        field_type=base.field_type,
        is_required=base.is_required,
        source=base.source,
        default_value=base.default_value,
        description=base.description,
        validation_rules=base.validation_rules.model_dump(exclude_none=True)
            if base.validation_rules else None,
        placeholder=base.placeholder,
        help_text=base.help_text,
        display_order=base.display_order,
        category=base.category,
        secret_ref=base.secret_ref,
        extraction_hint=base.extraction_hint,
    )


# ── DI provider — wire vào dependencies.py sau ───────────────────────────────

async def get_input_field_repo(request: Request) -> MysqlScenarioInputFieldRepo:
    """Lấy MysqlScenarioInputFieldRepo singleton (init 1 lần ở startup app)."""
    repo = getattr(request.app.state, "input_field_repo", None)
    if repo is None:
        raise HTTPException(503, "Input field repository chưa init")
    return repo


async def get_yaml_sync_service(
    scenario_repo: ScenarioRepository = Depends(get_repo),
    input_field_repo: MysqlScenarioInputFieldRepo = Depends(get_input_field_repo),
) -> YamlSyncService:
    return YamlSyncService(scenario_repo, input_field_repo)


# ── Permission helper ────────────────────────────────────────────────────────

async def _require_writable_revision(
    scenario_code: str,
    version_no: int,
    user: AuthenticatedUser,
    scenario_repo: ScenarioRepository,
) -> tuple[int, int, bool]:
    """Resolve (rev_id, scenario_pk, is_draft) + check user có quyền edit.

    Permissions:
    - Builtin scenarios: chỉ admin
    - User scenarios: owner hoặc admin

    Raise HTTPException 404/403/409 phù hợp.

    Note: scenario_pk là BIGINT id của scenario_definitions, không phải code.
    """
    defn = await scenario_repo.get_definition(scenario_code)
    if defn is None or defn.is_archived:
        raise HTTPException(404, f"Scenario '{scenario_code}' không tồn tại")

    if not user.is_admin:
        if defn.source_type == "builtin":
            raise HTTPException(
                403, "Builtin scenario chỉ admin được sửa"
            )
        if defn.owner_id != user.user_id:
            raise HTTPException(
                403,
                f"User '{user.user_id}' không phải owner của '{scenario_code}'",
            )

    rev = await scenario_repo.get_revision_by_version(
        scenario_code, version_no
    )
    if rev is None:
        raise HTTPException(
            404,
            f"Revision {version_no} của '{scenario_code}' không tồn tại",
        )

    is_draft = defn.published_revision_id != rev.id

    # Resolve scenario_pk (BIGINT) qua MysqlScenarioRepo internal helper.
    # Helper signature: _resolve_scenario_pk(cur, code) — cần acquire cursor.
    from store.mysql_scenario_repo import MysqlScenarioRepo
    import aiomysql
    if not isinstance(scenario_repo, MysqlScenarioRepo):
        raise HTTPException(
            500, "input_fields API yêu cầu MySQL scenario_repo backend"
        )
    pool = scenario_repo._get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            scenario_pk = await scenario_repo._resolve_scenario_pk(
                cur, scenario_code
            )

    return rev.id, scenario_pk, is_draft


def _ensure_draft(is_draft: bool, rev_id: int) -> None:
    """Raise 409 nếu rev đã published — caller phải tạo rev mới."""
    if not is_draft:
        raise HTTPException(
            409,
            f"Revision {rev_id} đã PUBLISHED và immutable. "
            f"Hãy tạo revision mới (clone) để chỉnh sửa inputs.",
        )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=InputFieldListResponse)
async def list_fields(
    scenario_code: str,
    version_no: int,
    user: AuthenticatedUser = Depends(get_current_user),
    scenario_repo: ScenarioRepository = Depends(get_repo),
    input_field_repo: MysqlScenarioInputFieldRepo = Depends(get_input_field_repo),
):
    """List fields của 1 revision. KHÔNG yêu cầu DRAFT (đọc OK trên published)."""
    defn = await scenario_repo.get_definition(scenario_code)
    if defn is None or defn.is_archived:
        raise HTTPException(404, f"Scenario '{scenario_code}' không tồn tại")

    # Read permission: builtin/own/admin/public
    if not user.is_admin:
        if defn.source_type != "builtin" and defn.visibility != "public":
            if defn.owner_id != user.user_id:
                raise HTTPException(
                    403,
                    f"User '{user.user_id}' không có quyền xem '{scenario_code}'",
                )

    rev = await scenario_repo.get_revision_by_version(
        scenario_code, version_no
    )
    if rev is None:
        raise HTTPException(
            404,
            f"Revision {version_no} của '{scenario_code}' không tồn tại",
        )

    fields = await input_field_repo.list_fields_by_revision(rev.id)
    return InputFieldListResponse(
        fields=[_to_response(f) for f in fields],
        revision_is_draft=(defn.published_revision_id != rev.id),
    )


@router.post("", response_model=InputFieldResponse, status_code=201)
async def create_field(
    scenario_code: str,
    version_no: int,
    body: InputFieldCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    scenario_repo: ScenarioRepository = Depends(get_repo),
    input_field_repo: MysqlScenarioInputFieldRepo = Depends(get_input_field_repo),
    yaml_sync: YamlSyncService = Depends(get_yaml_sync_service),
):
    """Tạo 1 input field mới. Tự động set display_order = max + 1 nếu = 0."""
    rev_id, scenario_pk, is_draft = await _require_writable_revision(
        scenario_code, version_no, user, scenario_repo,
    )
    _ensure_draft(is_draft, rev_id)

    # Auto-assign display_order nếu user không set
    if body.display_order == 0:
        existing = await input_field_repo.list_fields_by_revision(rev_id)
        body.display_order = len(existing)

    field = _base_to_field(body, rev_id, scenario_pk)
    try:
        new_id = await input_field_repo.create_field(field)
    except ValueError as e:
        raise HTTPException(409, str(e))

    # Sync YAML
    try:
        await yaml_sync.regenerate_revision_yaml(rev_id)
    except YamlSyncForbidden as e:
        raise HTTPException(409, str(e))

    created = await input_field_repo.get_field(new_id)
    return _to_response(created)


@router.put("/{field_id}", response_model=InputFieldResponse)
async def update_field(
    scenario_code: str,
    version_no: int,
    field_id: int,
    body: InputFieldUpdateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    scenario_repo: ScenarioRepository = Depends(get_repo),
    input_field_repo: MysqlScenarioInputFieldRepo = Depends(get_input_field_repo),
    yaml_sync: YamlSyncService = Depends(get_yaml_sync_service),
):
    """Update 1 field."""
    rev_id, scenario_pk, is_draft = await _require_writable_revision(
        scenario_code, version_no, user, scenario_repo,
    )
    _ensure_draft(is_draft, rev_id)

    existing = await input_field_repo.get_field(field_id)
    if existing is None:
        raise HTTPException(404, f"Field id={field_id} không tồn tại")
    if existing.revision_id != rev_id:
        raise HTTPException(
            400,
            f"Field id={field_id} không thuộc revision {rev_id}",
        )

    field = _base_to_field(body, rev_id, scenario_pk, field_id=field_id)
    updated = await input_field_repo.update_field(field)
    if not updated:
        raise HTTPException(404, f"Field id={field_id} không tồn tại")

    try:
        await yaml_sync.regenerate_revision_yaml(rev_id)
    except YamlSyncForbidden as e:
        raise HTTPException(409, str(e))

    refreshed = await input_field_repo.get_field(field_id)
    return _to_response(refreshed)


@router.delete("/{field_id}", response_model=DeleteResponse)
async def delete_field(
    scenario_code: str,
    version_no: int,
    field_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
    scenario_repo: ScenarioRepository = Depends(get_repo),
    input_field_repo: MysqlScenarioInputFieldRepo = Depends(get_input_field_repo),
    yaml_sync: YamlSyncService = Depends(get_yaml_sync_service),
):
    rev_id, _, is_draft = await _require_writable_revision(
        scenario_code, version_no, user, scenario_repo,
    )
    _ensure_draft(is_draft, rev_id)

    existing = await input_field_repo.get_field(field_id)
    if existing is None:
        return DeleteResponse(deleted=False)
    if existing.revision_id != rev_id:
        raise HTTPException(
            400,
            f"Field id={field_id} không thuộc revision {rev_id}",
        )

    deleted = await input_field_repo.delete_field(field_id)
    if deleted:
        try:
            await yaml_sync.regenerate_revision_yaml(rev_id)
        except YamlSyncForbidden as e:
            raise HTTPException(409, str(e))
    return DeleteResponse(deleted=deleted)


@router.post("/reorder", response_model=InputFieldListResponse)
async def reorder_fields(
    scenario_code: str,
    version_no: int,
    body: ReorderRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    scenario_repo: ScenarioRepository = Depends(get_repo),
    input_field_repo: MysqlScenarioInputFieldRepo = Depends(get_input_field_repo),
    yaml_sync: YamlSyncService = Depends(get_yaml_sync_service),
):
    """Reorder fields trong 1 revision theo list id."""
    rev_id, _, is_draft = await _require_writable_revision(
        scenario_code, version_no, user, scenario_repo,
    )
    _ensure_draft(is_draft, rev_id)

    try:
        await input_field_repo.reorder_fields(rev_id, body.ordered_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        await yaml_sync.regenerate_revision_yaml(rev_id)
    except YamlSyncForbidden as e:
        raise HTTPException(409, str(e))

    fields = await input_field_repo.list_fields_by_revision(rev_id)
    return InputFieldListResponse(
        fields=[_to_response(f) for f in fields],
        revision_is_draft=True,
    )


@router.post("/bulk", response_model=InputFieldListResponse)
async def bulk_replace_fields(
    scenario_code: str,
    version_no: int,
    body: BulkReplaceRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    scenario_repo: ScenarioRepository = Depends(get_repo),
    input_field_repo: MysqlScenarioInputFieldRepo = Depends(get_input_field_repo),
    yaml_sync: YamlSyncService = Depends(get_yaml_sync_service),
):
    """Replace ALL fields của revision: DELETE hết rồi INSERT lại.

    Atomic qua transaction. UI gọi khi user save toàn bộ form 1 lần.
    """
    rev_id, scenario_pk, is_draft = await _require_writable_revision(
        scenario_code, version_no, user, scenario_repo,
    )
    _ensure_draft(is_draft, rev_id)

    fields_to_insert = [
        _base_to_field(f, rev_id, scenario_pk) for f in body.fields
    ]
    try:
        await input_field_repo.bulk_replace_fields(
            revision_id=rev_id,
            scenario_id=scenario_pk,
            fields=fields_to_insert,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))

    try:
        await yaml_sync.regenerate_revision_yaml(rev_id)
    except YamlSyncForbidden as e:
        raise HTTPException(409, str(e))

    refreshed = await input_field_repo.list_fields_by_revision(rev_id)
    return InputFieldListResponse(
        fields=[_to_response(f) for f in refreshed],
        revision_is_draft=True,
    )
