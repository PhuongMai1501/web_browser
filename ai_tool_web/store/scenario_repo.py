"""
store/scenario_repo.py — Abstract interface for scenario persistence.

Schema: 3 tables (scenario_definitions, scenario_revisions, scenario_runs).
Xem PLAN_USER_SCENARIO_CUSTOMIZATION.md §1.2 để biết chi tiết field.

Phase 1 implementation: SqliteScenarioRepo (via aiosqlite).
Phase 2 implementation: PostgresScenarioRepo.
Service layer chỉ depend vào interface này → swap engine không cần rewrite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Mapping, Optional

from pydantic import BaseModel, Field


# ── Data models (persistence layer) ──────────────────────────────────────────

class ScenarioDefinition(BaseModel):
    """Row in scenario_definitions table."""
    id: str                                      # "builtin_xxx" | "user_xxx_yyy"
    name: str
    owner_id: Optional[str] = None               # NULL cho builtin
    org_id: Optional[str] = None                 # Phase 2+
    source_type: str                             # 'builtin' | 'user' | 'cloned'
    visibility: str = "private"                  # 'private' | 'org' | 'public'
    published_revision_id: Optional[int] = None  # NULL = chưa publish
    is_archived: bool = False
    created_at: datetime
    updated_at: datetime


class ScenarioRevision(BaseModel):
    """Row in scenario_revisions table. Immutable except for last_test_run_* fields."""
    id: Optional[int] = None                     # None = not yet persisted, filled by append
    scenario_id: str
    version_no: int                              # tăng dần trong cùng scenario_id, unique
    raw_yaml: str
    normalized_spec_json: dict
    yaml_hash: str                               # sha256(raw_yaml)
    parent_revision_id: Optional[int] = None     # rev trước trong CÙNG scenario
    clone_source_revision_id: Optional[int] = None  # rev gốc khi clone từ scenario khác
    schema_version: int = 1
    static_validation_status: str                # 'pending' | 'passed' | 'failed'
    static_validation_errors: Optional[list] = None
    last_test_run_at: Optional[datetime] = None
    last_test_run_status: Optional[str] = None   # 'passed' | 'failed'
    last_test_run_id: Optional[int] = None
    created_by: str
    created_at: datetime


class ScenarioRun(BaseModel):
    """Row in scenario_runs table. Created khi session start, updated khi terminal."""
    id: Optional[int] = None                     # None = not yet persisted, filled by create_run
    scenario_id: str
    revision_id: int                             # pin cứng revision chạy
    session_id: str                              # link sang runtime session (Redis)
    mode: str                                    # 'production' | 'test'
    started_by: str
    runtime_policy_snapshot: dict                # allowed_domains + quota + hook whitelist tại start
    status: str                                  # 'running' | 'completed' | 'failed' | 'cancelled'
    created_at: datetime


class DefinitionFilters(BaseModel):
    """Filter params cho list_definitions. Mặc định ẩn archived."""
    owner_id: Optional[str] = None
    source_type: Optional[str] = None             # 'builtin' | 'user' | 'cloned'
    is_archived: Optional[bool] = False           # None = cả 2, False = chỉ active
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


# ── Repository interface ─────────────────────────────────────────────────────

class ScenarioRepository(ABC):
    """Persistence interface cho scenarios, revisions, runs.

    Tất cả methods async. Implementations phải:
    - Transactional cho `append_revision` (tránh race version_no).
    - Idempotent cho `archive_definition`, `set_published_revision`.
    - Thread-safe trong scope async event loop.
    """

    # ── Definitions ──────────────────────────────────────────────────────────

    @abstractmethod
    async def create_definition(self, defn: ScenarioDefinition) -> None:
        """Insert scenario definition mới. Raise ValueError nếu id đã tồn tại."""

    @abstractmethod
    async def get_definition(self, scenario_id: str) -> Optional[ScenarioDefinition]:
        """Trả definition hoặc None. Mặc định bao gồm cả archived (caller tự filter)."""

    @abstractmethod
    async def list_definitions(
        self, filters: DefinitionFilters
    ) -> list[ScenarioDefinition]:
        """List definitions match filters, sorted by updated_at DESC."""

    @abstractmethod
    async def archive_definition(self, scenario_id: str) -> None:
        """Soft delete: set is_archived=true. Idempotent."""

    @abstractmethod
    async def set_published_revision(
        self, scenario_id: str, rev_id: Optional[int]
    ) -> None:
        """Update published_revision_id. None = unpublish.
        Caller responsibility: verify rev_id thuộc scenario_id + status='passed'."""

    @abstractmethod
    async def count_builtin(self) -> int:
        """Count scenario_definitions WHERE source_type='builtin'.
        Dùng ở startup để quyết định có cần seed không (G2)."""

    @abstractmethod
    async def count_by_owner(self, owner_id: str) -> int:
        """Count active (non-archived) scenarios của 1 owner. Dùng cho quota (§3.4)."""

    # ── Revisions (append-only) ──────────────────────────────────────────────

    @abstractmethod
    async def append_revision(self, rev: ScenarioRevision) -> int:
        """Insert revision mới với version_no = max(version_no)+1 cho scenario_id.

        Transactional: 2 request đồng thời phải tuần tự, UNIQUE(scenario_id, version_no).
        rev.version_no input bị bỏ qua — repo tự set.
        Trả về id của revision vừa insert.
        """

    @abstractmethod
    async def get_revision(self, rev_id: int) -> Optional[ScenarioRevision]:
        """Get revision by internal id."""

    @abstractmethod
    async def get_revision_by_version(
        self, scenario_id: str, version_no: int
    ) -> Optional[ScenarioRevision]:
        """Get revision by (scenario_id, version_no) — stable reference."""

    @abstractmethod
    async def list_revisions(
        self,
        scenario_id: str,
        limit: int = 20,
        before_id: Optional[int] = None,
    ) -> list[ScenarioRevision]:
        """List revisions newest-first (by id DESC), paginate qua before_id cursor."""

    @abstractmethod
    async def get_latest_revision(
        self, scenario_id: str
    ) -> Optional[ScenarioRevision]:
        """Convenience: revision có version_no cao nhất."""

    @abstractmethod
    async def get_published_revision(
        self, scenario_id: str
    ) -> Optional[ScenarioRevision]:
        """Follow definition.published_revision_id → revision content.
        Return None nếu scenario chưa publish."""

    @abstractmethod
    async def update_revision_test_status(
        self,
        rev_id: int,
        status: str,                   # 'passed' | 'failed'
        run_id: int,
        at: datetime,
    ) -> None:
        """Update last_test_run_* fields. Đây là fields mutable DUY NHẤT trên revision."""

    # ── Runs ─────────────────────────────────────────────────────────────────

    @abstractmethod
    async def create_run(self, run: ScenarioRun) -> int:
        """Insert run, trả id. Gọi từ API khi session enqueue."""

    @abstractmethod
    async def update_run_status(self, run_id: int, status: str) -> None:
        """Gọi từ worker khi session done/failed/cancelled (G6).
        status phải là terminal value."""

    @abstractmethod
    async def get_run(self, run_id: int) -> Optional[ScenarioRun]:
        """Get run by id."""

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @abstractmethod
    async def close(self) -> None:
        """Close underlying connections. Gọi khi app shutdown."""
