-- 002_scenario_images.sql — Visual Hint Targeting Phase 1
-- Lưu metadata ảnh khoanh đỏ user upload làm hint cho action target.
-- Source of truth runtime = YAML (image_hint URL embedded). Bảng này là
-- index phụ phục vụ UI list, GC khi xóa revision, và audit upload.
--
-- Schema match convention thực tế của scenario_definitions/revisions/runs:
--   BIGINT(20) cho IDs, INT(11) cho boolean, TIMESTAMP DEFAULT utc_timestamp(),
--   utf8mb4_unicode_ci, không có FOREIGN KEY constraint (enforce app-layer).

CREATE TABLE IF NOT EXISTS `scenario_images` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,

  -- FK refs (no constraints, app-layer enforce)
  `revision_id` bigint(20) NOT NULL,           -- → scenario_revisions.id
  `scenario_id` bigint(20) NOT NULL,           -- → scenario_definitions.id (denorm)

  -- File metadata
  `filename` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `cdn_url` varchar(512) COLLATE utf8mb4_unicode_ci NOT NULL,
  `mime_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'image/png',
  `size_bytes` int(11) NOT NULL,
  `sha256` char(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,

  -- Step linking (advisory only — UI display, không sync với YAML khi step move)
  `step_index` int(11) DEFAULT NULL,
  `step_note` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,

  -- Audit
  `date_created` timestamp NULL DEFAULT utc_timestamp(),
  `date_updated` timestamp NULL DEFAULT utc_timestamp(),
  `created_by` bigint(20) DEFAULT NULL,
  `updated_by` bigint(20) DEFAULT NULL,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_image_rev_filename` (`revision_id`, `filename`),
  KEY `scenario_images_revision_id_idx` (`revision_id`),
  KEY `idx_img_scenario` (`scenario_id`),
  KEY `idx_img_sha` (`sha256`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─────────────────────────────────────────────────────────────────────────────
-- Rollback:
-- DROP TABLE IF EXISTS `scenario_images`;
-- ─────────────────────────────────────────────────────────────────────────────
