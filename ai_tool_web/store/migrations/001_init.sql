-- 001_init.sql — Initial schema for scenario customization (Phase 1).
-- Engine: SQLite (Phase 1). Compatible SQL for Postgres (Phase 2) — chỉ khác type aliases.
-- Xem PLAN_USER_SCENARIO_CUSTOMIZATION.md §1.2.

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_definitions — metadata 1 scenario (không chứa spec content)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scenario_definitions (
    id                     VARCHAR(64) PRIMARY KEY,
    name                   VARCHAR NOT NULL,
    owner_id               VARCHAR(64),                                   -- NULL cho builtin
    org_id                 VARCHAR(64),                                   -- Phase 2+
    source_type            VARCHAR(16) NOT NULL
                           CHECK (source_type IN ('builtin','user','cloned')),
    visibility             VARCHAR(16) NOT NULL DEFAULT 'private'
                           CHECK (visibility IN ('private','org','public')),
    published_revision_id  INTEGER,                                       -- FK set sau khi rev tồn tại
    is_archived            INTEGER NOT NULL DEFAULT 0,                    -- SQLite bool
    created_at             TEXT NOT NULL,                                 -- ISO 8601 string
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_def_owner
    ON scenario_definitions(owner_id)
    WHERE is_archived = 0;

CREATE INDEX IF NOT EXISTS idx_def_source
    ON scenario_definitions(source_type)
    WHERE is_archived = 0;

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_revisions — mỗi save = 1 row immutable
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scenario_revisions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id                 VARCHAR(64) NOT NULL,
    version_no                  INTEGER NOT NULL,                         -- tăng dần trong cùng scenario
    raw_yaml                    TEXT NOT NULL,
    normalized_spec_json        TEXT NOT NULL,                            -- JSON serialized
    yaml_hash                   CHAR(64) NOT NULL,                        -- sha256(raw_yaml)
    parent_revision_id          INTEGER,                                   -- rev trước trong CÙNG scenario
    clone_source_revision_id    INTEGER,                                   -- rev gốc khi clone từ scenario khác
    schema_version              INTEGER NOT NULL DEFAULT 1,
    static_validation_status    VARCHAR(16) NOT NULL
                                CHECK (static_validation_status IN ('pending','passed','failed')),
    static_validation_errors    TEXT,                                     -- JSON serialized hoặc NULL
    last_test_run_at            TEXT,                                     -- ISO 8601 hoặc NULL
    last_test_run_status        VARCHAR(16),
    last_test_run_id            INTEGER,
    created_by                  VARCHAR(64) NOT NULL,
    created_at                  TEXT NOT NULL,

    FOREIGN KEY (scenario_id)              REFERENCES scenario_definitions(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_revision_id)       REFERENCES scenario_revisions(id),
    FOREIGN KEY (clone_source_revision_id) REFERENCES scenario_revisions(id),
    UNIQUE (scenario_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_rev_scenario
    ON scenario_revisions(scenario_id);

CREATE INDEX IF NOT EXISTS idx_rev_hash
    ON scenario_revisions(scenario_id, yaml_hash);                        -- dùng detect no-op save

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_runs — mỗi session chạy ghi 1 row
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scenario_runs (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id                VARCHAR(64) NOT NULL,
    revision_id                INTEGER NOT NULL,                          -- pin cứng rev đang chạy
    session_id                 VARCHAR NOT NULL,                          -- link sang runtime session
    mode                       VARCHAR(16) NOT NULL
                               CHECK (mode IN ('production','test')),
    started_by                 VARCHAR(64) NOT NULL,
    runtime_policy_snapshot    TEXT NOT NULL,                             -- JSON
    status                     VARCHAR(16) NOT NULL
                               CHECK (status IN ('running','completed','failed','cancelled')),
    created_at                 TEXT NOT NULL,

    FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id),
    FOREIGN KEY (revision_id) REFERENCES scenario_revisions(id)
);

CREATE INDEX IF NOT EXISTS idx_run_scenario   ON scenario_runs(scenario_id);
CREATE INDEX IF NOT EXISTS idx_run_session    ON scenario_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_run_status_run ON scenario_runs(status) WHERE status = 'running';
