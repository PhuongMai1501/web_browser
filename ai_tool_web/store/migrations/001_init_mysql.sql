-- 001_init_mysql.sql — MySQL schema cho user-scenario system.
-- Engine: MariaDB 10.x / MySQL 8.0+ (utc_timestamp() function).
--
-- File này MIRROR schema thực tế đang chạy production (DB `changchatbot`).
-- Đã rewrite 2026-05-05 để khớp với output `SHOW CREATE TABLE` của 3 bảng:
--   scenario_definitions, scenario_revisions, scenario_runs.
--
-- Khác biệt so với 001_init.sql (SQLite):
--   - BIGINT(20) AUTO_INCREMENT cho id (không dùng VARCHAR id như SQLite)
--   - code VARCHAR(64) UNIQUE — ID user-facing tách khỏi PK
--   - INT(11) cho boolean (is_archived) — không dùng TINYINT(1)
--   - TIMESTAMP DEFAULT utc_timestamp() — không dùng DATETIME(6)
--   - JSON columns dùng COLLATE utf8mb4_bin + CHECK json_valid()
--   - KHÔNG có FOREIGN KEY constraints — enforce ở app layer
--   - KHÔNG có CHECK enum constraints — enforce ở Pydantic layer
--   - utf8mb4 cho Vietnamese content

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_definitions
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `scenario_definitions` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `code` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `owner_id` int(11) DEFAULT NULL,
  `owner_code` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `org_id` int(11) DEFAULT NULL,
  `source_type` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL,
  `visibility` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'private',
  `published_revision_id` bigint(20) DEFAULT NULL,
  `is_archived` int(11) DEFAULT 0,
  `date_created` timestamp NULL DEFAULT utc_timestamp(),
  `date_updated` timestamp NULL DEFAULT utc_timestamp(),
  `created_by` bigint(20) DEFAULT NULL,
  `updated_by` bigint(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`),
  KEY `idx_def_owner` (`owner_id`, `is_archived`),
  KEY `idx_def_source` (`source_type`, `is_archived`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_revisions
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `scenario_revisions` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `scenario_id` bigint(20) NOT NULL,
  `version_no` int(11) NOT NULL,
  `raw_yaml` longtext COLLATE utf8mb4_unicode_ci NOT NULL,
  `normalized_spec_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL
      CHECK (json_valid(`normalized_spec_json`)),
  `yaml_hash` char(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `parent_revision_id` bigint(20) DEFAULT NULL,
  `clone_source_revision_id` bigint(20) DEFAULT NULL,
  `schema_version` int(11) DEFAULT 2,
  `static_validation_status` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'pending',
  `static_validation_errors` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL
      CHECK (json_valid(`static_validation_errors`)),
  `last_test_run_at` timestamp NULL DEFAULT utc_timestamp(),
  `last_test_run_status` varchar(16) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `last_test_run_id` bigint(20) DEFAULT NULL,
  `date_created` timestamp NULL DEFAULT utc_timestamp(),
  `date_updated` timestamp NULL DEFAULT utc_timestamp(),
  `created_by` bigint(20) DEFAULT NULL,
  `updated_by` bigint(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rev_version` (`scenario_id`, `version_no`),
  KEY `scenario_revisions_scenario_id_idx` (`scenario_id`),
  KEY `idx_rev_hash` (`scenario_id`, `yaml_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────────────────────────────────────────
-- scenario_runs
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `scenario_runs` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `scenario_id` bigint(20) NOT NULL,
  `revision_id` bigint(20) NOT NULL,
  `session_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `mode` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'production',
  `started_by` bigint(20) DEFAULT NULL,
  `runtime_policy_snapshot` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL
      CHECK (json_valid(`runtime_policy_snapshot`)),
  `status` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'running',
  `date_created` timestamp NULL DEFAULT utc_timestamp(),
  `date_updated` timestamp NULL DEFAULT utc_timestamp(),
  `created_by` bigint(20) DEFAULT NULL,
  `updated_by` bigint(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `scenario_runs_scenario_id_idx` (`scenario_id`),
  KEY `idx_run_session` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
