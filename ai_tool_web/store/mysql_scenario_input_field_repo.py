"""
store/mysql_scenario_input_field_repo.py — Repository cho bảng
`scenario_input_fields` (Phase 1 Sprint 1, 2026-05-11).

Schema (xem `migrations/003_scenario_input_fields.sql`):
- id BIGINT PK AUTO_INCREMENT
- revision_id BIGINT (FK app-layer → scenario_revisions.id)
- scenario_id BIGINT (denorm FK → scenario_definitions.id)
- name VARCHAR(64), display_label VARCHAR(255), field_type VARCHAR(16)
- is_required INT(0/1), source VARCHAR(16) 'context'|'ask_user'
- default_value TEXT NULL (app cast theo field_type)
- description TEXT NULL
- validation_rules JSON NULL (JSON Schema 7 subset)
- placeholder VARCHAR(255), help_text TEXT, display_order INT
- category VARCHAR(32) 'user_input'|... (Phase 2)
- secret_ref VARCHAR(255) NULL (Phase 2)
- extraction_hint TEXT NULL (Phase 2)
- template_id BIGINT NULL (Phase 3)
- date_created/date_updated TIMESTAMP DEFAULT utc_timestamp()
- created_by/updated_by BIGINT NULL (Phase 1 mock auth → NULL)

UNIQUE (revision_id, name) — không có 2 field cùng `name` trong 1 revision.

Convention nhất quán với MysqlScenarioImageRepo / MysqlScenarioRepo:
aiomysql pool, DictCursor, business id BIGINT, không add FK constraint.

Phase 1 chỉ 1 implementation (MySQL). Refactor abstract interface sau nếu cần.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiomysql
from pydantic import BaseModel, Field

_log = logging.getLogger(__name__)


# ── Pydantic model ───────────────────────────────────────────────────────────

class ScenarioInputField(BaseModel):
    """Row trong `scenario_input_fields` table.

    `id`, `created_at`, `updated_at` là None khi chưa persist; được fill
    sau khi insert hoặc khi load từ DB.

    Field naming map:
        DB column           → Pydantic attr
        date_created        → created_at
        date_updated        → updated_at
        is_required (0/1)   → is_required (bool)
    """

    id: Optional[int] = None
    revision_id: int
    scenario_id: int

    name: str = Field(..., max_length=64)
    display_label: str = Field(..., max_length=255)
    field_type: str = Field(..., max_length=16)  # string|secret|number|bool
    is_required: bool = False
    source: str = Field(default="context", max_length=16)  # context|ask_user
    default_value: Optional[str] = None
    description: Optional[str] = None

    validation_rules: Optional[dict[str, Any]] = None

    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    display_order: int = 0

    # Phase 2 reserved (UI Phase 1 không expose, schema sẵn)
    category: str = "user_input"
    secret_ref: Optional[str] = None
    extraction_hint: Optional[str] = None

    # Phase 3 reserved
    template_id: Optional[int] = None

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


def _row_to_field(row: dict) -> ScenarioInputField:
    """Convert DictCursor row → ScenarioInputField."""
    validation_rules_raw = row.get("validation_rules")
    if isinstance(validation_rules_raw, (bytes, bytearray)):
        validation_rules_raw = validation_rules_raw.decode("utf-8")
    if isinstance(validation_rules_raw, str):
        try:
            validation_rules = json.loads(validation_rules_raw)
        except json.JSONDecodeError:
            _log.warning(
                "Invalid JSON in validation_rules for field id=%s, ignored",
                row.get("id"),
            )
            validation_rules = None
    else:
        validation_rules = validation_rules_raw  # dict hoặc None

    return ScenarioInputField(
        id=row["id"],
        revision_id=int(row["revision_id"]),
        scenario_id=int(row["scenario_id"]),
        name=row["name"],
        display_label=row["display_label"],
        field_type=row["field_type"],
        is_required=bool(row["is_required"]),
        source=row["source"],
        default_value=row.get("default_value"),
        description=row.get("description"),
        validation_rules=validation_rules,
        placeholder=row.get("placeholder"),
        help_text=row.get("help_text"),
        display_order=int(row.get("display_order") or 0),
        category=row.get("category") or "user_input",
        secret_ref=row.get("secret_ref"),
        extraction_hint=row.get("extraction_hint"),
        template_id=row.get("template_id"),
        created_at=_ensure_aware_utc(row.get("date_created")),
        updated_at=_ensure_aware_utc(row.get("date_updated")),
    )


def _serialize_validation_rules(rules: Optional[dict[str, Any]]) -> Optional[str]:
    """Pydantic dict → JSON string cho DB."""
    if rules is None:
        return None
    return json.dumps(rules, ensure_ascii=False, sort_keys=True)


# ── Repository ───────────────────────────────────────────────────────────────

class MysqlScenarioInputFieldRepo:
    """aiomysql repo cho `scenario_input_fields`. Connection pool, per-op acquire."""

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
            "MysqlScenarioInputFieldRepo initialized: %s:%d/%s pool=%d",
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

    async def create_field(self, field: ScenarioInputField) -> int:
        """Insert row. Trả về id của row mới.

        Raise ValueError nếu vi phạm UNIQUE(revision_id, name) — caller
        nên check trước hoặc bắt exception để xử lý overwrite.
        """
        pool = self._get_pool()
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    await cur.execute(
                        """
                        INSERT INTO scenario_input_fields
                            (revision_id, scenario_id,
                             name, display_label, field_type, is_required, source,
                             default_value, description, validation_rules,
                             placeholder, help_text, display_order,
                             category, secret_ref, extraction_hint, template_id,
                             date_created, date_updated, created_by, updated_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            field.revision_id,
                            field.scenario_id,
                            field.name,
                            field.display_label,
                            field.field_type,
                            1 if field.is_required else 0,
                            field.source,
                            field.default_value,
                            field.description,
                            _serialize_validation_rules(field.validation_rules),
                            field.placeholder,
                            field.help_text,
                            field.display_order,
                            field.category,
                            field.secret_ref,
                            field.extraction_hint,
                            field.template_id,
                            field.created_at or now,
                            field.updated_at or now,
                            None,        # created_by Phase 1 NULL
                            None,        # updated_by Phase 1 NULL
                        ),
                    )
                    return int(cur.lastrowid)
                except aiomysql.IntegrityError as e:
                    raise ValueError(
                        f"Field name='{field.name}' đã tồn tại trong "
                        f"revision_id={field.revision_id}"
                    ) from e

    async def get_field(self, field_id: int) -> Optional[ScenarioInputField]:
        """Lookup theo PK id."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM scenario_input_fields WHERE id = %s",
                    (field_id,),
                )
                row = await cur.fetchone()
        return _row_to_field(row) if row else None

    async def get_field_by_name(
        self, revision_id: int, name: str
    ) -> Optional[ScenarioInputField]:
        """Lookup theo UNIQUE(revision_id, name)."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM scenario_input_fields
                    WHERE revision_id = %s AND name = %s
                    """,
                    (revision_id, name),
                )
                row = await cur.fetchone()
        return _row_to_field(row) if row else None

    async def list_fields_by_revision(
        self, revision_id: int
    ) -> list[ScenarioInputField]:
        """List fields của 1 revision theo display_order ASC.

        Trả về [] nếu revision không có field (vd revision dạng agent mode
        không cần inputs khai báo).
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM scenario_input_fields
                    WHERE revision_id = %s
                    ORDER BY display_order ASC, id ASC
                    """,
                    (revision_id,),
                )
                rows = await cur.fetchall()
        return [_row_to_field(r) for r in rows]

    async def update_field(self, field: ScenarioInputField) -> bool:
        """Update full field theo id. Trả về True nếu có row update,
        False nếu id không tồn tại.

        Tự động set date_updated = now.
        """
        if field.id is None:
            raise ValueError("Field.id required cho update")
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE scenario_input_fields
                    SET name = %s,
                        display_label = %s,
                        field_type = %s,
                        is_required = %s,
                        source = %s,
                        default_value = %s,
                        description = %s,
                        validation_rules = %s,
                        placeholder = %s,
                        help_text = %s,
                        display_order = %s,
                        category = %s,
                        secret_ref = %s,
                        extraction_hint = %s,
                        template_id = %s,
                        date_updated = %s
                    WHERE id = %s
                    """,
                    (
                        field.name,
                        field.display_label,
                        field.field_type,
                        1 if field.is_required else 0,
                        field.source,
                        field.default_value,
                        field.description,
                        _serialize_validation_rules(field.validation_rules),
                        field.placeholder,
                        field.help_text,
                        field.display_order,
                        field.category,
                        field.secret_ref,
                        field.extraction_hint,
                        field.template_id,
                        datetime.now(timezone.utc),
                        field.id,
                    ),
                )
                return cur.rowcount > 0

    async def delete_field(self, field_id: int) -> bool:
        """Hard delete theo id. Trả về True nếu có row delete."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM scenario_input_fields WHERE id = %s",
                    (field_id,),
                )
                return cur.rowcount > 0

    # ── Bulk operations ──────────────────────────────────────────────────────

    async def bulk_replace_fields(
        self,
        revision_id: int,
        scenario_id: int,
        fields: list[ScenarioInputField],
    ) -> list[int]:
        """Replace ALL fields của 1 revision: DELETE hết rồi INSERT lại.

        Dùng cho UI tab "Inputs" khi user save toàn bộ form 1 lần.
        Atomic qua transaction.

        Trả về list id mới (theo thứ tự fields input).
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM scenario_input_fields WHERE revision_id = %s",
                        (revision_id,),
                    )

                    new_ids: list[int] = []
                    now = datetime.now(timezone.utc)
                    for idx, field in enumerate(fields):
                        await cur.execute(
                            """
                            INSERT INTO scenario_input_fields
                                (revision_id, scenario_id,
                                 name, display_label, field_type, is_required, source,
                                 default_value, description, validation_rules,
                                 placeholder, help_text, display_order,
                                 category, secret_ref, extraction_hint, template_id,
                                 date_created, date_updated, created_by, updated_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                revision_id,
                                scenario_id,
                                field.name,
                                field.display_label,
                                field.field_type,
                                1 if field.is_required else 0,
                                field.source,
                                field.default_value,
                                field.description,
                                _serialize_validation_rules(field.validation_rules),
                                field.placeholder,
                                field.help_text,
                                idx,  # display_order theo thứ tự input
                                field.category,
                                field.secret_ref,
                                field.extraction_hint,
                                field.template_id,
                                now,
                                now,
                                None,
                                None,
                            ),
                        )
                        new_ids.append(int(cur.lastrowid))
                await conn.commit()
                return new_ids
            except Exception:
                await conn.rollback()
                raise

    async def reorder_fields(
        self, revision_id: int, ordered_field_ids: list[int]
    ) -> None:
        """Update display_order theo thứ tự id trong list. Atomic qua transaction.

        Raise ValueError nếu có id không thuộc revision_id.
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    # Validate tất cả id thuộc revision này
                    placeholders = ",".join(["%s"] * len(ordered_field_ids))
                    await cur.execute(
                        f"""
                        SELECT id FROM scenario_input_fields
                        WHERE id IN ({placeholders}) AND revision_id = %s
                        """,
                        (*ordered_field_ids, revision_id),
                    )
                    rows = await cur.fetchall()
                    valid_ids = {r[0] for r in rows}
                    invalid = [i for i in ordered_field_ids if i not in valid_ids]
                    if invalid:
                        raise ValueError(
                            f"Field IDs không thuộc revision_id={revision_id}: {invalid}"
                        )

                    now = datetime.now(timezone.utc)
                    for idx, field_id in enumerate(ordered_field_ids):
                        await cur.execute(
                            """
                            UPDATE scenario_input_fields
                            SET display_order = %s, date_updated = %s
                            WHERE id = %s
                            """,
                            (idx, now, field_id),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    # ── Cascade helpers (app-layer enforce, không có FK) ─────────────────────

    async def delete_by_revision(self, revision_id: int) -> int:
        """Xóa hết fields của 1 revision. Gọi từ service khi xóa revision.
        Trả về số row đã xóa."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM scenario_input_fields WHERE revision_id = %s",
                    (revision_id,),
                )
                return cur.rowcount

    async def delete_by_scenario(self, scenario_id: int) -> int:
        """Xóa hết fields của tất cả revisions thuộc 1 scenario. Gọi từ service
        khi hard delete scenario_definition.
        Trả về số row đã xóa."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM scenario_input_fields WHERE scenario_id = %s",
                    (scenario_id,),
                )
                return cur.rowcount

    async def clone_fields_to_revision(
        self,
        src_revision_id: int,
        dst_revision_id: int,
        dst_scenario_id: int,
    ) -> int:
        """Copy tất cả fields từ src revision sang dst revision (cho clone scenario
        hoặc tạo revision mới từ rev cũ). Trả về số row đã copy.

        Giữ nguyên display_order. Reset id (AUTO_INCREMENT).
        """
        src_fields = await self.list_fields_by_revision(src_revision_id)
        if not src_fields:
            return 0

        pool = self._get_pool()
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    for f in src_fields:
                        await cur.execute(
                            """
                            INSERT INTO scenario_input_fields
                                (revision_id, scenario_id,
                                 name, display_label, field_type, is_required, source,
                                 default_value, description, validation_rules,
                                 placeholder, help_text, display_order,
                                 category, secret_ref, extraction_hint, template_id,
                                 date_created, date_updated, created_by, updated_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                dst_revision_id,
                                dst_scenario_id,
                                f.name,
                                f.display_label,
                                f.field_type,
                                1 if f.is_required else 0,
                                f.source,
                                f.default_value,
                                f.description,
                                _serialize_validation_rules(f.validation_rules),
                                f.placeholder,
                                f.help_text,
                                f.display_order,
                                f.category,
                                f.secret_ref,
                                f.extraction_hint,
                                f.template_id,
                                now,
                                now,
                                None,
                                None,
                            ),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return len(src_fields)

    # ── Count / stats ────────────────────────────────────────────────────────

    async def count_by_revision(self, revision_id: int) -> int:
        """Đếm số fields của 1 revision (dùng cho backfill idempotent check)."""
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM scenario_input_fields WHERE revision_id = %s",
                    (revision_id,),
                )
                row = await cur.fetchone()
        return int(row[0]) if row else 0
