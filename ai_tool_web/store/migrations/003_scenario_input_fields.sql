-- ═══════════════════════════════════════════════════════════════════════════════
-- 003_scenario_input_fields.sql — Input Fields Management (Phase 1 Sprint 1)
--
-- Mục đích: Tách inputs[] schema khỏi YAML (normalized_spec_json), lưu vào
-- bảng riêng. UI có 1 màn riêng quản lý field (add/edit/delete/reorder) qua
-- form. YAML section `inputs:` sẽ auto-sync từ DB (Sprint 2).
--
-- Source of truth (sau migration):
--   DB là source cho inputs[]. YAML inputs block tự gen từ DB qua yaml_sync
--   service. User edit qua UI tab "Inputs", KHÔNG edit text YAML trực tiếp
--   ở section auto-gen (lock readonly).
--
-- Schema design — xem docs/DB_REVIEW.md section 13.2 (verified 2026-05-11).
--
-- Dependencies:
--   - scenario_revisions.id phải tồn tại (FK app-layer enforce)
--   - scenario_definitions.id phải tồn tại (FK app-layer enforce)
--
-- Người chạy: hiepqn (qua MySQL Workbench)
-- Ngày: 2026-05-11
--
-- AN TOÀN:
--   - CHỈ CREATE TABLE mới, KHÔNG ALTER bảng cũ
--   - KHÔNG có DROP / TRUNCATE / DELETE
--   - Tham chiếu bảng cũ qua app layer (không add FK constraint)
--
-- HƯỚNG DẪN CHẠY:
--   1. BACKUP DB `changchatbot` trước (mysqldump hoặc snapshot)
--      Lý do: prod đã có ~30 scenarios + ~47 revisions, không còn empty
--   2. Mở MySQL Workbench → connect tới 172.28.8.11
--   3. Mở file này (File → Open SQL Script)
--   4. Chạy STEP 0 → đọc output, confirm bảng chưa tồn tại
--   5. Chạy STEP 1 (Cmd/Ctrl + Enter trên block CREATE TABLE)
--   6. Chạy STEP 2 (verify schema)
--   7. Sau đó chạy backfill: python ai_tool_web/scripts/backfill_input_fields.py
-- ═══════════════════════════════════════════════════════════════════════════════

USE changchatbot;


-- ═════════════════════════════════════════════════════════════════
-- STEP 0 — PRE-CHECK: confirm bảng `scenario_input_fields` chưa tồn tại
-- ═════════════════════════════════════════════════════════════════

SELECT
    COUNT(*) AS table_exists,
    (CASE WHEN COUNT(*) = 0 THEN '✓ Safe to CREATE' ELSE '⚠ Already exists — review before re-run' END) AS status
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'changchatbot'
  AND TABLE_NAME = 'scenario_input_fields';
-- Kỳ vọng: table_exists = 0, status = '✓ Safe to CREATE'
-- Nếu = 1 → DỪNG. CREATE TABLE IF NOT EXISTS sẽ skip nhưng vẫn cần xem schema có khớp expected không.


-- Pre-check thêm: confirm 4 bảng dependencies tồn tại
SELECT
    table_name,
    table_rows AS approx_rows
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'changchatbot'
  AND TABLE_NAME IN ('scenario_definitions', 'scenario_revisions', 'scenario_runs', 'scenario_images')
ORDER BY table_name;
-- Kỳ vọng: 4 dòng, table_name khớp với 4 bảng đã có.


-- ═════════════════════════════════════════════════════════════════
-- STEP 1 — CREATE TABLE scenario_input_fields
-- ═════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS `scenario_input_fields` (
  `id` BIGINT(20) NOT NULL AUTO_INCREMENT,

  -- FK refs (no constraints, app-layer enforce — giữ convention 4 bảng hiện tại)
  `revision_id` BIGINT(20) NOT NULL                       COMMENT '→ scenario_revisions.id',
  `scenario_id` BIGINT(20) NOT NULL                       COMMENT '→ scenario_definitions.id (denorm để query nhanh)',

  -- Field definition (mirror Pydantic ScenarioInputField + extension)
  `name` VARCHAR(64) COLLATE utf8mb4_unicode_ci NOT NULL  COMMENT 'Key trong context dict, vd "so_hieu", "username"',
  `display_label` VARCHAR(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Label hiển thị trên UI form',
  `field_type` VARCHAR(16) COLLATE utf8mb4_unicode_ci NOT NULL    COMMENT 'string | secret | number | bool',
  `is_required` INT(11) NOT NULL DEFAULT 0                COMMENT '0/1 — boolean theo convention codebase',
  `source` VARCHAR(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'context' COMMENT 'context (request body) | ask_user (runtime)',
  `default_value` TEXT COLLATE utf8mb4_unicode_ci DEFAULT NULL  COMMENT 'Giá trị mặc định lưu dạng text, app cast theo field_type',
  `description` TEXT COLLATE utf8mb4_unicode_ci DEFAULT NULL    COMMENT 'Mô tả — UI tooltip + LLM Super Agent đọc',

  -- Validation rules (JSON Schema 7 subset)
  `validation_rules` LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL
      CHECK (json_valid(`validation_rules`))
      COMMENT 'JSON: {minLength, maxLength, pattern, enum, minimum, maximum}',

  -- UI hints
  `placeholder` VARCHAR(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `help_text` TEXT COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `display_order` INT(11) NOT NULL DEFAULT 0              COMMENT 'Thứ tự render trên UI form (0-based)',

  -- Reserved cho Phase 2 (Super Agent integration) — schema sẵn, UI Phase 1 chưa expose
  `category` VARCHAR(32) COLLATE utf8mb4_unicode_ci DEFAULT 'user_input'
      COMMENT 'Phase 2: user_input | credential | config | system',
  `secret_ref` VARCHAR(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL
      COMMENT 'Phase 2: vault path template, vd "vault/{domain}/{user_id}/credentials.password"',
  `extraction_hint` TEXT COLLATE utf8mb4_unicode_ci DEFAULT NULL
      COMMENT 'Phase 2: hint cho LLM Super Agent extract field này từ user query',

  -- Reserved cho Phase 3 (templates / reuse cross-scenario)
  `template_id` BIGINT(20) DEFAULT NULL
      COMMENT 'Phase 3: → input_field_templates.id (bảng templates chưa tạo)',

  -- Audit (theo pattern 4 bảng hiện tại — created_by/updated_by BIGINT NULL ở Phase 1 mock auth)
  `date_created` TIMESTAMP NULL DEFAULT utc_timestamp(),
  `date_updated` TIMESTAMP NULL DEFAULT utc_timestamp(),
  `created_by` BIGINT(20) DEFAULT NULL,
  `updated_by` BIGINT(20) DEFAULT NULL,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_field_rev_name` (`revision_id`, `name`),
  KEY `idx_field_revision` (`revision_id`, `display_order`)        COMMENT 'Query pattern: list fields của 1 revision theo thứ tự',
  KEY `idx_field_scenario` (`scenario_id`)                          COMMENT 'Query pattern: list all fields của scenario qua revisions'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Phase 1 Sprint 1 (2026-05-11) — Input field schema tách từ YAML inputs[]';


-- ═════════════════════════════════════════════════════════════════
-- STEP 2 — VERIFY: xem schema sau khi tạo
-- ═════════════════════════════════════════════════════════════════

SHOW CREATE TABLE scenario_input_fields;


SELECT
    TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, COLUMN_COMMENT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'changchatbot'
  AND TABLE_NAME = 'scenario_input_fields'
ORDER BY ORDINAL_POSITION;


-- Confirm bảng mới rỗng (sẵn sàng cho backfill)
SELECT 'scenario_input_fields' AS tbl, COUNT(*) AS row_count FROM scenario_input_fields;
-- Kỳ vọng: row_count = 0
-- Bước tiếp: chạy `python ai_tool_web/scripts/backfill_input_fields.py --dry-run`
-- để preview backfill 47 revisions, sau đó bỏ --dry-run để apply thật.


-- ═════════════════════════════════════════════════════════════════
-- STEP 3 — ROLLBACK (chỉ chạy nếu cần revert)
-- Mở comment khi muốn xóa bảng (cả structure + data).
-- ═════════════════════════════════════════════════════════════════

-- DROP TABLE IF EXISTS scenario_input_fields;
-- SELECT 'Rollback complete — scenario_input_fields dropped' AS status;
