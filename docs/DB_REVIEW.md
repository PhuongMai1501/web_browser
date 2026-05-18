# DB Review — Scenario Storage (MariaDB 10.6 @ 172.28.8.11 / changchatbot)

**Last reviewed**: 2026-05-11
**Reviewer**: hiepqn
**Scope**: Đánh giá DB schema hiện tại có đủ phục vụ tích hợp Super Agent không + roadmap nâng cấp.

**Verified 2026-05-11**: Schema dưới đây đối chiếu với `SHOW CREATE TABLE` thật từ prod DB (dump tại `dev/data_base/db_scenario_*.txt`). 100% khớp.

**Data state (2026-05-11)**:
- `scenario_definitions`: AUTO_INCREMENT=31 (~30 scenarios)
- `scenario_revisions`: AUTO_INCREMENT=48 (~47 revisions)
- `scenario_runs`: AUTO_INCREMENT=3 (~2 runs)
- `scenario_images`: AUTO_INCREMENT=4 (~3 images)

→ Backfill `scenario_input_fields` (Phase 1 section 13) phải xử lý ~47 revisions, idempotent + dry-run flag bắt buộc.
→ Mọi migration tới đây phải BACKUP data trước, KHÔNG còn an toàn như giai đoạn bảng rỗng (APPLY_SCHEMA_CHANGES.sql 2026-04-25 là historical, KHÔNG chạy lại được).

---

## 1. Tổng quan 4 bảng hiện có

```
┌─────────────────────────────┐         ┌──────────────────────────────┐
│  scenario_definitions       │ 1────n  │  scenario_revisions          │
│  (metadata scenario)        │◀────────│  (version YAML per scenario) │
│  PK: id                     │         │  PK: id                       │
│  UNIQUE: code               │         │  FK: scenario_id              │
│  published_revision_id ─────┼────────▶│  UNIQUE: scenario_id+version  │
└─────────────────────────────┘         └──────────────────────────────┘
              ▲                                       ▲
              │                                       │
              │ 1────n                          1────n│
              │                                       │
┌─────────────┴───────────────┐         ┌─────────────┴────────────────┐
│  scenario_runs              │         │  scenario_images             │
│  (audit log mỗi lần chạy)   │         │  (Visual Hint Phase 1)       │
│  PK: id                     │         │  PK: id                       │
│  FK: scenario_id, rev_id    │         │  FK: revision_id, scenario_id │
│  session_id (UUID string)   │         │  UNIQUE: revision_id+filename │
└─────────────────────────────┘         └──────────────────────────────┘
```

**Note quan trọng**: KHÔNG có `FOREIGN KEY` constraint nào — enforce ở app layer (Pydantic + service).
KHÔNG có `CHECK` enum — enforce ở Pydantic.

---

## 2. Bảng `scenario_definitions` — Metadata scenario

**Mục đích**: 1 hàng = 1 "kịch bản" về mặt logic (vd "Kiểm tra version QCVN"). Tách biệt với các bản YAML version (lưu ở `scenario_revisions`).

### Schema (sau APPLY_SCHEMA_CHANGES.sql)

| Column | Type | Null | Default | Mục đích | Ghi chú |
|--------|------|------|---------|----------|---------|
| `id` | BIGINT AUTO_INCREMENT | NO | — | PK | |
| `code` | VARCHAR(64) | NO | — | **ID user-facing**, dùng trong URL/API/YAML | UNIQUE. VD: `user_hiepqn_check_law_version_qcvn` |
| `name` | VARCHAR(255) | YES | NULL | Tên hiển thị | VD: "Kiểm tra version & Tải QCVN/TCVN" |
| `owner_id` | INT(11) | YES | NULL | User ID chủ sở hữu | Liên kết bảng users (chưa có) |
| `owner_code` | VARCHAR(64) | YES | NULL | User code (mock auth Phase 1) | String backup khi chưa có user table |
| `org_id` | INT(11) | YES | NULL | Tổ chức sở hữu | Multi-tenant support |
| `source_type` | VARCHAR(16) | NO | — | Loại scenario | Values: `builtin` / `user` / `cloned` |
| `visibility` | VARCHAR(16) | NO | `'private'` | Phạm vi truy cập | Values: `private` / `org` / `public` |
| `published_revision_id` | BIGINT | YES | NULL | Trỏ đến revision đang active | → `scenario_revisions.id` |
| `is_archived` | INT(11) | YES | 0 | Soft delete | 0=active, 1=archived |
| `date_created` | TIMESTAMP | YES | utc_timestamp() | | |
| `date_updated` | TIMESTAMP | YES | utc_timestamp() | | |
| `created_by` | BIGINT | YES | NULL | User tạo | |
| `updated_by` | BIGINT | YES | NULL | User update lần cuối | |

### Indexes
- PK `id`
- UNIQUE `code`
- `idx_def_owner` (owner_id, is_archived) — list scenario của 1 user
- `idx_def_source` (source_type, is_archived) — phân biệt builtin vs user-created

---

## 3. Bảng `scenario_revisions` — Version YAML

**Mục đích**: Mỗi lần user sửa YAML → tạo 1 revision mới. Active revision được trỏ qua `scenario_definitions.published_revision_id`. Không sửa in-place để giữ history.

### Schema

| Column | Type | Null | Default | Mục đích | Ghi chú |
|--------|------|------|---------|----------|---------|
| `id` | BIGINT AUTO_INCREMENT | NO | — | PK | |
| `scenario_id` | BIGINT | NO | — | FK → `scenario_definitions.id` | |
| `version_no` | INT(11) | NO | — | Số phiên bản (1, 2, 3...) | UNIQUE với scenario_id |
| `raw_yaml` | LONGTEXT | NO | — | YAML gốc user nhập | Source of truth |
| `normalized_spec_json` | JSON | NO | — | `ScenarioSpec` đã parse từ YAML | Sync vào Redis dùng runtime |
| `yaml_hash` | CHAR(64) | NO | — | SHA-256 của `raw_yaml` | Chống duplicate revision |
| `parent_revision_id` | BIGINT | YES | NULL | Revision trước đó | Tracking edit history |
| `clone_source_revision_id` | BIGINT | YES | NULL | Source nếu clone từ scenario khác | |
| `schema_version` | INT(11) | YES | 2 | YAML schema version (v1/v2) | |
| `static_validation_status` | VARCHAR(16) | NO | `'pending'` | Kết quả validate lúc save | `pending` / `passed` / `failed` |
| `static_validation_errors` | JSON | YES | NULL | Chi tiết lỗi nếu failed | |
| `last_test_run_at` | DATETIME(6) | YES | NULL | Lần test cuối | Để admin biết rev có chạy được không |
| `last_test_run_status` | VARCHAR(16) | YES | NULL | Status lần test cuối | |
| `last_test_run_id` | BIGINT | YES | NULL | → `scenario_runs.id` | |
| `date_created`, `date_updated`, `created_by`, `updated_by` | Audit | | | | |

### Indexes
- PK `id`
- UNIQUE `uq_rev_version` (scenario_id, version_no)
- `scenario_revisions_scenario_id_idx` (scenario_id)
- `idx_rev_hash` (scenario_id, yaml_hash)

### Cấu trúc `normalized_spec_json` (Pydantic `ScenarioSpec`)

```json
{
  "id": "user_hiepqn_check_law_version_qcvn",
  "display_name": "Kiểm tra version & Tải QCVN/TCVN",
  "description": "Tải tiêu chuẩn kỹ thuật QCVN/TCVN",
  "enabled": true,
  "builtin": false,
  "version": 1,
  "mode": "flow",                    // "flow" | "agent" | "hybrid"
  "start_url": "https://thuvienphapluat.vn/",
  "goal": "Kiểm tra version & Tải QCVN/TCVN",
  "max_steps_default": 20,
  "allowed_domains": ["thuvienphapluat.vn"],
  "inputs": [                        // ← KEY field cho Super Agent
    {
      "name": "username",
      "type": "string",              // "string" | "secret" | "number" | "bool"
      "required": true,
      "source": "context",           // "context" | "ask_user"
      "default": null,
      "description": "Tên đăng nhập"
    }
    // ... so_hieu, password, title_hint
  ],
  "steps": [...],                    // flow_runner đọc
  "success": {...},                  // optional
  "failure": {...},                  // optional
  "context_schema": {},              // legacy v1
  "system_prompt_extra": "",         // dùng mode=agent
  "hooks": {"pre_check": null, "post_step": null, "final_capture": null}
}
```

---

## 4. Bảng `scenario_runs` — Audit log

**Mục đích**: Mỗi lần `POST /v1/sessions` → ghi 1 hàng. Track scenario nào, revision nào, session_id, ai trigger.

### Schema

| Column | Type | Null | Default | Mục đích |
|--------|------|------|---------|----------|
| `id` | BIGINT AUTO_INCREMENT | NO | — | PK |
| `scenario_id` | BIGINT | NO | — | FK → `scenario_definitions.id` |
| `revision_id` | BIGINT | NO | — | FK → `scenario_revisions.id` (rev nào chạy) |
| `session_id` | VARCHAR(64) | NO | — | UUID từ API `/v1/sessions` |
| `mode` | VARCHAR(16) | NO | `'production'` | `production` / `test` / `debug` |
| `started_by` | BIGINT | YES | NULL | User trigger |
| `runtime_policy_snapshot` | JSON | NO | — | Snapshot config lúc run (max_steps, context, callback_url...) |
| `status` | VARCHAR(16) | NO | `'running'` | `running` / `completed` / `failed` / `cancelled` |
| `date_created`, `date_updated`, `created_by`, `updated_by` | Audit | | | |

### Indexes
- PK `id`
- `scenario_runs_scenario_id_idx` (scenario_id)
- `idx_run_session` (session_id) — query "session này dùng scenario gì"

### ⚠️ Vấn đề tiềm ẩn (chưa fix)
- KHÔNG có cột `finished_at`, `duration_ms` → muốn thống kê SLA phải JOIN với Redis session_store
- KHÔNG có cột `error_msg`, `error_code` → debug fail phải đọc log
- KHÔNG có cột `result_summary` (JSON) → kết quả callback không lưu lâu dài

---

## 5. Bảng `scenario_images` — Visual Hint (Phase 1 mới)

**Mục đích**: Index ảnh khoanh đỏ user upload làm hint cho action target. Source of truth runtime = YAML (image URL embedded). Bảng này phục vụ UI list + GC khi xóa revision.

### Schema

| Column | Type | Null | Default | Mục đích |
|--------|------|------|---------|----------|
| `id` | BIGINT AUTO_INCREMENT | NO | — | PK |
| `revision_id` | BIGINT | NO | — | → `scenario_revisions.id` |
| `scenario_id` | BIGINT | NO | — | → `scenario_definitions.id` (denorm) |
| `filename` | VARCHAR(255) | NO | — | Tên file ảnh |
| `cdn_url` | VARCHAR(512) | NO | — | URL MinIO |
| `mime_type` | VARCHAR(32) | NO | `'image/png'` | |
| `size_bytes` | INT(11) | NO | — | |
| `sha256` | CHAR(64) | YES | NULL | Dedup |
| `step_index` | INT(11) | YES | NULL | Step nào dùng ảnh (advisory) |
| `step_note` | VARCHAR(255) | YES | NULL | Note cho UI |
| `date_created`, `date_updated`, `created_by`, `updated_by` | Audit | | | |

### Indexes
- PK `id`
- UNIQUE `uq_image_rev_filename` (revision_id, filename)
- `scenario_images_revision_id_idx` (revision_id)
- `idx_img_scenario` (scenario_id)
- `idx_img_sha` (sha256)

---

## 6. Đánh giá: Đủ cho Super Agent integration chưa?

### ✅ ĐỦ cho V0 PoC

| Need của Super Agent | Có sẵn |
|----------------------|--------|
| Scenario ID stable để tham chiếu | `scenario_definitions.code` UNIQUE |
| Display name | `scenario_definitions.name` + `spec.display_name` |
| Mô tả ngắn (LLM router đọc) | `spec.description` |
| **Inputs schema** (biết phải gửi gì) | `spec.inputs[]` đầy đủ với `{name, type, required, source, description}` |
| Phân biệt input động vs hỏi runtime | `inputs[].source` (`context` vs `ask_user`) |
| Type info | `inputs[].type` (string/secret/number/bool) |
| Required validation | `inputs[].required` |
| Versioning | `scenario_revisions` + `published_revision_id` |
| Audit run history | `scenario_runs` (scenario, rev, session, user, mode, status) |
| Scope/visibility | `owner_id`, `org_id`, `visibility` |

### ⚠️ THIẾU cho V1 production

#### 6.1 Mapping intent → scenario (LLM router)

**Vấn đề**: Hiện chỉ có `name` + `description` free-text → LLM Super Agent phải match heuristic, dễ nhầm khi nhiều scenarios.

**Đề xuất thêm columns vào `scenario_definitions`**:

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN category VARCHAR(64) DEFAULT 'general' AFTER name,
  ADD COLUMN intent_keywords JSON DEFAULT NULL AFTER category,
  ADD COLUMN example_queries JSON DEFAULT NULL AFTER intent_keywords,
  ADD INDEX idx_def_category (category);
```

Ví dụ data:
```json
{
  "category": "law_document_download",
  "intent_keywords": ["QCVN", "TCVN", "tải tiêu chuẩn", "Quy chuẩn kỹ thuật", "BNNMT"],
  "example_queries": [
    "tải QCVN 10:2025",
    "lấy về văn bản kỹ thuật BNNMT",
    "QCVN 86 bản đồ địa hình"
  ]
}
```

#### 6.2 Phân loại nguồn input cho từng field

**Vấn đề**: Hiện `inputs[].source` chỉ có `context` / `ask_user`. Super Agent không tự biết:
- field nào lookup từ secret store (vault)
- field nào extract từ user message qua LLM
- field nào từ user profile/config

**Đề xuất**: mở rộng `InputField` Pydantic + sync vào `normalized_spec_json`:

```python
class InputField(BaseModel):
    name: str
    type: Literal["string", "secret", "number", "bool"]
    required: bool = False
    source: Literal["context", "ask_user"]
    # NEW V1:
    category: Literal["user_input", "credential", "config", "system"] = "user_input"
    secret_ref: Optional[str] = None        # vd "vault/thuvienphapluat/{user_id}/credentials.password"
    extraction_hint: Optional[str] = None    # vd "Số hiệu QCVN/TCVN từ user query"
    default: Optional[Any] = None
    description: str = ""
```

**Không cần ALTER TABLE** — vì `normalized_spec_json` là JSON, chỉ cần update Pydantic + re-publish revision.

#### 6.3 Capabilities flags

**Vấn đề**: Super Agent không biết scenario này download file không, cần login không, có CAPTCHA không... trước khi gọi.

**Đề xuất thêm column `capabilities` JSON vào `scenario_definitions`**:

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN capabilities JSON DEFAULT NULL AFTER visibility;
```

Ví dụ data:
```json
["login_required", "downloads_files", "uploads_to_cdn", "long_running"]
```

#### 6.4 Output schema (callback shape)

**Vấn đề**: Hiện callback trả `result` free-form. Super Agent phải parse heuristic hoặc hardcode per-scenario.

**Đề xuất thêm column `output_schema` JSON vào `scenario_definitions`**:

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN output_schema JSON DEFAULT NULL AFTER capabilities;
```

Ví dụ data:
```json
{
  "type": "object",
  "properties": {
    "downloaded_filename": {"type": "string"},
    "downloaded_cdn_url": {"type": "string", "format": "uri"}
  },
  "required": ["downloaded_filename", "downloaded_cdn_url"]
}
```

#### 6.5 SLA / Cost hint

**Vấn đề**: Super Agent cần báo user "sẽ mất ~3 phút" trước khi gọi.

**Đề xuất**:

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN expected_duration_seconds INT DEFAULT NULL AFTER capabilities,
  ADD COLUMN cost_credits INT DEFAULT NULL;
```

#### 6.6 Rate limit / Concurrent quota

**Vấn đề**: Hiện chỉ có `job_queue.is_over_capacity` global. Super Agent spam 1 scenario tốn tài nguyên.

**Đề xuất**:

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN rate_limit_per_user_per_hour INT DEFAULT NULL,
  ADD COLUMN concurrent_max INT DEFAULT 5;
```

#### 6.7 Authorization scopes (multi-tenant)

**Vấn đề**: Hiện chỉ có JWT user-level. Scenario nào Super Agent được phép gọi?

**Đề xuất**:

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN required_scopes JSON DEFAULT NULL;
```

Ví dụ: `["scenario:execute:law", "data:download:public"]`.

#### 6.8 Run table enrichment

**Vấn đề**: `scenario_runs` thiếu các cột để debug + thống kê.

**Đề xuất**:

```sql
ALTER TABLE scenario_runs
  ADD COLUMN finished_at TIMESTAMP NULL AFTER status,
  ADD COLUMN duration_ms INT NULL AFTER finished_at,
  ADD COLUMN error_code VARCHAR(64) NULL,
  ADD COLUMN error_msg TEXT NULL,
  ADD COLUMN result_summary JSON NULL,
  ADD COLUMN triggered_by VARCHAR(32) DEFAULT 'human',  -- 'human' / 'super_agent' / 'cron' / 'api'
  ADD INDEX idx_run_status (status, finished_at);
```

---

## 7. Roadmap nâng cấp DB — 3 phase

### Phase 1 — Discovery & routing (làm trước khi tích hợp Super Agent thật)

Mục tiêu: Super Agent LLM router chọn đúng scenario, biết SLA.

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN category VARCHAR(64) DEFAULT 'general' AFTER name,
  ADD COLUMN intent_keywords JSON DEFAULT NULL AFTER category,
  ADD COLUMN example_queries JSON DEFAULT NULL AFTER intent_keywords,
  ADD COLUMN expected_duration_seconds INT DEFAULT NULL,
  ADD INDEX idx_def_category (category);
```

**Effort**: 1 buổi (migration + UI admin form 3 ô text + LLM router prompt update).

### Phase 2 — Contract & quota (sau khi có >5 scenarios live)

Mục tiêu: Super Agent biết I/O contract, control quota.

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN capabilities JSON DEFAULT NULL,
  ADD COLUMN output_schema JSON DEFAULT NULL,
  ADD COLUMN rate_limit_per_user_per_hour INT DEFAULT NULL,
  ADD COLUMN concurrent_max INT DEFAULT 5;

ALTER TABLE scenario_runs
  ADD COLUMN finished_at TIMESTAMP NULL,
  ADD COLUMN duration_ms INT NULL,
  ADD COLUMN error_code VARCHAR(64) NULL,
  ADD COLUMN error_msg TEXT NULL,
  ADD COLUMN result_summary JSON NULL,
  ADD COLUMN triggered_by VARCHAR(32) DEFAULT 'human',
  ADD INDEX idx_run_status (status, finished_at);
```

**Effort**: 1-2 ngày (migration + Pydantic update + UI admin + analytics dashboard).

### Phase 3 — Marketplace & security (nếu mở multi-tenant)

Mục tiêu: Bảo mật phân quyền, billing, embedding search.

```sql
ALTER TABLE scenario_definitions
  ADD COLUMN required_scopes JSON DEFAULT NULL,
  ADD COLUMN cost_credits INT DEFAULT NULL,
  ADD COLUMN description_embedding BLOB DEFAULT NULL;  -- hoặc bảng riêng

CREATE TABLE scenario_health_metrics (
  scenario_id BIGINT PRIMARY KEY,
  total_runs INT DEFAULT 0,
  success_count INT DEFAULT 0,
  failure_count INT DEFAULT 0,
  last_run_at TIMESTAMP NULL,
  success_rate_30d DECIMAL(5,2) DEFAULT NULL,
  avg_duration_ms_30d INT DEFAULT NULL
);
```

**Effort**: 3-5 ngày + tích hợp vector DB hoặc dùng MariaDB Vector (10.6 chưa có, cần upgrade).

---

## 8. Sample queries Super Agent sẽ dùng

### 8.1 List scenarios available cho user

```sql
SELECT
  d.code,
  d.name,
  JSON_EXTRACT(r.normalized_spec_json, '$.description') AS description,
  JSON_EXTRACT(r.normalized_spec_json, '$.inputs') AS inputs_schema,
  -- After Phase 1:
  d.category,
  d.intent_keywords,
  d.example_queries,
  d.expected_duration_seconds
FROM scenario_definitions d
JOIN scenario_revisions r ON d.published_revision_id = r.id
WHERE d.is_archived = 0
  AND JSON_EXTRACT(r.normalized_spec_json, '$.enabled') = true
  AND (
    d.visibility = 'public'
    OR (d.visibility = 'org' AND d.org_id = :user_org_id)
    OR (d.visibility = 'private' AND d.owner_id = :user_id)
  )
ORDER BY d.name;
```

### 8.2 Get input schema cho 1 scenario (Super Agent extract inputs)

```sql
SELECT
  d.code,
  d.name,
  JSON_EXTRACT(r.normalized_spec_json, '$.inputs') AS inputs
FROM scenario_definitions d
JOIN scenario_revisions r ON d.published_revision_id = r.id
WHERE d.code = :scenario_code
  AND d.is_archived = 0;
```

### 8.3 Audit: scenario này đã chạy bao nhiêu lần, tỉ lệ thành công

```sql
SELECT
  scenario_id,
  COUNT(*) AS total_runs,
  SUM(status = 'completed') AS success_count,
  SUM(status = 'failed') AS failure_count,
  AVG(duration_ms) AS avg_duration_ms  -- Sau Phase 2
FROM scenario_runs
WHERE scenario_id = :scenario_id
  AND date_created >= NOW() - INTERVAL 30 DAY
GROUP BY scenario_id;
```

### 8.4 Reverse lookup: session này dùng scenario gì

```sql
SELECT
  d.code,
  d.name,
  r.version_no,
  run.status,
  run.runtime_policy_snapshot
FROM scenario_runs run
JOIN scenario_definitions d ON run.scenario_id = d.id
JOIN scenario_revisions r ON run.revision_id = r.id
WHERE run.session_id = :session_id;
```

---

## 9. Notes về convention hiện tại (giữ nguyên khi update)

- **utf8mb4_unicode_ci** cho text (utf8mb4_bin cho JSON columns)
- **BIGINT(20)** cho mọi ID
- **INT(11)** cho boolean (0/1)
- **TIMESTAMP DEFAULT utc_timestamp()** — không dùng DATETIME(6) (đã fix qua APPLY_SCHEMA_CHANGES.sql)
- **KHÔNG có FOREIGN KEY constraint** — enforce ở app layer
- **KHÔNG có CHECK enum** — enforce ở Pydantic
- **VARCHAR(16) cho enum string** — không dùng SMALLINT
- **JSON columns** dùng `COLLATE utf8mb4_bin` + `CHECK (json_valid(...))`
- **soft delete** qua `is_archived` (0/1) — KHÔNG xóa hard

---

## 10. Migration file naming convention

Đặt file mới ở `dev/deploy_server/ai_tool_web/store/migrations/`:

```
001_init_mysql.sql               (Đã apply)
002_scenario_images.sql          (Đã apply)
003_super_agent_phase1.sql       (TODO - khi làm Phase 1)
004_super_agent_phase2.sql       (TODO - khi làm Phase 2)
005_super_agent_phase3.sql       (TODO - khi làm Phase 3)
```

Mỗi file phải có:
- Header comment giải thích mục đích + ngày + người chạy
- `USE changchatbot;`
- Pre-check (SELECT COUNT để confirm bảng tồn tại + có data hay không)
- ALTER statements
- Verify section (SHOW CREATE TABLE / INFORMATION_SCHEMA query)
- Rollback section (commented, dùng khi cần revert)

Template tham khảo: `APPLY_SCHEMA_CHANGES.sql` (đã apply 2026-04-25).

---

## 11. Checklist trước mỗi lần ALTER

- [ ] Backup `changchatbot` DB (mysqldump hoặc snapshot)
- [ ] Confirm 3 bảng nào sẽ touch — KHÔNG đụng bảng khác
- [ ] Đếm row count trước → ghi lại
- [ ] Test ALTER trên DB staging trước (nếu có)
- [ ] Chạy ALTER → verify schema mới qua `SHOW CREATE TABLE`
- [ ] Confirm row count không đổi (trừ khi ADD/DROP column)
- [ ] Rebuild Redis cache scenarios (sync từ MySQL `normalized_spec_json`)
- [ ] Test 1 scenario qua UI/API → đảm bảo không broken
- [ ] Update file này (`DB_REVIEW.md`) với schema mới

---

## 12. Open questions

1. **Có nên tạo bảng `users` không?** Hiện `owner_id` (INT), `created_by` (BIGINT) trỏ về user table chưa tồn tại. Phase 1 mock auth dùng `owner_code` VARCHAR. Khi tích hợp Azure SSO → cần bảng `users` thật?

2. **Có nên dedup `org_id` với hệ thống Chang chính?** Bảng `changchatbot` này là tool con, chia sẻ DB với app chính. Có nên join chéo với bảng `organizations` của app chính, hay duplicate?

3. **`scenario_runs.session_id` là String UUID — có cần FK với `sessions` bảng?** Hiện session lưu Redis (TTL), không có bảng MySQL. Nếu cần audit dài hạn → cần persist session metadata vào MySQL.

4. **Khi clone scenario, có copy luôn `scenario_images` không?** Hiện code app có handle nhưng chưa rõ behavior — cần document.

5. **Vector embedding cho semantic search**: dùng MariaDB Vector (11.7+), hay external (Qdrant/pgvector)? MariaDB 10.6 hiện tại không hỗ trợ.

---

**Reference**:
- Schema thực tế: `dev/deploy_server/ai_tool_web/store/migrations/001_init_mysql.sql`
- Image table: `dev/deploy_server/ai_tool_web/store/migrations/002_scenario_images.sql`
- Last apply log: `dev/deploy_server/APPLY_SCHEMA_CHANGES.sql` (2026-04-25)
- Pydantic models: `dev/deploy_server/LLM_base/scenarios/spec.py` + `flow_models.py`
- API endpoints: `dev/deploy_server/ai_tool_web/api/routes/scenarios.py` + `user_scenarios.py` + `sessions.py`

---

## 13. Plan: Input Fields Management (Phase 1 — `003_scenario_input_fields.sql`)

**Mục tiêu**: Tách input schema khỏi YAML, lưu vào bảng riêng `scenario_input_fields`. UI có 1 màn riêng quản lý field (add/edit/delete/reorder) qua form. YAML section `inputs:` auto-sync từ DB.

**Status**: 📋 Planned 2026-05-11 — chưa thực thi code. Anh review plan này khi rảnh.

### 13.1 Decisions đã chốt (2026-05-11)

| Decision | Choice | Lý do |
|----------|--------|-------|
| Source of truth cho `inputs` | **DB** | Edit qua form, tránh YAML drift |
| YAML inputs editable | **❌ Lock + readonly display** | User chỉ edit qua tab Inputs Manager |
| Per-revision vs per-definition | **Per-revision** | Versioning sạch, rollback đúng |
| Phase 1 scope | **Core CRUD + auto-sync YAML** | Templates + Super Agent fields để Phase 2/3 |
| Validation rules format | **JSON Schema 7 subset** | Chuẩn industry, dễ integrate sau |

### 13.2 DB Schema mới — `scenario_input_fields`

```sql
-- 003_scenario_input_fields.sql
CREATE TABLE IF NOT EXISTS `scenario_input_fields` (
  `id` BIGINT(20) NOT NULL AUTO_INCREMENT,

  -- FK refs (no constraints, app-layer enforce)
  `revision_id` BIGINT(20) NOT NULL,           -- → scenario_revisions.id
  `scenario_id` BIGINT(20) NOT NULL,           -- → scenario_definitions.id (denorm)

  -- Field definition (mirror ScenarioInputField Pydantic)
  `name` VARCHAR(64) NOT NULL,                 -- key trong context dict
  `display_label` VARCHAR(255) NOT NULL,       -- label trên UI form
  `field_type` VARCHAR(16) NOT NULL,           -- string | secret | number | bool | enum | url | date
  `is_required` INT(11) NOT NULL DEFAULT 0,    -- 0/1
  `source` VARCHAR(16) NOT NULL DEFAULT 'context',  -- context | ask_user
  `default_value` TEXT DEFAULT NULL,
  `description` TEXT DEFAULT NULL,             -- LLM Super Agent đọc

  -- Validation (JSON Schema 7 subset)
  `validation_rules` JSON DEFAULT NULL CHECK (json_valid(`validation_rules`)),
  -- vd: {"minLength": 3, "maxLength": 50, "pattern": "^QCVN.*", "enum": [...]}

  -- UI hints
  `placeholder` VARCHAR(255) DEFAULT NULL,
  `help_text` TEXT DEFAULT NULL,
  `display_order` INT(11) NOT NULL DEFAULT 0,

  -- Reserved cho Phase 2 (Super Agent fields) — schema sẵn, chưa expose UI
  `category` VARCHAR(32) DEFAULT 'user_input', -- user_input | credential | config | system
  `secret_ref` VARCHAR(255) DEFAULT NULL,      -- vd "vault/thuvienphapluat/{user_id}/password"
  `extraction_hint` TEXT DEFAULT NULL,         -- vd "Số hiệu QCVN từ user query"

  -- Reserved cho Phase 3 (templates)
  `template_id` BIGINT(20) DEFAULT NULL,       -- → input_field_templates.id

  -- Audit
  `date_created` TIMESTAMP NULL DEFAULT utc_timestamp(),
  `date_updated` TIMESTAMP NULL DEFAULT utc_timestamp(),
  `created_by` BIGINT(20) DEFAULT NULL,
  `updated_by` BIGINT(20) DEFAULT NULL,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_field_rev_name` (`revision_id`, `name`),
  KEY `idx_field_revision` (`revision_id`, `display_order`),
  KEY `idx_field_scenario` (`scenario_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**Convention tuân thủ codebase hiện tại**:
- BIGINT(20) cho IDs, INT(11) cho boolean
- VARCHAR(16) cho enum string
- TIMESTAMP DEFAULT utc_timestamp()
- JSON column với CHECK json_valid()
- KHÔNG có FOREIGN KEY (enforce app layer)
- utf8mb4_unicode_ci

### 13.3 Pydantic models mới

**File**: `dev/deploy_server/ai_tool_web/api/models/input_field.py` (mới)

```python
from typing import Optional, Literal
from pydantic import BaseModel, Field

class ValidationRules(BaseModel):
    """JSON Schema 7 subset."""
    minLength: Optional[int] = None
    maxLength: Optional[int] = None
    pattern: Optional[str] = None
    enum: Optional[list[str]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None

class InputFieldBase(BaseModel):
    name: str = Field(..., max_length=64, regex=r"^[a-z][a-z0-9_]*$")
    display_label: str = Field(..., max_length=255)
    field_type: Literal["string", "secret", "number", "bool", "enum", "url", "date"]
    is_required: bool = False
    source: Literal["context", "ask_user"] = "context"
    default_value: Optional[str] = None
    description: Optional[str] = None
    validation_rules: Optional[ValidationRules] = None
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    display_order: int = 0
    # Phase 2 (UI chưa expose, schema sẵn)
    category: Literal["user_input", "credential", "config", "system"] = "user_input"
    secret_ref: Optional[str] = None
    extraction_hint: Optional[str] = None

class InputFieldCreateRequest(InputFieldBase):
    pass

class InputFieldUpdateRequest(InputFieldBase):
    pass

class InputFieldResponse(InputFieldBase):
    id: int
    revision_id: int
    scenario_id: int
    date_created: str
    date_updated: str
```

### 13.4 API Endpoints mới

**File mới**: `dev/deploy_server/ai_tool_web/api/routes/input_fields.py`

```python
router = APIRouter(prefix="/v1/scenarios/{scenario_id}/revisions/{revision_id}/input-fields")

@router.get("", response_model=list[InputFieldResponse])
async def list_fields(scenario_id: int, revision_id: int): ...

@router.post("", response_model=InputFieldResponse, status_code=201)
async def create_field(scenario_id, revision_id, body: InputFieldCreateRequest): ...

@router.put("/{field_id}", response_model=InputFieldResponse)
async def update_field(field_id, body: InputFieldUpdateRequest): ...

@router.delete("/{field_id}", status_code=204)
async def delete_field(field_id): ...

@router.post("/reorder", response_model=list[InputFieldResponse])
async def reorder_fields(scenario_id, revision_id, body: ReorderRequest): ...

@router.post("/bulk", response_model=list[InputFieldResponse])
async def bulk_replace(scenario_id, revision_id, body: BulkReplaceRequest):
    """UI gọi khi user save toàn bộ form 1 lần."""
    ...
```

**Side effects mỗi endpoint mutate**:
1. Mutate DB `scenario_input_fields`
2. Regenerate `raw_yaml` của revision (call `regenerate_yaml_inputs_block()`)
3. Re-parse YAML → update `normalized_spec_json`
4. Sync sang Redis (qua `scenario_service.save_async()`)
5. Update `scenario_revisions.date_updated`

### 13.5 YAML Sync Logic

**File mới**: `dev/deploy_server/ai_tool_web/services/yaml_sync.py`

#### 13.5.1 Markers

```python
AUTO_GEN_START_MARKER = (
    "# ╔══════════════════════════════════════════════════════════╗\n"
    "# ║  AUTO-GENERATED — DO NOT EDIT MANUALLY                   ║\n"
    "# ║  Sync from DB: scenario_input_fields (revision_id={rev_id}) ║\n"
    "# ║  Edit via: Inputs tab → Add/Edit Field                   ║\n"
    "# ╚══════════════════════════════════════════════════════════╝\n"
)
AUTO_GEN_END_MARKER = (
    "# ╔══════════════════════════════════════════════════════════╗\n"
    "# ║  END AUTO-GENERATED                                       ║\n"
    "# ╚══════════════════════════════════════════════════════════╝\n"
)
```

#### 13.5.2 Regenerate YAML inputs block

```python
def regenerate_yaml(revision_id: int) -> str:
    """DB → YAML. Gọi sau mỗi mutate input field."""
    fields = (
        session.query(ScenarioInputField)
        .filter_by(revision_id=revision_id)
        .order_by(ScenarioInputField.display_order)
        .all()
    )

    # Build YAML inputs block từ DB
    inputs_list = [field.to_yaml_dict() for field in fields]
    inputs_yaml = yaml.dump(
        {"inputs": inputs_list},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )

    auto_gen_block = (
        AUTO_GEN_START_MARKER.format(rev_id=revision_id)
        + inputs_yaml
        + AUTO_GEN_END_MARKER
    )

    # Lấy phần user-editable từ raw_yaml cũ (strip auto-gen block + cũ inputs:)
    rev = session.query(ScenarioRevision).get(revision_id)
    user_section = strip_auto_gen_and_inputs_block(rev.raw_yaml)

    new_raw_yaml = auto_gen_block + "\n" + user_section
    return new_raw_yaml
```

#### 13.5.3 Strip auto-gen + inputs block khỏi YAML

```python
def strip_auto_gen_and_inputs_block(raw_yaml: str) -> str:
    """Loại bỏ:
      - Vùng giữa AUTO_GEN_START_MARKER và AUTO_GEN_END_MARKER (nếu có)
      - Top-level `inputs:` block (nếu YAML cũ chưa có markers)
    Trả về phần còn lại (allowed_domains, steps, success, hooks, ...).
    """
    # Pattern 1: có markers (post-migration)
    start_idx = raw_yaml.find("AUTO-GENERATED")
    end_idx = raw_yaml.find("END AUTO-GENERATED")
    if start_idx >= 0 and end_idx > start_idx:
        # Skip cả auto-gen block
        before = raw_yaml[:_marker_line_start(raw_yaml, start_idx)]
        after = raw_yaml[_marker_line_end(raw_yaml, end_idx):]
        return before + after

    # Pattern 2: YAML cũ pre-migration, có top-level `inputs:`
    parsed = yaml.safe_load(raw_yaml)
    if isinstance(parsed, dict) and "inputs" in parsed:
        del parsed["inputs"]
        return yaml.dump(parsed, sort_keys=False, allow_unicode=True)

    return raw_yaml
```

#### 13.5.4 Drift detection khi parse YAML

```python
def parse_and_validate_yaml(revision_id: int, raw_yaml: str) -> ScenarioSpec:
    """Khi user save YAML qua tab Flow YAML."""
    spec = parse_yaml_to_spec(raw_yaml)

    # Validate inputs từ YAML khớp với DB
    db_fields = (
        session.query(ScenarioInputField)
        .filter_by(revision_id=revision_id)
        .order_by(ScenarioInputField.display_order)
        .all()
    )

    yaml_inputs = spec.inputs
    db_inputs_as_spec = [f.to_input_field() for f in db_fields]

    if not _spec_inputs_equal(yaml_inputs, db_inputs_as_spec):
        raise YamlInputsDriftError(
            "YAML `inputs:` section is auto-managed and out of sync with DB. "
            "Either: (a) regenerate via 'Sync YAML' button, "
            "or (b) edit inputs through 'Inputs' tab instead."
        )

    return spec
```

### 13.6 UI Concept

#### 13.6.1 Layout 2 tab tại trang Scenario Editor

```
┌─────────────────────────────────────────────────────────────┐
│ Scenario: Kiểm tra version & Tải QCVN/TCVN  [Rev 12 active] │
│ ┌─────────────┬──────────────┬──────────────┬─────────────┐ │
│ │ ▶ Inputs    │   Flow YAML  │  Test Run    │   History   │ │
│ └─────────────┴──────────────┴──────────────┴─────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

#### 13.6.2 Tab "Inputs"

```
┌─────────────────────────────────────────────────┐
│  Input Fields (4)                  [+ Add Field] │
│  ┌────────────────────────────────────────────┐  │
│  │ ≡ 1. username (string, required)      [✏][🗑]│  │
│  │   Tên đăng nhập / Email thuvienphapluat...  │  │
│  ├────────────────────────────────────────────┤  │
│  │ ≡ 2. password (secret, required)      [✏][🗑]│  │
│  ├────────────────────────────────────────────┤  │
│  │ ≡ 3. so_hieu (string, required)       [✏][🗑]│  │
│  ├────────────────────────────────────────────┤  │
│  │ ≡ 4. title_hint (string, required)    [✏][🗑]│  │
│  └────────────────────────────────────────────┘  │
│                                                  │
│  Drag ≡ to reorder. Changes auto-sync to YAML.  │
└─────────────────────────────────────────────────┘
```

#### 13.6.3 Modal "Add/Edit Field"

```
┌──────────────────────────────────────────┐
│  Edit Input Field                  [X]   │
├──────────────────────────────────────────┤
│  Name (key)     [so_hieu          ]      │
│    (lowercase + underscore, no space)    │
│  Display Label  [Số hiệu QCVN     ]      │
│  Type           [string         ▾]       │
│  Required       [✓]                      │
│  Source         [context        ▾]       │
│  ─────────────────────────────────       │
│  Description (Super Agent LLM đọc)       │
│  [Số hiệu QCVN/TCVN — điền vào...]      │
│                                          │
│  Default value  [                ]       │
│  Placeholder    [vd QCVN 86      ]       │
│  Help text      [                ]       │
│  ─────────────────────────────────       │
│  ▾ Validation rules                      │
│    Min length   [    ]  Max  [    ]      │
│    Pattern      [                ]       │
│    Enum         [+ Add value]            │
│  ─────────────────────────────────       │
│            [Cancel]  [Save]              │
└──────────────────────────────────────────┘
```

#### 13.6.4 Tab "Flow YAML" — Lock inputs section

```yaml
# ╔══════════════════════════════════════════════════════════╗
# ║  AUTO-GENERATED — DO NOT EDIT MANUALLY                   ║
# ║  Sync from DB: scenario_input_fields (revision_id=42)    ║
# ║  Edit via: Inputs tab → Add/Edit Field                   ║
# ╚══════════════════════════════════════════════════════════╝
inputs:
  - name: username
    type: string
    required: true
    source: context
    description: "Tên đăng nhập / Email thuvienphapluat.vn"
  - name: password
    type: secret
    required: true
    source: context
    description: "Mật khẩu thuvienphapluat.vn (masked)"
  - name: so_hieu
    type: string
    required: true
    source: context
    description: "Số hiệu QCVN/TCVN — điền vào ô search"
  - name: title_hint
    type: string
    required: true
    source: context
    description: "Text đặc trưng trong tiêu đề tiêu chuẩn cần mở"
# ╔══════════════════════════════════════════════════════════╗
# ║  END AUTO-GENERATED                                       ║
# ╚══════════════════════════════════════════════════════════╝

# ──── User editable from here ────
allowed_domains:
  - thuvienphapluat.vn

steps:
  - action: if_visible
    target: ...
```

**UI behavior**:
- Vùng giữa 2 markers: **readonly** (gray background, lock icon góc phải)
- User edit thử → reject save với toast "This section is managed via Inputs tab"
- Vùng còn lại: full editor cho `steps`, `success`, `failure`, `hooks`, etc.

### 13.7 Migration plan (chi tiết)

#### Step 1: Tạo bảng (file `003_scenario_input_fields.sql`)

Template tham khảo `APPLY_SCHEMA_CHANGES.sql` — pre-check → CREATE → verify.

```sql
USE changchatbot;

-- Pre-check: bảng chưa tồn tại
SELECT COUNT(*) AS table_exists
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'changchatbot'
  AND TABLE_NAME = 'scenario_input_fields';
-- Kỳ vọng: 0 → safe to create

CREATE TABLE IF NOT EXISTS `scenario_input_fields` (
  -- ... (xem 13.2)
);

-- Verify
SHOW CREATE TABLE scenario_input_fields;
SELECT COUNT(*) FROM scenario_input_fields;  -- Phải = 0
```

#### Step 2: Backfill script (`scripts/backfill_input_fields.py`)

```python
"""
Backfill scenario_input_fields từ scenario_revisions.normalized_spec_json.

Idempotent: chạy lần 2 sẽ skip revisions đã backfill.
Rollback: DELETE FROM scenario_input_fields; (chưa apply prod thì xóa được).
"""

def backfill():
    revs = session.query(ScenarioRevision).all()
    for rev in revs:
        # Skip nếu đã backfill
        existing = (
            session.query(ScenarioInputField)
            .filter_by(revision_id=rev.id)
            .count()
        )
        if existing > 0:
            print(f"⏭  Rev {rev.id}: already has {existing} fields, skip")
            continue

        spec = ScenarioSpec.model_validate_json(rev.normalized_spec_json)
        if not spec.inputs:
            print(f"⏭  Rev {rev.id}: no inputs in spec, skip")
            continue

        for idx, inp in enumerate(spec.inputs):
            field = ScenarioInputField(
                revision_id=rev.id,
                scenario_id=rev.scenario_id,
                name=inp.name,
                display_label=inp.name.replace("_", " ").title(),  # default heuristic
                field_type=inp.type,
                is_required=1 if inp.required else 0,
                source=inp.source,
                default_value=str(inp.default) if inp.default else None,
                description=inp.description,
                display_order=idx,
                category="user_input",  # default, admin sau sửa
                created_by=None,
                updated_by=None,
            )
            session.add(field)

        print(f"✅ Rev {rev.id}: backfilled {len(spec.inputs)} fields")

    session.commit()
```

#### Step 3: Inject markers vào raw_yaml hiện có

```python
def inject_markers_into_existing_yaml():
    revs = session.query(ScenarioRevision).all()
    for rev in revs:
        if AUTO_GEN_START_MARKER in rev.raw_yaml:
            print(f"⏭  Rev {rev.id}: already has markers")
            continue

        new_yaml = yaml_sync.regenerate_yaml(rev.id)  # Đã backfill ở Step 2
        rev.raw_yaml = new_yaml
        print(f"✅ Rev {rev.id}: markers injected")

    session.commit()
```

#### Step 4: Deploy code mới

- Tag Pydantic models trong `api/models/input_field.py`
- API routes `api/routes/input_fields.py`
- Service `services/yaml_sync.py`
- UI 2 tab (`scenario-editor/InputsTab.tsx`, `FlowYamlTab.tsx`)

#### Step 5: Verify end-to-end

Test scenario QCVN sau migration:
```
1. UI mở scenario QCVN, tab Inputs → thấy 4 field đã backfill ✓
2. UI thêm field thứ 5 `email_recipient`
3. Save → check DB: 5 row trong scenario_input_fields ✓
4. Tab Flow YAML → thấy `inputs:` block có 5 field, auto-gen markers ✓
5. Run scenario QCVN → context validate fail vì thiếu `email_recipient` (chứng minh sync hoạt động) ✓
6. UI xóa field `email_recipient` qua tab Inputs
7. Save → YAML block update → run scenario → pass ✓
```

### 13.8 Phase 1 task breakdown (3-4 ngày)

| # | Task | Files | Estimate |
|---|------|-------|----------|
| 1 | Migration SQL `003_scenario_input_fields.sql` | `migrations/` | 0.5d |
| 2 | Backfill script `backfill_input_fields.py` | `scripts/` | 0.5d |
| 3 | Pydantic models `InputFieldBase/Create/Update/Response` | `api/models/input_field.py` | 0.5d |
| 4 | Service `yaml_sync.py` — regenerate + strip + drift detector | `services/yaml_sync.py` | 1d |
| 5 | API routes CRUD + bulk + reorder | `api/routes/input_fields.py` | 0.5d |
| 6 | Update scenario_service.py để gọi yaml_sync sau mutate | `services/scenario_service.py` | 0.5d |
| 7 | UI tab "Inputs" với list + drag-drop reorder | `frontend/scenario-editor/InputsTab.tsx` | 1d |
| 8 | UI modal "Add/Edit Field" với validation form | `frontend/scenario-editor/InputFieldModal.tsx` | 0.5d |
| 9 | UI tab "Flow YAML" với lock auto-gen section | `frontend/scenario-editor/FlowYamlTab.tsx` | 0.5d |
| 10 | E2E test scenario QCVN migration → add → run | `tests/e2e/test_input_fields.py` | 0.5d |

**Total**: ~6 ngày dev (1 person) hoặc 3-4 ngày (2 person song song UI + backend).

### 13.9 Risks & Mitigation

| Risk | Mức độ | Mitigation |
|------|--------|------------|
| Backfill làm hỏng data | Cao | Backup DB trước; idempotent script; test trên staging |
| YAML drift sau migration | Cao | Drift detector + UI warning + nightly job verify |
| User confused 2 tab | Trung | Tour onboarding lần đầu; tooltip "Inputs managed via form" |
| Performance khi scenario có 50+ fields | Thấp | Pagination form (chỉ render 20, scroll lazy) |
| Race condition khi 2 admin edit cùng lúc | Trung | Optimistic locking qua `date_updated` timestamp |
| YAML user edit manually section auto-gen | Trung | Reject save với clear error message; cho copy-paste readonly |

### 13.10 Reserved fields cho Phase 2 / Phase 3

DB schema đã có sẵn các cột chưa expose UI nhưng sẵn sàng cho phase sau:

| Field | Phase | Purpose |
|-------|-------|---------|
| `category` | Phase 2 (Super Agent) | Phân loại user_input / credential / config / system |
| `secret_ref` | Phase 2 (Super Agent) | Template path lookup vault |
| `extraction_hint` | Phase 2 (Super Agent) | Hướng dẫn LLM extract |
| `template_id` | Phase 3 (Templates) | Reference đến `input_field_templates` |

→ Phase 1 chỉ cần migration 1 lần. Phase 2/3 chỉ cần update Pydantic model + UI tab "Advanced", KHÔNG phải ALTER TABLE.

### 13.11 TODO checklist (anh check khi sẵn sàng thực thi)

- [ ] Review plan section 13 này, confirm design decisions
- [ ] Confirm thời điểm thực thi (sau session 05-XX nào?)
- [ ] Backup DB `changchatbot` trước migration
- [ ] Chạy `003_scenario_input_fields.sql` trên staging trước (nếu có)
- [ ] Chạy backfill script + verify count rows = expected
- [ ] Deploy code API + UI
- [ ] Smoke test QCVN scenario end-to-end
- [ ] Document Super Agent integration với spec mới (Phase 1 đủ chưa, hay đợi Phase 2 expose `category`/`secret_ref`)
- [ ] Update file này (13.X status) sau khi apply xong
