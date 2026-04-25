"""
store/mysql_scenario_repo.py — MariaDB/MySQL implementation của ScenarioRepository.

Phase 1 production engine. Async qua aiomysql, connection pool.

Schema mapping (model ↔ DB column):

| Model field                          | DB column                                            |
|--------------------------------------|------------------------------------------------------|
| ScenarioDefinition.id (str)          | scenario_definitions.code (VARCHAR(64) UNIQUE)       |
| ScenarioDefinition.owner_id (str)    | scenario_definitions.owner_code (VARCHAR(64))        |
| ScenarioDefinition.created_at        | scenario_definitions.date_created                    |
| ScenarioDefinition.updated_at        | scenario_definitions.date_updated                    |
| ScenarioRevision.scenario_id (str)   | resolve via scenario_definitions.code → id BIGINT FK |
| ScenarioRevision.created_by (str)    | DB created_by BIGINT NULL (Phase 1: ghi NULL)        |
| ScenarioRevision.created_at          | date_created                                         |
| ScenarioRun.scenario_id (str)        | same — code → BIGINT lookup                          |
| ScenarioRun.started_by (str)         | DB started_by BIGINT NULL (Phase 1: ghi NULL)        |
| ScenarioRun.created_at               | date_created                                         |

DB column `id BIGINT AUTO_INCREMENT` chỉ dùng nội bộ làm FK target —
không expose ra model. Service layer luôn dùng `code` string làm business key.

Phase 2 (real auth) sẽ populate INT user IDs vào created_by/owner_id;
hiện tại dùng `owner_code` string làm bridge. Audit fields created_by/started_by
mất giá trị string lúc save; reload return "" sentinel.

Connection pool size mặc định 5, configurable qua MYSQL_POOL_SIZE env.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiomysql

from store.scenario_repo import (
    DefinitionFilters,
    ScenarioDefinition,
    ScenarioRepository,
    ScenarioRevision,
    ScenarioRun,
)


_log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _json_dump(v: Any) -> Optional[str]:
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, default=str)


def _json_load(v: Any) -> Any:
    """MariaDB JSON column trả về str (LONGTEXT + CHECK json_valid).
    Nếu driver đã decode (rare) thì trả về dict/list trực tiếp.
    """
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    return json.loads(v)


def _ensure_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Chuyển naive datetime (từ MySQL TIMESTAMP) sang aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Row → model conversion ───────────────────────────────────────────────────

def _row_to_definition(row: dict) -> ScenarioDefinition:
    return ScenarioDefinition(
        id=row["code"],
        name=row["name"],
        owner_id=row.get("owner_code"),
        org_id=str(row["org_id"]) if row.get("org_id") is not None else None,
        source_type=row["source_type"],
        visibility=row["visibility"],
        published_revision_id=row.get("published_revision_id"),
        is_archived=bool(row.get("is_archived") or 0),
        created_at=_ensure_aware_utc(row["date_created"]),
        updated_at=_ensure_aware_utc(row["date_updated"]),
    )


def _row_to_revision(row: dict) -> ScenarioRevision:
    return ScenarioRevision(
        id=row["id"],
        scenario_id=row["scenario_code"],
        version_no=row["version_no"],
        raw_yaml=row["raw_yaml"],
        normalized_spec_json=_json_load(row["normalized_spec_json"]) or {},
        yaml_hash=row["yaml_hash"],
        parent_revision_id=row.get("parent_revision_id"),
        clone_source_revision_id=row.get("clone_source_revision_id"),
        schema_version=row.get("schema_version") or 1,
        static_validation_status=row["static_validation_status"],
        static_validation_errors=_json_load(row.get("static_validation_errors")),
        last_test_run_at=_ensure_aware_utc(row.get("last_test_run_at")),
        last_test_run_status=row.get("last_test_run_status"),
        last_test_run_id=row.get("last_test_run_id"),
        created_by=str(row.get("created_by") or ""),
        created_at=_ensure_aware_utc(row["date_created"]),
    )


def _row_to_run(row: dict) -> ScenarioRun:
    return ScenarioRun(
        id=row["id"],
        scenario_id=row["scenario_code"],
        revision_id=row["revision_id"],
        session_id=row["session_id"],
        mode=row["mode"],
        started_by=str(row.get("started_by") or ""),
        runtime_policy_snapshot=_json_load(row["runtime_policy_snapshot"]) or {},
        status=row["status"],
        created_at=_ensure_aware_utc(row["date_created"]),
    )


# ── Repository ───────────────────────────────────────────────────────────────

class MysqlScenarioRepo(ScenarioRepository):
    """aiomysql implementation. Connection pool, per-op acquire.

    Usage:
        repo = MysqlScenarioRepo(
            host="172.28.8.11", port=3306,
            user="chatbotadmin", password="...",
            db="changchatbot",
        )
        await repo.init()
        ...
        await repo.close()
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        db: str,
        pool_size: int = 5,
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
        # Lock serialize multi-statement transactions để tránh race version_no.
        # UNIQUE(scenario_id, version_no) là defense-in-depth.
        self._tx_lock = asyncio.Lock()

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
            "MysqlScenarioRepo initialized: %s:%d/%s pool=%d",
            self._host, self._port, self._db, self._pool_size,
        )

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    def _get_pool(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError("Repo chưa init. Gọi await repo.init() trước.")
        return self._pool

    async def _resolve_scenario_pk(
        self, cur: aiomysql.DictCursor, code: str
    ) -> int:
        """Lookup scenario_definitions.id (BIGINT FK target) từ code string.
        Raise ValueError nếu không tồn tại."""
        await cur.execute(
            "SELECT id FROM scenario_definitions WHERE code = %s",
            (code,),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"Scenario code '{code}' không tồn tại")
        return int(row["id"])

    # ── Definitions ──────────────────────────────────────────────────────────

    async def create_definition(self, defn: ScenarioDefinition) -> None:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    await cur.execute(
                        """
                        INSERT INTO scenario_definitions
                            (code, name, owner_id, owner_code, org_id,
                             source_type, visibility, published_revision_id,
                             is_archived, date_created, date_updated,
                             created_by, updated_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            defn.id,
                            defn.name,
                            None,                         # owner_id BIGINT — Phase 1 luôn NULL
                            defn.owner_id,                # owner_code VARCHAR
                            None,                         # org_id INT — Phase 2
                            defn.source_type,
                            defn.visibility,
                            defn.published_revision_id,
                            1 if defn.is_archived else 0,
                            defn.created_at,
                            defn.updated_at,
                            None,                         # created_by — Phase 1 NULL
                            None,                         # updated_by — Phase 1 NULL
                        ),
                    )
                except aiomysql.IntegrityError as e:
                    raise ValueError(
                        f"Scenario id '{defn.id}' đã tồn tại"
                    ) from e

    async def get_definition(
        self, scenario_id: str
    ) -> Optional[ScenarioDefinition]:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM scenario_definitions WHERE code = %s",
                    (scenario_id,),
                )
                row = await cur.fetchone()
        return _row_to_definition(row) if row else None

    async def list_definitions(
        self, filters: DefinitionFilters
    ) -> list[ScenarioDefinition]:
        clauses: list[str] = []
        params: list[Any] = []

        if filters.owner_id is not None:
            clauses.append("owner_code = %s")
            params.append(filters.owner_id)
        if filters.source_type is not None:
            clauses.append("source_type = %s")
            params.append(filters.source_type)
        if filters.is_archived is False:
            clauses.append("is_archived = 0")
        elif filters.is_archived is True:
            clauses.append("is_archived = 1")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM scenario_definitions
            {where}
            ORDER BY date_updated DESC
            LIMIT %s OFFSET %s
        """
        params.extend([filters.limit, filters.offset])

        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return [_row_to_definition(r) for r in rows]

    async def archive_definition(self, scenario_id: str) -> None:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE scenario_definitions
                    SET is_archived = 1, date_updated = %s
                    WHERE code = %s
                    """,
                    (_now_utc(), scenario_id),
                )

    async def set_published_revision(
        self, scenario_id: str, rev_id: Optional[int]
    ) -> None:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE scenario_definitions
                    SET published_revision_id = %s, date_updated = %s
                    WHERE code = %s
                    """,
                    (rev_id, _now_utc(), scenario_id),
                )

    async def count_builtin(self) -> int:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT COUNT(*) AS c FROM scenario_definitions "
                    "WHERE source_type = 'builtin'"
                )
                row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def count_by_owner(self, owner_id: str) -> int:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT COUNT(*) AS c FROM scenario_definitions
                    WHERE owner_code = %s AND is_archived = 0
                    """,
                    (owner_id,),
                )
                row = await cur.fetchone()
        return int(row["c"]) if row else 0

    # ── Revisions ────────────────────────────────────────────────────────────

    async def append_revision(self, rev: ScenarioRevision) -> int:
        """Insert revision với version_no = max+1 cho scenario.

        Transaction: BEGIN → SELECT MAX → INSERT → COMMIT.
        Lock serialize cross-coroutine. UNIQUE(scenario_id, version_no) là
        defense-in-depth.
        """
        pool = self._get_pool()
        async with self._tx_lock:
            async with pool.acquire() as conn:
                # Tắt autocommit cho transaction tường minh
                await conn.begin()
                try:
                    async with conn.cursor(aiomysql.DictCursor) as cur:
                        scenario_pk = await self._resolve_scenario_pk(
                            cur, rev.scenario_id
                        )

                        await cur.execute(
                            """
                            SELECT COALESCE(MAX(version_no), 0) AS mx
                            FROM scenario_revisions
                            WHERE scenario_id = %s
                            """,
                            (scenario_pk,),
                        )
                        row = await cur.fetchone()
                        next_version = int(row["mx"] if row else 0) + 1

                        await cur.execute(
                            """
                            INSERT INTO scenario_revisions
                                (scenario_id, version_no, raw_yaml,
                                 normalized_spec_json, yaml_hash,
                                 parent_revision_id, clone_source_revision_id,
                                 schema_version, static_validation_status,
                                 static_validation_errors,
                                 last_test_run_at, last_test_run_status,
                                 last_test_run_id,
                                 created_by, date_created, date_updated,
                                 updated_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                scenario_pk,
                                next_version,
                                rev.raw_yaml,
                                _json_dump(rev.normalized_spec_json),
                                rev.yaml_hash,
                                rev.parent_revision_id,
                                rev.clone_source_revision_id,
                                rev.schema_version,
                                rev.static_validation_status,
                                _json_dump(rev.static_validation_errors),
                                rev.last_test_run_at,
                                rev.last_test_run_status,
                                rev.last_test_run_id,
                                None,                  # created_by Phase 1 NULL
                                rev.created_at,
                                rev.created_at,        # date_updated = same lúc tạo
                                None,                  # updated_by
                            ),
                        )
                        new_id = cur.lastrowid
                    await conn.commit()
                    return int(new_id)
                except Exception:
                    await conn.rollback()
                    raise

    async def get_revision(self, rev_id: int) -> Optional[ScenarioRevision]:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT r.*, d.code AS scenario_code
                    FROM scenario_revisions r
                    JOIN scenario_definitions d ON d.id = r.scenario_id
                    WHERE r.id = %s
                    """,
                    (rev_id,),
                )
                row = await cur.fetchone()
        return _row_to_revision(row) if row else None

    async def get_revision_by_version(
        self, scenario_id: str, version_no: int
    ) -> Optional[ScenarioRevision]:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT r.*, d.code AS scenario_code
                    FROM scenario_revisions r
                    JOIN scenario_definitions d ON d.id = r.scenario_id
                    WHERE d.code = %s AND r.version_no = %s
                    """,
                    (scenario_id, version_no),
                )
                row = await cur.fetchone()
        return _row_to_revision(row) if row else None

    async def list_revisions(
        self,
        scenario_id: str,
        limit: int = 20,
        before_id: Optional[int] = None,
    ) -> list[ScenarioRevision]:
        clauses = ["d.code = %s"]
        params: list[Any] = [scenario_id]
        if before_id is not None:
            clauses.append("r.id < %s")
            params.append(before_id)
        params.append(limit)

        sql = f"""
            SELECT r.*, d.code AS scenario_code
            FROM scenario_revisions r
            JOIN scenario_definitions d ON d.id = r.scenario_id
            WHERE {' AND '.join(clauses)}
            ORDER BY r.id DESC
            LIMIT %s
        """
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return [_row_to_revision(r) for r in rows]

    async def get_latest_revision(
        self, scenario_id: str
    ) -> Optional[ScenarioRevision]:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT r.*, d.code AS scenario_code
                    FROM scenario_revisions r
                    JOIN scenario_definitions d ON d.id = r.scenario_id
                    WHERE d.code = %s
                    ORDER BY r.version_no DESC
                    LIMIT 1
                    """,
                    (scenario_id,),
                )
                row = await cur.fetchone()
        return _row_to_revision(row) if row else None

    async def get_published_revision(
        self, scenario_id: str
    ) -> Optional[ScenarioRevision]:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT r.*, d.code AS scenario_code
                    FROM scenario_revisions r
                    JOIN scenario_definitions d
                        ON d.published_revision_id = r.id
                    WHERE d.code = %s
                    """,
                    (scenario_id,),
                )
                row = await cur.fetchone()
        return _row_to_revision(row) if row else None

    async def update_revision_test_status(
        self,
        rev_id: int,
        status: str,
        run_id: int,
        at: datetime,
    ) -> None:
        if status not in ("passed", "failed"):
            raise ValueError(f"Invalid test status: {status}")
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE scenario_revisions
                    SET last_test_run_status = %s,
                        last_test_run_id = %s,
                        last_test_run_at = %s
                    WHERE id = %s
                    """,
                    (status, run_id, at, rev_id),
                )

    # ── Runs ─────────────────────────────────────────────────────────────────

    async def create_run(self, run: ScenarioRun) -> int:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                scenario_pk = await self._resolve_scenario_pk(
                    cur, run.scenario_id
                )
                await cur.execute(
                    """
                    INSERT INTO scenario_runs
                        (scenario_id, revision_id, session_id, mode,
                         started_by, runtime_policy_snapshot, status,
                         date_created, date_updated, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        scenario_pk,
                        run.revision_id,
                        run.session_id,
                        run.mode,
                        None,                          # started_by Phase 1 NULL
                        _json_dump(run.runtime_policy_snapshot),
                        run.status,
                        run.created_at,
                        run.created_at,
                        None,
                        None,
                    ),
                )
                return int(cur.lastrowid)

    async def update_run_status(self, run_id: int, status: str) -> None:
        if status not in ("running", "completed", "failed", "cancelled"):
            raise ValueError(f"Invalid run status: {status}")
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE scenario_runs
                    SET status = %s, date_updated = %s
                    WHERE id = %s
                    """,
                    (status, _now_utc(), run_id),
                )

    async def get_run(self, run_id: int) -> Optional[ScenarioRun]:
        pool = self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT r.*, d.code AS scenario_code
                    FROM scenario_runs r
                    JOIN scenario_definitions d ON d.id = r.scenario_id
                    WHERE r.id = %s
                    """,
                    (run_id,),
                )
                row = await cur.fetchone()
        return _row_to_run(row) if row else None
