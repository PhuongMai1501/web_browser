"""
store/sqlite_scenario_repo.py — SQLite implementation của ScenarioRepository.

Phase 1 engine (file-based, 0 ops cost). Async qua aiosqlite.
Schema y hệt Postgres Phase 2 → migration chỉ đổi connection, không rewrite.

Lưu ý:
- SQLite không có jsonb → lưu JSON dưới dạng TEXT, serialize/deserialize trong repo.
- SQLite không có bool → dùng INTEGER 0/1.
- Datetime lưu ISO 8601 string (UTC).
- append_revision dùng BEGIN IMMEDIATE + SELECT MAX để tránh race version_no
  (UNIQUE constraint bảo vệ tầng cuối).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from store.scenario_repo import (
    DefinitionFilters,
    ScenarioDefinition,
    ScenarioRepository,
    ScenarioRevision,
    ScenarioRun,
)


_log = logging.getLogger(__name__)

_MIGRATION_FILE = Path(__file__).parent / "migrations" / "001_init.sql"


# ── Serialization helpers ────────────────────────────────────────────────────

def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def _json_dump(v) -> Optional[str]:
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, default=str)


def _json_load(s: Optional[str]):
    if s is None:
        return None
    return json.loads(s)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Row → model conversion ───────────────────────────────────────────────────

def _row_to_definition(row) -> ScenarioDefinition:
    return ScenarioDefinition(
        id=row["id"],
        name=row["name"],
        owner_id=row["owner_id"],
        org_id=row["org_id"],
        source_type=row["source_type"],
        visibility=row["visibility"],
        published_revision_id=row["published_revision_id"],
        is_archived=bool(row["is_archived"]),
        created_at=_str_to_dt(row["created_at"]),
        updated_at=_str_to_dt(row["updated_at"]),
    )


def _row_to_revision(row) -> ScenarioRevision:
    return ScenarioRevision(
        id=row["id"],
        scenario_id=row["scenario_id"],
        version_no=row["version_no"],
        raw_yaml=row["raw_yaml"],
        normalized_spec_json=_json_load(row["normalized_spec_json"]) or {},
        yaml_hash=row["yaml_hash"],
        parent_revision_id=row["parent_revision_id"],
        clone_source_revision_id=row["clone_source_revision_id"],
        schema_version=row["schema_version"],
        static_validation_status=row["static_validation_status"],
        static_validation_errors=_json_load(row["static_validation_errors"]),
        last_test_run_at=_str_to_dt(row["last_test_run_at"]),
        last_test_run_status=row["last_test_run_status"],
        last_test_run_id=row["last_test_run_id"],
        created_by=row["created_by"],
        created_at=_str_to_dt(row["created_at"]),
    )


def _row_to_run(row) -> ScenarioRun:
    return ScenarioRun(
        id=row["id"],
        scenario_id=row["scenario_id"],
        revision_id=row["revision_id"],
        session_id=row["session_id"],
        mode=row["mode"],
        started_by=row["started_by"],
        runtime_policy_snapshot=_json_load(row["runtime_policy_snapshot"]) or {},
        status=row["status"],
        created_at=_str_to_dt(row["created_at"]),
    )


# ── Repository ───────────────────────────────────────────────────────────────

class SqliteScenarioRepo(ScenarioRepository):
    """aiosqlite implementation. Single connection, serialized writes qua SQLite's
    internal lock. Đủ cho Phase 1 single-node.

    Usage:
        repo = SqliteScenarioRepo("scenarios.db")
        await repo.init()              # apply migrations (idempotent)
        ...
        await repo.close()
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        # Lock serialize các multi-statement transaction (BEGIN…COMMIT) giữa coroutines.
        # aiosqlite single-connection thread không tự atomic cross-coroutine.
        self._tx_lock = asyncio.Lock()

    async def init(self) -> None:
        """Open connection và apply migrations. Idempotent."""
        # isolation_level=None → autocommit mode, cho phép BEGIN/COMMIT explicit.
        # Mặc định "" (implicit transaction) sẽ conflict với BEGIN IMMEDIATE ở append_revision.
        self._conn = await aiosqlite.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        # Enable FK + WAL cho concurrent read khi có writer
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")

        sql = _MIGRATION_FILE.read_text(encoding="utf-8")
        await self._conn.executescript(sql)
        _log.info("SqliteScenarioRepo initialized: %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Repo chưa init. Gọi await repo.init() trước.")
        return self._conn

    # ── Definitions ──────────────────────────────────────────────────────────

    async def create_definition(self, defn: ScenarioDefinition) -> None:
        db = self._db()
        try:
            await db.execute(
                """
                INSERT INTO scenario_definitions
                    (id, name, owner_id, org_id, source_type, visibility,
                     published_revision_id, is_archived, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    defn.id, defn.name, defn.owner_id, defn.org_id,
                    defn.source_type, defn.visibility,
                    defn.published_revision_id,
                    1 if defn.is_archived else 0,
                    _dt_to_str(defn.created_at),
                    _dt_to_str(defn.updated_at),
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"Scenario id '{defn.id}' đã tồn tại") from e

    async def get_definition(
        self, scenario_id: str
    ) -> Optional[ScenarioDefinition]:
        db = self._db()
        async with db.execute(
            "SELECT * FROM scenario_definitions WHERE id = ?",
            (scenario_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_definition(row) if row else None

    async def list_definitions(
        self, filters: DefinitionFilters
    ) -> list[ScenarioDefinition]:
        db = self._db()
        clauses = []
        params: list = []

        if filters.owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(filters.owner_id)
        if filters.source_type is not None:
            clauses.append("source_type = ?")
            params.append(filters.source_type)
        if filters.is_archived is False:
            clauses.append("is_archived = 0")
        elif filters.is_archived is True:
            clauses.append("is_archived = 1")
        # is_archived=None → không filter

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([filters.limit, filters.offset])

        sql = f"""
            SELECT * FROM scenario_definitions
            {where}
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
        """
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_definition(r) for r in rows]

    async def archive_definition(self, scenario_id: str) -> None:
        db = self._db()
        await db.execute(
            """
            UPDATE scenario_definitions
            SET is_archived = 1, updated_at = ?
            WHERE id = ?
            """,
            (_dt_to_str(_now_utc()), scenario_id),
        )
        await db.commit()

    async def set_published_revision(
        self, scenario_id: str, rev_id: Optional[int]
    ) -> None:
        db = self._db()
        await db.execute(
            """
            UPDATE scenario_definitions
            SET published_revision_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (rev_id, _dt_to_str(_now_utc()), scenario_id),
        )
        await db.commit()

    async def count_builtin(self) -> int:
        db = self._db()
        async with db.execute(
            "SELECT COUNT(*) AS c FROM scenario_definitions WHERE source_type = 'builtin'"
        ) as cur:
            row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def count_by_owner(self, owner_id: str) -> int:
        db = self._db()
        async with db.execute(
            """
            SELECT COUNT(*) AS c FROM scenario_definitions
            WHERE owner_id = ? AND is_archived = 0
            """,
            (owner_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["c"]) if row else 0

    # ── Revisions ────────────────────────────────────────────────────────────

    async def append_revision(self, rev: ScenarioRevision) -> int:
        """Insert revision mới với version_no = max+1 cho scenario_id.

        Dùng asyncio.Lock để serialize BEGIN…COMMIT cross-coroutine
        (aiosqlite single-conn không atomic nếu không lock).
        UNIQUE(scenario_id, version_no) là defense-in-depth.
        """
        db = self._db()
        async with self._tx_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """
                    SELECT COALESCE(MAX(version_no), 0) AS mx
                    FROM scenario_revisions
                    WHERE scenario_id = ?
                    """,
                    (rev.scenario_id,),
                ) as cur:
                    row = await cur.fetchone()
                next_version = (row["mx"] if row else 0) + 1

                cur = await db.execute(
                    """
                    INSERT INTO scenario_revisions
                        (scenario_id, version_no, raw_yaml, normalized_spec_json,
                         yaml_hash, parent_revision_id, clone_source_revision_id,
                         schema_version, static_validation_status,
                         static_validation_errors, last_test_run_at,
                         last_test_run_status, last_test_run_id,
                         created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rev.scenario_id, next_version, rev.raw_yaml,
                        _json_dump(rev.normalized_spec_json),
                        rev.yaml_hash,
                        rev.parent_revision_id,
                        rev.clone_source_revision_id,
                        rev.schema_version,
                        rev.static_validation_status,
                        _json_dump(rev.static_validation_errors),
                        _dt_to_str(rev.last_test_run_at),
                        rev.last_test_run_status,
                        rev.last_test_run_id,
                        rev.created_by,
                        _dt_to_str(rev.created_at),
                    ),
                )
                new_id = cur.lastrowid
                await db.execute("COMMIT")
                return int(new_id)
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def get_revision(self, rev_id: int) -> Optional[ScenarioRevision]:
        db = self._db()
        async with db.execute(
            "SELECT * FROM scenario_revisions WHERE id = ?", (rev_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_revision(row) if row else None

    async def get_revision_by_version(
        self, scenario_id: str, version_no: int
    ) -> Optional[ScenarioRevision]:
        db = self._db()
        async with db.execute(
            """
            SELECT * FROM scenario_revisions
            WHERE scenario_id = ? AND version_no = ?
            """,
            (scenario_id, version_no),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_revision(row) if row else None

    async def list_revisions(
        self,
        scenario_id: str,
        limit: int = 20,
        before_id: Optional[int] = None,
    ) -> list[ScenarioRevision]:
        db = self._db()
        clauses = ["scenario_id = ?"]
        params: list = [scenario_id]
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        params.append(limit)

        sql = f"""
            SELECT * FROM scenario_revisions
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC
            LIMIT ?
        """
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_revision(r) for r in rows]

    async def get_latest_revision(
        self, scenario_id: str
    ) -> Optional[ScenarioRevision]:
        db = self._db()
        async with db.execute(
            """
            SELECT * FROM scenario_revisions
            WHERE scenario_id = ?
            ORDER BY version_no DESC
            LIMIT 1
            """,
            (scenario_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_revision(row) if row else None

    async def get_published_revision(
        self, scenario_id: str
    ) -> Optional[ScenarioRevision]:
        db = self._db()
        async with db.execute(
            """
            SELECT r.* FROM scenario_revisions r
            JOIN scenario_definitions d ON d.published_revision_id = r.id
            WHERE d.id = ?
            """,
            (scenario_id,),
        ) as cur:
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
        db = self._db()
        await db.execute(
            """
            UPDATE scenario_revisions
            SET last_test_run_status = ?,
                last_test_run_id = ?,
                last_test_run_at = ?
            WHERE id = ?
            """,
            (status, run_id, _dt_to_str(at), rev_id),
        )
        await db.commit()

    # ── Runs ─────────────────────────────────────────────────────────────────

    async def create_run(self, run: ScenarioRun) -> int:
        db = self._db()
        cur = await db.execute(
            """
            INSERT INTO scenario_runs
                (scenario_id, revision_id, session_id, mode, started_by,
                 runtime_policy_snapshot, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.scenario_id, run.revision_id, run.session_id,
                run.mode, run.started_by,
                _json_dump(run.runtime_policy_snapshot),
                run.status,
                _dt_to_str(run.created_at),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def update_run_status(self, run_id: int, status: str) -> None:
        if status not in ("running", "completed", "failed", "cancelled"):
            raise ValueError(f"Invalid run status: {status}")
        db = self._db()
        await db.execute(
            "UPDATE scenario_runs SET status = ? WHERE id = ?",
            (status, run_id),
        )
        await db.commit()

    async def get_run(self, run_id: int) -> Optional[ScenarioRun]:
        db = self._db()
        async with db.execute(
            "SELECT * FROM scenario_runs WHERE id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_run(row) if row else None
