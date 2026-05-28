# DB_CLEANUP_REVIEW.md

**Phạm vi**: review schema 5 bảng `scenario_*` của `changchatbot @ 172.28.8.11`, đánh dấu các bảng/cột dead để DROP.
**Ngày**: 2026-05-28
**Người soạn**: Claude theo yêu cầu hiepqn — verified bằng code trace + dump schema (`dev/data_base/db_scenario_*.txt`).
**Liên quan**: [DB_REVIEW.md](DB_REVIEW.md) (review tổng quan 2026-05-11).

---

## TL;DR

| Bảng | Tổng cột | Bỏ | Còn |
|---|---|---|---|
| `scenario_images` | 14 | **DROP TABLE** | 0 |
| `scenario_runs` | 12 | **DROP TABLE** | 0 |
| `scenario_input_fields` | 22 | **DROP TABLE** | 0 |
| `scenario_definitions` | 14 | 4 cột + 1 index | 10 |
| `scenario_revisions` | 18 | 3 cột | 15 |

**Tổng**: **3 bảng DROP** + 7 cột DROP COLUMN + 1 index DROP. Sau cleanup còn **2 bảng × 25 cột** (10 + 15).

**Code paths cần dọn theo** (chi tiết ở phần "Code cleanup checklist" cuối doc):
- Toàn bộ stack `scenario_images` (service/repo/route/DI/handler/test/migration)
- Repo methods `create_run`/`update_run_status`/`get_run` của `scenario_runs` + sqlite + tests
- **Toàn bộ stack `scenario_input_fields`** (route/repo/service yaml_sync/DI/tests) + **UI tab Inputs** (`web_UI_test/src/components/InputFieldsTab.{jsx,module.css}`, `InputFieldModal.{jsx,module.css}` + reference trong `ScenariosPage.jsx`) — user chốt edit raw YAML trực tiếp, không cần form CRUD field.
- Pydantic field `schema_version`/`created_by`/`updated_by` ở model layer (optional)

**Prerequisite trước khi DROP `scenario_images`**: chạy `dev/data_base/find_legacy_image_hint_urls.sql` (Q1) — nếu count > 0 → migrate YAML sang GDrive URL trước.

---

## 1. `scenario_definitions` (14 cột)

| Cột | Status | Evidence | Đề xuất |
|---|---|---|---|
| `id` BIGINT | ✅ USED | PK, FK target cho 4 bảng khác | KEEP |
| `code` VARCHAR(64) UNIQUE | ✅ USED (business key) | `mysql_scenario_repo.py:211,263,289,346` | KEEP |
| `name` | ✅ USED | INSERT + `_row_to_definition` | KEEP |
| `owner_id` INT | ❌ **DEAD** | INSERT NULL `mysql_scenario_repo.py:238` ("Phase 1 luôn NULL"); KHÔNG ai SELECT WHERE owner_id; bridge dùng `owner_code` (string) | **DROP** |
| `owner_code` VARCHAR(64) | ✅ USED | INSERT `defn.owner_id`; `count_by_owner` WHERE owner_code | KEEP |
| `org_id` INT | ❌ **DEAD** | INSERT NULL `:240` ("Phase 2"); `_row_to_definition:92` đọc nhưng row luôn NULL | **DROP** |
| `source_type` VARCHAR(16) | ✅ USED | `count_builtin` WHERE source_type='builtin'; filter `list_definitions`; index `idx_def_source` | KEEP |
| `visibility` VARCHAR(16) | ✅ USED (metadata) | INSERT + đọc; chưa thấy filter WHERE visibility — chỉ metadata UI | KEEP |
| `published_revision_id` | ✅ USED | `set_published_revision`; JOIN trong `get_published_revision` | KEEP |
| `is_archived` INT | ✅ USED | `archive_definition`; filter list + `count_by_owner` | KEEP |
| `date_created` | ✅ USED | INSERT | KEEP |
| `date_updated` | ✅ USED | UPDATE `archive_definition`, `set_published_revision`; ORDER BY DESC | KEEP |
| `created_by` BIGINT | ❌ **DEAD** | INSERT NULL `:247` ("Phase 1 NULL"); `_row_to_definition` không đọc | **DROP** |
| `updated_by` BIGINT | ❌ **DEAD** | INSERT NULL `:248`; không UPDATE | **DROP** |

**Index ảnh hưởng**: `idx_def_owner (owner_id, is_archived)` — chứa cột `owner_id` luôn NULL → vô dụng. **DROP INDEX** kèm.
Index `idx_def_source` GIỮ (cột source_type live).

**DROP**: 4 cột + 1 index. Còn lại 10 cột.

---

## 2. `scenario_revisions` (18 cột)

| Cột | Status | Evidence | Đề xuất |
|---|---|---|---|
| `id` BIGINT | ✅ USED | PK | KEEP |
| `scenario_id` BIGINT | ✅ USED | FK to definitions.id | KEEP |
| `version_no` INT | ✅ USED | `append_revision` next_version; UNIQUE | KEEP |
| `raw_yaml` LONGTEXT | ✅ USED | INSERT + SELECT + `update_revision_yaml` | KEEP |
| `normalized_spec_json` JSON | ✅ USED | tương tự | KEEP |
| `yaml_hash` CHAR(64) | ✅ USED | INSERT + UPDATE; index `idx_rev_hash` | KEEP |
| `parent_revision_id` BIGINT | ✅ USED | `user_scenario_service.py:310,553` set thật; test verify chain | KEEP |
| `clone_source_revision_id` BIGINT | ✅ USED | `user_scenario_service.py:310,553` set khi clone | KEEP |
| `schema_version` INT (default 2) | ⚠️ **WRITE-ONLY** | `builtin_seeder.py:136` + `user_scenario_service.py:554` luôn ghi =1; **0 code path** rẽ nhánh theo nó (`grep schema_version [=!<>]` ra rỗng) | **DROP** (recommended) |
| `static_validation_status` | ✅ USED | INSERT + SELECT | KEEP |
| `static_validation_errors` JSON | ✅ USED | tương tự | KEEP |
| `last_test_run_at` | ✅ USED | `update_revision_test_status` | KEEP |
| `last_test_run_status` | ✅ USED | tương tự | KEEP |
| `last_test_run_id` | ✅ USED | tương tự | KEEP |
| `date_created` | ✅ USED | INSERT | KEEP |
| `date_updated` | ✅ USED | INSERT + `update_revision_yaml` | KEEP |
| `created_by` BIGINT | ❌ **DEAD** | INSERT NULL `:415` ("Phase 1 NULL"); model layer trả "" sentinel | **DROP** |
| `updated_by` BIGINT | ❌ **DEAD** | INSERT NULL `:418`; không UPDATE | **DROP** |

**Lưu ý `schema_version`**: bản chất là "DSL version" — nếu sau này có v3 cần migrate runtime parser thì sẽ cần. Hiện tại 100% scenarios = v1, không có v2/v3, không có branching code. → **DROP an toàn**; khi có v3 thì ADD COLUMN lại + add migration logic.

**DROP**: 3 cột. Còn 15.

---

## 3. `scenario_runs` (12 cột) — ⚠️ **TOÀN BỘ BẢNG DEAD**

**Bằng chứng dead**:
- `create_run()` chỉ gọi trong **test code**:
  - `dev/deploy_server/ai_tool_web/store/_smoke_test.py:123`
  - `dev/deploy_server/ai_tool_web/tests/test_mysql_scenario_repo.py:325`
  - `dev/deploy_server/ai_tool_web/tests/test_scenario_repo.py:305,333`
- **0 production caller** (worker, API, scenario_service, user_scenario_service).
- `AUTO_INCREMENT=3` (dump 2026-05-11) — đúng 3 run từ test, không phải prod.
- Audit per-run hiện đi **Redis (sessions:*) + CDN (request_flow.json, session.json, result.json)**. Không có gap functional khi drop bảng này.

**Đề xuất**: **DROP TABLE `scenario_runs`** + clean code:
- `mysql_scenario_repo.py`: bỏ `create_run`, `update_run_status`, `get_run`, `_row_to_run`
- `sqlite_scenario_repo.py`: tương tự
- `store/scenario_repo.py`: bỏ `ScenarioRun` dataclass + interface methods
- `store/_smoke_test.py`, `tests/test_mysql_scenario_repo.py`, `tests/test_scenario_repo.py`: bỏ test cases liên quan

**Risk**: nếu sau này muốn audit DB cho mỗi run (cho report "Hôm nay user X chạy bao nhiêu scenario") → phải build lại bảng. Lựa chọn được user chốt: DROP, nếu cần thì add lại.

---

## 4. `scenario_input_fields` (22 cột) — ⚠️ **DROP TABLE** (cả stack dead)

**Bằng chứng**:
- Production traffic (Sup Agent qua `POST /v1/sessions`) = 100% qua `scenario_yaml` inline (ad-hoc spec `_custom_*`) hoặc `query` (LLM gen → ad-hoc spec `_q_*`). Verify ở `sessions.py:351-394`: cả 2 nhánh `normalize_yaml` → ad-hoc spec, **KHÔNG lưu DB**, **KHÔNG touch `scenario_input_fields`**. Worker đọc `spec_snapshot` từ Redis.
- Path duy nhất chạm bảng này: User → Web UI Inputs tab (`InputFieldsTab.jsx`) → CRUD qua API `/v1/scenarios/{code}/revisions/{n}/input-fields` → `yaml_sync.regenerate_revision_yaml` → UPDATE raw_yaml.
- User chốt: **bỏ tab Inputs, edit YAML trực tiếp** (RESERVED Phase 2/3 fields cũng drop kèm — user đã chốt ở turn trước).
- DB hiện chỉ có scenarios builtin + `_q_*`/`_custom_*`, không có scenario `user` được tạo qua UI → bảng chưa từng có giá trị thực.

**Đề xuất**: **DROP TABLE `scenario_input_fields`** + clean cả stack:
- `store/mysql_scenario_input_field_repo.py` (toàn file)
- `services/yaml_sync.py` (toàn file — chỉ phục vụ regenerate YAML khi input_fields đổi)
- `api/routes/input_fields.py` (toàn file)
- `api/app.py:32,81,214-227,256-259` — bỏ import + register router + DI init + cleanup
- `api/dependencies.py:26,59-77` — `get_input_field_repo`, `get_yaml_sync_service`
- Tests: `test_input_field_*`, `test_yaml_sync_*`, `test_user_scenarios_api.py` (phần input_fields)
- Migration `003_scenario_input_fields.sql` GIỮ file (lịch sử)
- **UI cleanup**: xóa `web_UI_test/src/components/InputFieldsTab.jsx`, `InputFieldModal.jsx`, 2 CSS module tương ứng; gỡ reference + tab "Inputs" trong `ScenariosPage.jsx`

**Risk**: nếu sau muốn quay lại model "DB là source, YAML auto-gen từ DB" (DB_REVIEW section 13 plan) → phải build lại bảng + repo + service + UI tab. User chấp nhận trade-off.

---

## 5. `scenario_images` (14 cột) — DROP TABLE

Đã xác định dead ở turn trước (chuyển sang GDrive URL trực tiếp, không upload CDN nữa).

**Stack code cần xóa**:
- `store/mysql_scenario_image_repo.py` (toàn file)
- `services/scenario_image_service.py` (toàn file)
- `api/routes/scenario_images.py` (toàn file)
- `api/dependencies.py:85-97` — `get_image_repo`, `get_image_service`
- `api/app.py:193-197` — startup init repo singleton
- `api/exception_handlers.py:17` — `ScenarioImageNotFound/BadRequest/UploadFailed` handlers
- `tests/test_scenario_image_service.py`, `tests/test_scenario_images_api.py`
- `store/migrations/002_scenario_images.sql` (không xóa file, để track lịch sử migration)
- Router register trong app (search `scenario_images` router include)

**Prerequisite**: chạy `dev/data_base/find_legacy_image_hint_urls.sql` (Q1) → nếu > 0 → migrate YAML sang GDrive URL trước, không thì runtime vision_matcher fetch fail.

---

## 6. Tổng hợp DROP scope

### DROP TABLE
1. `scenario_images` (sau khi migrate YAML)
2. `scenario_runs` (gỡ luôn)
3. `scenario_input_fields` (gỡ kèm UI tab Inputs + service yaml_sync)

### DROP COLUMN
**scenario_definitions** (4 cột + 1 index):
- `owner_id`, `org_id`, `created_by`, `updated_by`
- INDEX `idx_def_owner`

**scenario_revisions** (3 cột):
- `schema_version`, `created_by`, `updated_by`

### Code cleanup checklist (sau khi DROP DB)
- [ ] `mysql_scenario_repo.py`: xóa `create_run/update_run_status/get_run/_row_to_run` + tham chiếu `created_by/updated_by/started_by/owner_id/org_id/schema_version` ở INSERT/UPDATE/_row_*
- [ ] `sqlite_scenario_repo.py`: mirror
- [ ] `store/scenario_repo.py`: bỏ `ScenarioRun` dataclass + interface; trim `ScenarioDefinition.org_id`, `ScenarioRevision.created_by/schema_version` nếu không còn dùng ở model
- [ ] **Toàn bộ stack `scenario_input_fields`** (xem mục 4): xóa file `mysql_scenario_input_field_repo.py`, `services/yaml_sync.py`, `api/routes/input_fields.py`; gỡ register/DI/init trong `api/app.py` + `api/dependencies.py`
- [ ] **Toàn bộ stack `scenario_images`** (xem mục 5)
- [ ] `services/user_scenario_service.py`: bỏ `created_by=user.user_id`, `schema_version=1`
- [ ] `services/builtin_seeder.py`: bỏ `created_by=system_user_id`, `schema_version=1`
- [ ] **UI cleanup**: xóa `web_UI_test/src/components/InputFieldsTab.{jsx,module.css}`, `InputFieldModal.{jsx,module.css}`; gỡ tab "Inputs" + import trong `ScenariosPage.jsx`
- [ ] Tests: cập nhật/xóa các test ref đến cột/bảng đã drop (`test_input_field_*`, `test_yaml_sync_*`, phần liên quan trong `test_user_scenarios_api.py`)
- [ ] Pydantic models (`models.py`, `store/scenario_repo.py`): trim field theo schema mới

---

## 7. Migration order (khuyến nghị)

1. **Backup**: `mysqldump -h 172.28.8.11 -u <u> -p changchatbot scenario_definitions scenario_revisions scenario_runs scenario_images scenario_input_fields > backup_before_db_cleanup_$(date +%Y%m%d_%H%M%S).sql`
2. **Quét legacy URL**: chạy `dev/data_base/find_legacy_image_hint_urls.sql` Q1. Nếu > 0 → migrate YAML sang GDrive, lặp tới khi = 0.
3. **DROP DB** (chạy `dev/data_base/cleanup_db_dead_columns.sql` — file tạo cùng đợt này):
   - DROP TABLE scenario_images
   - DROP TABLE scenario_runs
   - ALTER 3 bảng còn lại (DROP COLUMN)
4. **Code cleanup**: theo checklist mục 6. Push GitHub → laptop sync product_build → GitLab → K8s redeploy.
5. **Smoke test**: tạo 1 scenario user, list, archive — verify không crash.

---

## 8. Risks & mitigation

| Risk | Likelihood | Mitigation |
|---|---|---|
| Code production còn ref cột đã drop → crash query "Unknown column" | Cao | Cleanup code TRƯỚC khi drop DB, hoặc cleanup DB rồi rollback nếu code chưa kịp deploy. Recommended: code first, deploy, verify, rồi drop DB. |
| Phase 2 Super Agent cần `secret_ref`/`extraction_hint` → phải ADD COLUMN lại | Trung | Doc rõ trong DB_REVIEW section 13 — Phase 2 cần ALTER. Schema chấp nhận thay đổi. |
| Có scenario thực sự dùng image_hint CDN cũ → migrate sót → vision fail runtime | Trung | Q1 ở `find_legacy_image_hint_urls.sql` đảm bảo 0 trước khi DROP TABLE images. |
| `scenario_runs` sau này cần audit DB → phải build lại bảng + worker insert | Thấp (đã chốt) | Tài liệu hóa quyết định. Build lại tốn ~1 buổi nếu cần. |
| `schema_version` nay drop, sau có DSL v3 → mất khả năng phân biệt | Thấp | ADD COLUMN lại + migration logic khi có v3 thật. |

---

## Files liên quan đợt cleanup
- `dev/data_base/find_legacy_image_hint_urls.sql` — quét YAML legacy URL trước DROP images
- `dev/data_base/cleanup_db_dead_columns.sql` — sẽ tạo cùng đợt, chạy sau khi code đã deploy
- `dev/data_base/db_scenario_*.txt` — dump schema gốc 2026-05-11 (baseline)
- `dev/deploy_server/docs/DB_REVIEW.md` — review tổng 2026-05-11 (vẫn còn dùng)
