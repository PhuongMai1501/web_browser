"""
store/mysql_scenario_image_repo.py — Repository cho bảng `scenario_images`
(Visual Hint Targeting Phase 1).

Lưu metadata ảnh khoanh đỏ user upload làm hint cho `TargetSpec.image_hint`.
File binary nằm trên MinIO; bảng này chỉ giữ metadata + CDN URL.

Schema (xem `migrations/002_scenario_images.sql`):
- id BIGINT PK AUTO_INCREMENT
- revision_id BIGINT (FK app-layer → scenario_revisions.id)
- scenario_id BIGINT (denorm FK → scenario_definitions.id, để query theo scenario)
- filename, cdn_url, mime_type, size_bytes, sha256
- step_index INT NULL, step_note VARCHAR NULL
- date_created/date_updated TIMESTAMP DEFAULT utc_timestamp()
- created_by/updated_by BIGINT NULL (Phase 1 luôn NULL)

UNIQUE (revision_id, filename) — re-upload cùng filename trong cùng revision
là overwrite (Q-C trong PLAN_VISUAL_HINTS.md).

Convention nhất quán với MysqlScenarioRepo: aiomysql pool, DictCursor,
business id BIGINT, không expose qua Pydantic ngoại trừ via repo methods.

Phase 1 chỉ có 1 implementation (MySQL) → KHÔNG tách abstract interface.
Refactor sau nếu cần SQLite/Postgres parity.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import aiomysql
from pydantic import BaseModel

_log = logging.getLogger(__name__)


# ── Pydantic model ───────────────────────────────────────────────────────────

class ScenarioImage(BaseModel):
    """Row in `scenario_images` table.

    `id`, `created_at`, `updated_at` là None khi chưa persist; được fill
    sau khi insert hoặc khi load từ DB.
    """

    id: Optional[int] = None
    revision_id: int
    scenario_id: int
    filename: str
    cdn_url: str
    mime_type: str = "image/png"
    size_bytes: int
    sha256: Optional[str] = None
    step_index: Optional[int] = None
    step_note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ensure_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive datetime từ MySQL TIMESTAMP → aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_image(row: dict) -> ScenarioImage:
    return ScenarioImage(
        id=row["id"],
        revision_id=int(row["revision_id"]),
        scenario_id=int(row["scenario_id"]),
        filename=row["filename"],
        cdn_url=row["cdn_url"],
        mime_type=row.get("mime_type") or "image/png",
        size_bytes=int(row["size_bytes"]),
        sha256=row.get("sha256"),
        step_index=row.get("step_index"),
        step_note=row.get("step_note"),
        created_at=_ensure_aware_utc(row.get("date_created")),
        updated_at=_ensure_aware_utc(row.get("date_updated")),
    )


# ── Repository ───────────────────────────────────────────────────────────────

class MysqlScenarioImageRepo:
    """aiomysql repo cho `scenario_images`. Connection pool, per-op acquire."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        db: str,
        pool_size: int = 3,
        charset: str = "utf8mb4",
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._db = db
        self._pool_size = pool_size
        self._charset = charset
        self._pool: Optional[aiomysql.Pool] = None

    async def init(self) -> None:
        """Open pool. Set timezone UTC để TIMESTAMP read/write consistent."""
        self._pool = await aiomysql.create_pool(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            db=self._db,
            charset=self._charset,
            autocommit=True,
            minsize=1,
            maxsize=self._pool_size,
            init_command="SET time_zone='+00:00'",
        )
        _log.info(
            "MysqlScenarioImageRepo initialized: %s:%d/%s pool=%d",
            self._host, self._port, self._db, self._pool_size,
        )

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    def _get_pool(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError(
                "Repo chưa init. Gọi await repo.init() trước."
            )
        return self._pool

    # ── CRUD ─────────────────────────────────────────────────────────────────

    async def create_image(self, image: ScenarioImage) -> int:
        """Insert row. Trả về id của row mới.

        Raise ValueError nếu vi phạm UNIQUE(revision_id, filename) — caller
        nên check trước hoặc bắt exception để xử lý overwrite.
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    await cur.execute(
                        """
                        INSERT INTO scenario_images
                            (revision_id, scenario_id, filename, cdn_url,
                             mime_type, size_bytes, sha256,
                             step_index, step_note,
                             date_created, date_updated,
                             created_by, updated_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s)
                        """,
                        (
                            image.revision_id,
                            image.scenario_id,
                            image.filename,
                            image.cdn_url,
                            image.mime_type,
                            image.size_bytes,
                            image.sha256,
                            image.step_index,
                            image.step_note,
                            image.created_at or datetime.now(timezone.utc),
                            image.updated_at or datetime.now(timezone.utc),
                            None,        # created_by Phase 1 NULL
                            None,        # updated_by Phase 1 NULL
                        ),
                    )
                    return int(cur.lastrowid)
                except aiomysql.IntegrityError as e:
                    raise ValueError(
                        f"Image '{image.filename}' đã tồn tại trong "
                        f"revision_id={image.revision_id}"
                    ) from e

    async def get_image(self, image_id: int) -> Optional[ScenarioImage]:
        """Lookup theo PK id."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM scenario_images WHERE id = %s",
                    (image_id,),
                )
                row = await cur.fetchone()
        return _row_to_image(row) if row else None

    async def get_image_by_filename(
        self, revision_id: int, filename: str
    ) -> Optional[ScenarioImage]:
        """Lookup theo (revision_id, filename) — composite UNIQUE."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM scenario_images
                    WHERE revision_id = %s AND filename = %s
                    """,
                    (revision_id, filename),
                )
                row = await cur.fetchone()
        return _row_to_image(row) if row else None

    async def list_by_revision(
        self, revision_id: int
    ) -> list[ScenarioImage]:
        """List ảnh của 1 revision. Sort theo step_index NULL last,
        rồi filename — UI hiển thị thứ tự gần với flow."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM scenario_images
                    WHERE revision_id = %s
                    ORDER BY step_index IS NULL, step_index ASC, filename ASC
                    """,
                    (revision_id,),
                )
                rows = await cur.fetchall()
        return [_row_to_image(r) for r in rows]

    async def list_by_scenario(
        self, scenario_id: int, limit: int = 200
    ) -> list[ScenarioImage]:
        """List ảnh tất cả revision của 1 scenario (admin/audit view)."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM scenario_images
                    WHERE scenario_id = %s
                    ORDER BY revision_id DESC, step_index IS NULL,
                             step_index ASC, filename ASC
                    LIMIT %s
                    """,
                    (scenario_id, limit),
                )
                rows = await cur.fetchall()
        return [_row_to_image(r) for r in rows]

    async def find_by_sha256(
        self, sha256: str
    ) -> Optional[ScenarioImage]:
        """Tìm 1 row có cùng sha256 — dedup helper. Trả row đầu tiên.

        Service layer dùng để decide reuse cdn_url thay vì re-upload binary
        khi user upload cùng ảnh nhiều lần (Q-E giữ sha256).
        """
        if not sha256:
            return None
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM scenario_images
                    WHERE sha256 = %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (sha256,),
                )
                row = await cur.fetchone()
        return _row_to_image(row) if row else None

    async def delete_image(
        self, revision_id: int, filename: str
    ) -> bool:
        """Xóa 1 ảnh. Trả True nếu có row bị xóa, False nếu không tồn tại.

        ⚠️ Không xóa file binary trên MinIO — caller (service layer) chịu
        trách nhiệm xóa MinIO sau khi DB delete thành công.
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    DELETE FROM scenario_images
                    WHERE revision_id = %s AND filename = %s
                    """,
                    (revision_id, filename),
                )
                return cur.rowcount > 0

    async def delete_by_revision(self, revision_id: int) -> int:
        """GC khi xóa revision. Trả số row bị xóa.

        ⚠️ Tương tự `delete_image` — không xóa MinIO. Caller phải orchestrate.
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM scenario_images WHERE revision_id = %s",
                    (revision_id,),
                )
                return cur.rowcount
