"""
api/routes/scenario_images.py — Endpoints cho Visual Hint Image (Phase 1).

Prefix: /v1/scenarios/{scenario_code}/revisions/{version_no}/images
Auth: header X-User-Id (qua MockAuthProvider — Phase 1)

Endpoints:
  POST   /v1/scenarios/{code}/revisions/{n}/images          → upload (multipart)
  GET    /v1/scenarios/{code}/revisions/{n}/images          → list
  DELETE /v1/scenarios/{code}/revisions/{n}/images/{name}   → delete

Permission delegated to ScenarioImageService:
- POST/DELETE: owner hoặc admin (builtin chỉ admin)
- GET: builtin/public (everyone) hoặc owner/admin
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from api.dependencies import get_current_user, get_image_service
from auth.providers import AuthenticatedUser
from services.scenario_image_service import (
    ScenarioImageBadRequest,
    ScenarioImageService,
)
from store.mysql_scenario_image_repo import ScenarioImage


router = APIRouter(
    prefix="/v1/scenarios/{scenario_code}/revisions/{version_no}/images",
    tags=["scenario-images"],
)


# ── Response models ──────────────────────────────────────────────────────────

class ImageResponse(BaseModel):
    id: int
    revision_id: int
    scenario_id: int
    filename: str
    cdn_url: str
    mime_type: str
    size_bytes: int
    sha256: Optional[str] = None
    step_index: Optional[int] = None
    step_note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ImageListResponse(BaseModel):
    images: list[ImageResponse]


class DeleteResponse(BaseModel):
    deleted: bool


def _to_resp(img: ScenarioImage) -> ImageResponse:
    return ImageResponse(
        id=img.id or 0,
        revision_id=img.revision_id,
        scenario_id=img.scenario_id,
        filename=img.filename,
        cdn_url=img.cdn_url,
        mime_type=img.mime_type,
        size_bytes=img.size_bytes,
        sha256=img.sha256,
        step_index=img.step_index,
        step_note=img.step_note,
        created_at=img.created_at,
        updated_at=img.updated_at,
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("", response_model=ImageResponse, status_code=201)
async def upload_image(
    scenario_code: str,
    version_no: int,
    file: UploadFile = File(..., description="PNG hoặc JPG, tối đa 5MB"),
    step_index: Optional[int] = Form(
        None, description="0-based index step gắn ảnh (advisory only)"
    ),
    step_note: Optional[str] = Form(
        None, description="Mô tả ngắn (max 255 char) cho UI hiển thị"
    ),
    user: AuthenticatedUser = Depends(get_current_user),
    service: ScenarioImageService = Depends(get_image_service),
):
    """Upload ảnh hint cho 1 revision. Idempotent overwrite cùng filename
    trong cùng revision."""
    file_bytes = await file.read()
    filename = file.filename or ""
    mime = file.content_type or ""

    # `step_note` length sanity (DB cột VARCHAR(255))
    if step_note is not None and len(step_note) > 255:
        raise ScenarioImageBadRequest(
            "step_note vượt quá 255 ký tự."
        )

    img = await service.upload_image(
        scenario_code=scenario_code,
        version_no=version_no,
        filename=filename,
        file_bytes=file_bytes,
        mime_type=mime,
        user=user,
        step_index=step_index,
        step_note=step_note,
    )
    return _to_resp(img)


@router.get("", response_model=ImageListResponse)
async def list_images(
    scenario_code: str,
    version_no: int,
    user: AuthenticatedUser = Depends(get_current_user),
    service: ScenarioImageService = Depends(get_image_service),
):
    """List ảnh hint của 1 revision."""
    images = await service.list_images(scenario_code, version_no, user)
    return ImageListResponse(images=[_to_resp(i) for i in images])


@router.delete("/{filename}", response_model=DeleteResponse)
async def delete_image(
    scenario_code: str,
    version_no: int,
    filename: str,
    user: AuthenticatedUser = Depends(get_current_user),
    service: ScenarioImageService = Depends(get_image_service),
):
    """Xóa 1 ảnh hint khỏi DB. Không xóa file MinIO (Phase sau có cron GC)."""
    deleted = await service.delete_image(
        scenario_code, version_no, filename, user
    )
    return DeleteResponse(deleted=deleted)
