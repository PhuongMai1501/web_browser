-- ═══════════════════════════════════════════════════════════════════════════════
-- APPLY_SCHEMA_CHANGES.sql
-- Apply Phase 1 schema changes cho 3 bảng scenario_*
-- Chạy trên: MariaDB 10.6 @ 172.28.8.11 / database changchatbot
-- Người chạy: hiepqn (qua MySQL Workbench)
-- Ngày: 2026-04-25
--
-- AN TOÀN:
--   - CHỈ touch 3 bảng: scenario_definitions, scenario_revisions, scenario_runs
--   - KHÔNG có DROP TABLE / TRUNCATE / DELETE
--   - KHÔNG động đến bất kỳ bảng nào khác trong changchatbot
--   - Tất cả 3 bảng hiện đang RỖNG → ALTER an toàn, không cần backup
--
-- HƯỚNG DẪN CHẠY:
--   1. Mở MySQL Workbench → connect tới 172.28.8.11
--   2. Mở file này (File → Open SQL Script)
--   3. Chạy lần lượt từng STEP (Cmd/Ctrl + Enter trên block)
--      hoặc chạy nguyên file (Execute Script ⚡)
--   4. Sau STEP 3, chạy STEP 4 (verify) để xem schema final
-- ═══════════════════════════════════════════════════════════════════════════════

USE changchatbot;


-- ═════════════════════════════════════════════════════════════════
-- STEP 0 — PRE-CHECK: confirm 3 bảng tồn tại và đang rỗng
-- (chạy trước, đọc kết quả, không gây thay đổi gì)
-- ═════════════════════════════════════════════════════════════════

SELECT 'scenario_definitions' AS tbl, COUNT(*) AS row_count FROM scenario_definitions
UNION ALL
SELECT 'scenario_revisions',          COUNT(*) FROM scenario_revisions
UNION ALL
SELECT 'scenario_runs',               COUNT(*) FROM scenario_runs;
-- Kỳ vọng: 3 dòng, row_count = 0 cho cả 3.
-- Nếu có row > 0 → DỪNG, hỏi lại trước khi tiếp tục.


-- ═════════════════════════════════════════════════════════════════
-- STEP 1 — TIER 1: FIX BUGS (must-fix, không có không chạy được code)
-- ═════════════════════════════════════════════════════════════════

-- 1.1 Fix typo column name: ` date_created` (có space đầu) → date_created
ALTER TABLE scenario_definitions
    CHANGE COLUMN ` date_created` date_created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE scenario_revisions
    CHANGE COLUMN ` date_created` date_created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;


-- 1.2 Fix sai data type trong scenario_revisions
ALTER TABLE scenario_revisions
    MODIFY COLUMN last_test_run_status     VARCHAR(16) NULL,
    MODIFY COLUMN last_test_run_at         DATETIME(6) NULL,
    MODIFY COLUMN static_validation_errors JSON        NULL;


-- 1.3 Thêm column thiếu: clone_source_revision_id (cho tính năng clone scenario)
ALTER TABLE scenario_revisions
    ADD COLUMN clone_source_revision_id BIGINT NULL AFTER parent_revision_id;


-- ═════════════════════════════════════════════════════════════════
-- STEP 2 — TIER 2: RECOMMENDED (giữ code gọn, perf tốt)
-- ═════════════════════════════════════════════════════════════════

-- 2.1 Đổi enum columns SMALLINT → VARCHAR(16) để readable + code đơn giản
ALTER TABLE scenario_definitions
    MODIFY COLUMN source_type VARCHAR(16) NOT NULL,
    MODIFY COLUMN visibility  VARCHAR(16) NOT NULL DEFAULT 'private';

ALTER TABLE scenario_revisions
    MODIFY COLUMN static_validation_status VARCHAR(16) NOT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN status VARCHAR(16) NOT NULL,
    MODIFY COLUMN mode   VARCHAR(16) NOT NULL;


-- 2.2 Đổi text fields lớn sang đúng type (LONGTEXT / JSON / CHAR)
ALTER TABLE scenario_revisions
    MODIFY COLUMN raw_yaml             LONGTEXT NOT NULL,
    MODIFY COLUMN normalized_spec_json JSON     NOT NULL,
    MODIFY COLUMN yaml_hash            CHAR(64) NOT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN runtime_policy_snapshot JSON NOT NULL;


-- 2.3 Đổi FK column type INT → BIGINT (match BIGINT PK)
ALTER TABLE scenario_definitions
    MODIFY COLUMN published_revision_id BIGINT NULL;

ALTER TABLE scenario_revisions
    MODIFY COLUMN parent_revision_id BIGINT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN revision_id BIGINT NOT NULL;


-- 2.4 session_id INT → VARCHAR(64) (UUID string từ /v1/sessions API)
ALTER TABLE scenario_runs
    MODIFY COLUMN session_id VARCHAR(64) NOT NULL;


-- 2.5 Thêm business key: code (cho URL, YAML id, UI dropdown)
ALTER TABLE scenario_definitions
    ADD COLUMN code VARCHAR(64) NOT NULL UNIQUE AFTER id;


-- 2.6 Thêm owner_code cho mock auth Phase 1 (string user id)
ALTER TABLE scenario_definitions
    ADD COLUMN owner_code VARCHAR(64) NULL AFTER owner_id;


-- 2.7 Nullable audit columns (Phase 1 chưa có int user id từ users table)
ALTER TABLE scenario_definitions
    MODIFY COLUMN created_by BIGINT NULL,
    MODIFY COLUMN updated_by BIGINT NULL;

ALTER TABLE scenario_revisions
    MODIFY COLUMN created_by BIGINT NULL,
    MODIFY COLUMN updated_by BIGINT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN started_by BIGINT NULL,
    MODIFY COLUMN created_by BIGINT NULL,
    MODIFY COLUMN updated_by BIGINT NULL;


-- 2.8 UNIQUE (scenario_id, version_no) — chống race condition khi save song song
ALTER TABLE scenario_revisions
    ADD UNIQUE KEY uq_rev_version (scenario_id, version_no);


-- 2.9 Indexes cho query pattern phổ biến
ALTER TABLE scenario_definitions
    ADD INDEX idx_def_owner  (owner_id, is_archived),
    ADD INDEX idx_def_source (source_type, is_archived);

ALTER TABLE scenario_revisions
    ADD INDEX idx_rev_hash (scenario_id, yaml_hash);

ALTER TABLE scenario_runs
    ADD INDEX idx_run_session (session_id);


-- ═════════════════════════════════════════════════════════════════
-- STEP 3 — TIER 3: OPTIONAL (Phase 2 cũng được, có thể skip lần này)
-- Giữ COMMENTED. Mở comment nếu muốn apply cùng lượt.
-- ═════════════════════════════════════════════════════════════════

-- Foreign keys (cascade khi xóa scenario)
-- ALTER TABLE scenario_revisions
--     ADD CONSTRAINT fk_rev_scenario
--         FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id) ON DELETE CASCADE;

-- ALTER TABLE scenario_runs
--     ADD CONSTRAINT fk_run_scenario
--         FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id),
--     ADD CONSTRAINT fk_run_revision
--         FOREIGN KEY (revision_id) REFERENCES scenario_revisions(id);

-- CHECK constraints (MariaDB 10.2.1+ enforce)
-- ALTER TABLE scenario_definitions
--     ADD CONSTRAINT chk_def_source     CHECK (source_type IN ('builtin','user','cloned')),
--     ADD CONSTRAINT chk_def_visibility CHECK (visibility  IN ('private','org','public'));

-- ALTER TABLE scenario_revisions
--     ADD CONSTRAINT chk_rev_status CHECK (static_validation_status IN ('pending','passed','failed'));

-- ALTER TABLE scenario_runs
--     ADD CONSTRAINT chk_run_mode   CHECK (mode   IN ('production','test')),
--     ADD CONSTRAINT chk_run_status CHECK (status IN ('running','completed','failed','cancelled'));


-- ═════════════════════════════════════════════════════════════════
-- STEP 4 — VERIFY: xem schema sau khi apply
-- (chạy block này, copy output gửi lại để mình double-check)
-- ═════════════════════════════════════════════════════════════════

SHOW CREATE TABLE scenario_definitions;
SHOW CREATE TABLE scenario_revisions;
SHOW CREATE TABLE scenario_runs;

-- Hoặc chi tiết hơn:
SELECT
    TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'changchatbot'
  AND TABLE_NAME IN ('scenario_definitions','scenario_revisions','scenario_runs')
ORDER BY TABLE_NAME, ORDINAL_POSITION;

-- Confirm 3 bảng vẫn rỗng (nếu Phase 1 chưa start):
SELECT 'scenario_definitions' AS tbl, COUNT(*) AS row_count FROM scenario_definitions
UNION ALL
SELECT 'scenario_revisions',          COUNT(*) FROM scenario_revisions
UNION ALL
SELECT 'scenario_runs',               COUNT(*) FROM scenario_runs;
