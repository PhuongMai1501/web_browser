-- 001_init_mysql.sql — MySQL schema cho Phase 1.5 user-scenario system.
-- Engine: MySQL 5.7+ (JSON support) hoặc MySQL 8.0+ (CHECK constraints enforced).
-- Mirror của 001_init.sql (SQLite). Khác biệt chính:
--   - BIGINT AUTO_INCREMENT thay vì INTEGER AUTOINCREMENT
--   - JSON native type thay vì TEXT JSON
--   - DATETIME(6) thay vì ISO string TEXT
--   - TINYINT(1) cho bool
--   - Drop partial index (MySQL không support WHERE clause trong CREATE INDEX)
--   - utf8mb4 cho Vietnamese content

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_definitions
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scenario_definitions (
    id                     VARCHAR(64)  NOT NULL,
    name                   VARCHAR(255) NOT NULL,
    owner_id               VARCHAR(64)  NULL,
    org_id                 VARCHAR(64)  NULL,
    source_type            VARCHAR(16)  NOT NULL,
    visibility             VARCHAR(16)  NOT NULL DEFAULT 'private',
    published_revision_id  BIGINT       NULL,
    is_archived            TINYINT(1)   NOT NULL DEFAULT 0,
    created_at             DATETIME(6)  NOT NULL,
    updated_at             DATETIME(6)  NOT NULL,

    PRIMARY KEY (id),

    CONSTRAINT chk_def_source_type
        CHECK (source_type IN ('builtin','user','cloned')),
    CONSTRAINT chk_def_visibility
        CHECK (visibility IN ('private','org','public')),

    INDEX idx_def_owner (owner_id, is_archived),
    INDEX idx_def_source (source_type, is_archived)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_revisions
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scenario_revisions (
    id                          BIGINT       NOT NULL AUTO_INCREMENT,
    scenario_id                 VARCHAR(64)  NOT NULL,
    version_no                  INT          NOT NULL,
    raw_yaml                    LONGTEXT     NOT NULL,
    normalized_spec_json        JSON         NOT NULL,
    yaml_hash                   CHAR(64)     NOT NULL,
    parent_revision_id          BIGINT       NULL,
    clone_source_revision_id    BIGINT       NULL,
    schema_version              INT          NOT NULL DEFAULT 1,
    static_validation_status    VARCHAR(16)  NOT NULL,
    static_validation_errors    JSON         NULL,
    last_test_run_at            DATETIME(6)  NULL,
    last_test_run_status        VARCHAR(16)  NULL,
    last_test_run_id            BIGINT       NULL,
    created_by                  VARCHAR(64)  NOT NULL,
    created_at                  DATETIME(6)  NOT NULL,

    PRIMARY KEY (id),

    CONSTRAINT chk_rev_val_status
        CHECK (static_validation_status IN ('pending','passed','failed')),

    CONSTRAINT fk_rev_scenario
        FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_rev_parent
        FOREIGN KEY (parent_revision_id) REFERENCES scenario_revisions(id),
    CONSTRAINT fk_rev_clone_source
        FOREIGN KEY (clone_source_revision_id) REFERENCES scenario_revisions(id),

    UNIQUE KEY uq_rev_version (scenario_id, version_no),

    INDEX idx_rev_scenario (scenario_id),
    INDEX idx_rev_hash (scenario_id, yaml_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_runs
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scenario_runs (
    id                         BIGINT       NOT NULL AUTO_INCREMENT,
    scenario_id                VARCHAR(64)  NOT NULL,
    revision_id                BIGINT       NOT NULL,
    session_id                 VARCHAR(64)  NOT NULL,
    mode                       VARCHAR(16)  NOT NULL,
    started_by                 VARCHAR(64)  NOT NULL,
    runtime_policy_snapshot    JSON         NOT NULL,
    status                     VARCHAR(16)  NOT NULL,
    created_at                 DATETIME(6)  NOT NULL,

    PRIMARY KEY (id),

    CONSTRAINT chk_run_mode
        CHECK (mode IN ('production','test')),
    CONSTRAINT chk_run_status
        CHECK (status IN ('running','completed','failed','cancelled')),

    CONSTRAINT fk_run_scenario
        FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id),
    CONSTRAINT fk_run_revision
        FOREIGN KEY (revision_id) REFERENCES scenario_revisions(id),

    INDEX idx_run_scenario (scenario_id),
    INDEX idx_run_session (session_id),
    INDEX idx_run_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
