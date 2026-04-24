# Đề xuất thay đổi Schema — Scenario Tables

> **Gửi:** DBA / Tech lead giữ quyền ALTER trên `changchatbot` DB
> **Từ:** Tool Web team (hiepqn)
> **Ngày:** 2026-04-24
> **Mục đích:** Adapt 3 bảng `scenario_*` để phục vụ Tool Web Phase 1 (user-configurable scenario system)

---

## 0. Bối cảnh ngắn

Tool Web là 1 tool nhỏ trong dự án chatbot chung, cho phép **user tự tạo scenario YAML** để chạy browser automation agent. Phase 1 deploy cho dev-internal test.

Code Python đã code xong + test xong 256/256 case (local SQLite). Giờ migrate sang MySQL/MariaDB trên server dùng chung 3 bảng đã có sẵn trong `changchatbot`:

- `scenario_definitions` — metadata scenario
- `scenario_revisions` — lịch sử phiên bản scenario (immutable)
- `scenario_runs` — mỗi lần chạy 1 session log

So sánh schema hiện tại với code Python → phát hiện một số khác biệt cần xử lý. Đề xuất thay đổi chia 3 tiers:

- **🔴 TIER 1** — MUST-FIX (bugs trong schema, không phụ thuộc code)
- **🟡 TIER 2** — RECOMMENDED (giữ code gọn, enterprise convention)
- **🟢 TIER 3** — OPTIONAL (data integrity, có thể làm sau)

---

## 1. 🔴 TIER 1 — MUST-FIX

Đây là các issue **chắc chắn là lỗi** trong DDL ban đầu. Code không thể work-around.

### 1.1 Fix typo tên cột: ` date_created` có khoảng trắng đầu

**Hiện tại:**
```
scenario_definitions.` date_created`    ← Space trước "date"
scenario_revisions.` date_created`      ← Space trước "date"
```

**Đổi:**
```
date_created (không space)
```

**Lý do:**
- Tên cột bắt đầu bằng space là **typo trong DDL**. MariaDB không báo lỗi nhưng mọi query phải escape backtick: `` SELECT `\ date_created` FROM... `` — rất fragile.
- Python driver `pymysql`/`aiomysql` sẽ lỗi nếu code ghi `"date_created"` (không space) trong `INSERT`.
- Ngoài ra còn mismatch giữa 2 bảng `scenario_definitions`, `scenario_revisions` (cùng bị lỗi) vs `scenario_runs` (không bị) → không consistent.

**SQL:**
```sql
ALTER TABLE scenario_definitions
    CHANGE COLUMN ` date_created` date_created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE scenario_revisions
    CHANGE COLUMN ` date_created` date_created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;
```

**Ảnh hưởng:** Không. Chỉ rename cột, data giữ nguyên. Backup không cần thiết vì bảng hiện rỗng.

---

### 1.2 Fix sai data type: `last_test_run_status`, `last_test_run_at`, `static_validation_errors`

Trong `scenario_revisions`:

| Cột | Hiện tại | Đổi thành | Lý do |
|---|---|---|---|
| `last_test_run_status` | `TIMESTAMP NOT NULL` | `VARCHAR(16) NULL` | Field này lưu string kết quả test run ('passed'/'failed'), không phải datetime. **Chắc chắn typo** — prob copy-paste nhầm từ `last_test_run_at`. |
| `last_test_run_at` | `TIMESTAMP NOT NULL` | `DATETIME(6) NULL` | Khi revision **chưa** test run, cột này phải NULL (không có thời gian). Hiện `NOT NULL` bắt buộc có giá trị → code INSERT fail. DATETIME(6) cho microsecond precision (audit log chính xác). |
| `static_validation_errors` | `SMALLINT NULL` | `JSON NULL` | Cột lưu **chi tiết lỗi validate** dạng structured JSON `[{field, message}]`. SMALLINT chỉ là 1 số integer → không thể lưu được. **Chắc chắn typo** — prob nhầm column kế bên. |

**SQL:**
```sql
ALTER TABLE scenario_revisions
    MODIFY COLUMN last_test_run_status    VARCHAR(16) NULL,
    MODIFY COLUMN last_test_run_at        DATETIME(6) NULL,
    MODIFY COLUMN static_validation_errors JSON NULL;
```

**Ảnh hưởng:** Không. Bảng rỗng. Nếu có data thật phải migrate.

---

### 1.3 Thêm cột bị thiếu: `clone_source_revision_id`

**Hiện tại:** `scenario_revisions` có `parent_revision_id` nhưng không có `clone_source_revision_id`.

**Đổi:** Thêm cột `clone_source_revision_id BIGINT NULL`.

**Lý do:**
- Scenario cho phép **clone**: user copy 1 scenario builtin (ví dụ `chang_login`) → tạo scenario riêng để sửa.
- `parent_revision_id` = revision TRƯỚC đó trong CÙNG scenario (chain edit trong 1 scenario). Ví dụ: v3 của scenario X có parent = v2 của X.
- `clone_source_revision_id` = revision NGUỒN khi **clone** (chain cross-scenario). Ví dụ: rev 1 của scenario `user_hiepqn_copy_chang_login` có clone_source = rev 1 của `chang_login` (builtin).
- Tách 2 field vì 2 khái niệm khác nhau:
  - `parent`: luôn same-scenario (edit history)
  - `clone_source`: cross-scenario (fork lineage, immutable sau khi set)

Không có cột này → tính năng clone (đã code + test) không lưu được gốc gác → mất audit trail.

**SQL:**
```sql
ALTER TABLE scenario_revisions
    ADD COLUMN clone_source_revision_id BIGINT NULL AFTER parent_revision_id;
```

**Ảnh hưởng:** Không. Thêm column mới, default NULL.

---

## 2. 🟡 TIER 2 — RECOMMENDED

Đề xuất đổi type/thêm column để **code đơn giản và performance tốt hơn**. Nếu không đổi, code vẫn work được nhưng phải thêm mapping layer.

### 2.1 Đổi enum columns từ `SMALLINT` → `VARCHAR`

**Các cột bị ảnh hưởng:**

| Bảng | Cột | Hiện tại | Đổi thành | Giá trị hợp lệ |
|---|---|---|---|---|
| `scenario_definitions` | `source_type` | SMALLINT NULL | VARCHAR(16) NOT NULL | `builtin` / `user` / `cloned` |
| `scenario_definitions` | `visibility` | VARCHAR(20) NULL | VARCHAR(16) NOT NULL DEFAULT 'private' | `private` / `org` / `public` |
| `scenario_revisions` | `static_validation_status` | SMALLINT NOT NULL | VARCHAR(16) NOT NULL | `pending` / `passed` / `failed` |
| `scenario_runs` | `status` | SMALLINT NOT NULL | VARCHAR(16) NOT NULL | `running` / `completed` / `failed` / `cancelled` |
| `scenario_runs` | `mode` | VARCHAR(20) NULL | VARCHAR(16) NOT NULL | `production` / `test` |

**Lý do chọn VARCHAR thay vì SMALLINT:**

**Pros của VARCHAR:**
- **Readable khi query Workbench:** `SELECT source_type FROM ...` trả 'builtin' ngay, không cần JOIN lookup table
- **Code Python đơn giản:** `spec.source_type = 'builtin'` — không cần enum class + mapping dict
- **Self-documenting:** đọc row là hiểu, không cần đọc bảng code mapping (1=builtin, 2=user, ...)
- **Dễ thêm value mới:** Phase 2 muốn thêm `source_type='shared'` → insert thẳng, không cần update mapping table
- **Search text ngay được:** `WHERE source_type = 'user'` vs `WHERE source_type = 2`
- **Index hiệu quả:** VARCHAR(16) nhỏ, CHAR_LENGTH ≤ 16 → index footprint nhỏ

**Cons của VARCHAR vs SMALLINT:**
- Lưu trữ: 16 bytes max vs 2 bytes → chênh ~14 bytes/row × N rows.
- Với scale < 1M rows → tổng overhead < 14MB, không đáng kể.

**Nếu keep SMALLINT:** code phải viết thêm mapping:
```python
SOURCE_TYPE_MAP = {1: 'builtin', 2: 'user', 3: 'cloned'}
SOURCE_TYPE_REVERSE = {v: k for k, v in SOURCE_TYPE_MAP.items()}
# Mỗi read: convert int → str
# Mỗi write: convert str → int
# Nếu thêm loại mới → update code + DB constant cùng lúc
```
→ Thêm ~5 mapping constants, ~20 dòng code boilerplate. Test phải cover. Và **khi debug qua Workbench phải nhớ code = gì** — phiền.

**SQL:**
```sql
ALTER TABLE scenario_definitions
    MODIFY COLUMN source_type VARCHAR(16) NOT NULL,
    MODIFY COLUMN visibility  VARCHAR(16) NOT NULL DEFAULT 'private';

ALTER TABLE scenario_revisions
    MODIFY COLUMN static_validation_status VARCHAR(16) NOT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN status      VARCHAR(16) NOT NULL,
    MODIFY COLUMN mode        VARCHAR(16) NOT NULL;
```

---

### 2.2 Đổi text fields lớn → type đúng (LONGTEXT / JSON / CHAR)

| Bảng | Cột | Hiện tại | Đổi thành | Lý do |
|---|---|---|---|---|
| `scenario_revisions` | `raw_yaml` | TEXT | LONGTEXT | TEXT max 64KB. YAML scenario phức tạp có thể > 64KB (vd 100 steps). LONGTEXT max 4GB — overhead lưu trữ chỉ +1 byte header. |
| `scenario_revisions` | `normalized_spec_json` | TEXT | JSON | MariaDB 10.2+ có JSON native type. Hỗ trợ `JSON_EXTRACT`, validate JSON schema tại insert, index được theo field. TEXT không có. |
| `scenario_revisions` | `yaml_hash` | TEXT | CHAR(64) | Field này là sha256 hex → **đúng 64 ký tự cố định**. CHAR(64) → fixed-width, index nhanh hơn, lookup `WHERE yaml_hash = '...'` cực nhanh (dùng detect duplicate YAML). TEXT→index kém + cần KEY length prefix. |
| `scenario_runs` | `runtime_policy_snapshot` | TEXT | JSON | Tương tự `normalized_spec_json`, lưu policy snapshot JSON. |

**SQL:**
```sql
ALTER TABLE scenario_revisions
    MODIFY COLUMN raw_yaml             LONGTEXT   NOT NULL,
    MODIFY COLUMN normalized_spec_json JSON       NOT NULL,
    MODIFY COLUMN yaml_hash            CHAR(64)   NOT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN runtime_policy_snapshot JSON NOT NULL;
```

---

### 2.3 Đổi FK column type: INT → BIGINT

**Các cột ảnh hưởng:**

| Bảng | Cột | Hiện tại | Đổi thành |
|---|---|---|---|
| `scenario_definitions` | `published_revision_id` | INT NULL | BIGINT NULL |
| `scenario_revisions` | `parent_revision_id` | INT NULL | BIGINT NULL |
| `scenario_runs` | `revision_id` | INT NULL | BIGINT NOT NULL |

**Lý do:**
- `scenario_revisions.id` là `BIGINT AUTO_INCREMENT` → tất cả FK trỏ tới phải cùng type BIGINT, không phải INT.
- INT max = 2.1 tỷ. Scale lâu dài (audit log revisions nhiều năm × N users) có thể vượt.
- FK phải same type để MySQL index đúng — nếu mismatch → query plan kém (implicit cast).

**SQL:**
```sql
ALTER TABLE scenario_definitions
    MODIFY COLUMN published_revision_id BIGINT NULL;

ALTER TABLE scenario_revisions
    MODIFY COLUMN parent_revision_id BIGINT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN revision_id BIGINT NOT NULL;
```

---

### 2.4 `scenario_runs.session_id` INT → VARCHAR(64)

**Hiện tại:** `session_id INT NULL`

**Đổi:** `session_id VARCHAR(64) NOT NULL`

**Lý do:**
- `session_id` là UUID dạng chuỗi (vd `9ee0143d-f924-4ef9-a894-3817188242c0`) từ API `/v1/sessions` response — không phải số nguyên.
- Dùng UUID string thay int vì tránh collision khi scale, dễ debug (session id thấy trong URL, log, Redis).
- NOT NULL vì mọi run đều PHẢI có session_id (link sang Redis session state).

**SQL:**
```sql
ALTER TABLE scenario_runs
    MODIFY COLUMN session_id VARCHAR(64) NOT NULL;
```

---

### 2.5 Thêm `code` column cho scenario_definitions

**Hiện tại:** chỉ có `id BIGINT AUTO_INCREMENT` (surrogate key).

**Đổi:** thêm `code VARCHAR(64) NOT NULL UNIQUE AFTER id`.

**Lý do:**
- Code cần 1 **business key** dạng string để user/URL/config reference. Vd:
  - `user_hiepqn_tim_luat` (user-created)
  - `chang_login` (builtin)
  - `search_thuvienphapluat` (builtin)
- UI dropdown, API URL `/v1/scenarios/{code}`, YAML `id:` field — tất cả dùng code.
- Integer id (bigint auto) tốt cho FK internal nhưng không share được với user.
- UNIQUE constraint đảm bảo code duy nhất trong DB (tránh 2 user cùng tên scenario).

**Pattern này chuẩn:** `id` (surrogate, FK-friendly) + `code`/`slug` (business, unique, human-readable) — phổ biến trong Spring Boot, Django.

**SQL:**
```sql
ALTER TABLE scenario_definitions
    ADD COLUMN code VARCHAR(64) NOT NULL UNIQUE AFTER id;
```

---

### 2.6 Thêm `owner_code` column (Phase 1 mock auth)

**Hiện tại:** `owner_id INT NULL` — expect FK tới `users(id)`.

**Đổi:** thêm `owner_code VARCHAR(64) NULL AFTER owner_id`.

**Lý do:**
- Phase 1 dùng **mock auth** — user identity = chuỗi string (vd `hiepqn`) gửi qua header `X-User-Id`. CHƯA có lookup users table.
- Code không thể map "hiepqn" string → int users.id vì chưa có integration.
- Giải pháp Phase 1: lưu user code string vào `owner_code`, để `owner_id` NULL.
- Phase 2 khi integrate với real auth (JWT + users table) → populate `owner_id`, `owner_code` deprecated (optional giữ cho backward compat).

Không thêm column này thì code Phase 1 không lưu được ownership → không enforce được permission (owner mới edit được).

**SQL:**
```sql
ALTER TABLE scenario_definitions
    ADD COLUMN owner_code VARCHAR(64) NULL AFTER owner_id;
```

---

### 2.7 Nullable audit columns: `created_by`, `updated_by`, `started_by`

**Hiện tại:**
```
scenario_definitions.created_by  BIGINT NOT NULL
scenario_definitions.updated_by  BIGINT NOT NULL
scenario_revisions.created_by    BIGINT NOT NULL
scenario_revisions.updated_by    BIGINT NOT NULL
scenario_runs.started_by         BIGINT NOT NULL
scenario_runs.created_by         BIGINT NOT NULL
scenario_runs.updated_by         BIGINT NOT NULL
```

**Đổi:** tất cả → `BIGINT NULL`.

**Lý do:**
- Phase 1 mock auth không có int user id → code không có giá trị để insert.
- Workarounds nếu giữ NOT NULL:
  - (a) Insert `0` sentinel: không đúng với users table FK sau này.
  - (b) Tạo user "system" (id=1) trong users table, hardcode ID: phức tạp, phụ thuộc state table khác.
  - (c) Make nullable: đơn giản nhất.

- Phase 2 khi auth thật → code populate user id đúng → các row mới có giá trị; row Phase 1 vẫn NULL (migrate/backfill được nếu cần).

**SQL:**
```sql
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
```

---

### 2.8 Thêm UNIQUE constraint: `(scenario_id, version_no)`

**Hiện tại:** không có UNIQUE — 2 revision cùng scenario có thể có version_no trùng → data inconsistent.

**Đổi:**
```sql
ALTER TABLE scenario_revisions
    ADD UNIQUE KEY uq_rev_version (scenario_id, version_no);
```

**Lý do:**
- Code tạo revision với `version_no = MAX(version_no) + 1` trong transaction → đảm bảo unique tại tầng code.
- UNIQUE constraint là **defense-in-depth** — nếu 2 tab user save đồng thời → 1 thành công, 1 fail với UNIQUE violation → retry.
- Không có UNIQUE → race condition → 2 rev cùng v=3, corrupted history.

---

### 2.9 Thêm indexes cho query pattern phổ biến

**Lý do:** Scale lên, query không có index → full table scan.

**SQL:**
```sql
ALTER TABLE scenario_definitions
    ADD INDEX idx_def_owner (owner_id, is_archived),
    ADD INDEX idx_def_source (source_type, is_archived);

ALTER TABLE scenario_revisions
    ADD INDEX idx_rev_hash (scenario_id, yaml_hash);

ALTER TABLE scenario_runs
    ADD INDEX idx_run_session (session_id);
```

**Giải thích từng index:**

- `idx_def_owner (owner_id, is_archived)`: Query `List scenarios của user X, ẩn archived` — thường xuyên chạy.
- `idx_def_source (source_type, is_archived)`: Query `List builtin scenarios` — UI dropdown main.
- `idx_rev_hash (scenario_id, yaml_hash)`: Detect no-op save (user save cùng YAML 2 lần) bằng cách check hash trùng rev gần nhất.
- `idx_run_session (session_id)`: Worker update `scenario_runs.status` by session_id → lookup nhanh.

---

## 3. 🟢 TIER 3 — OPTIONAL (Phase 2 có thể làm)

Không bắt buộc ship Phase 1, nhưng tốt cho data integrity về lâu dài.

### 3.1 Foreign Keys với CASCADE

```sql
ALTER TABLE scenario_revisions
    ADD CONSTRAINT fk_rev_scenario
        FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id) ON DELETE CASCADE;

ALTER TABLE scenario_runs
    ADD CONSTRAINT fk_run_scenario
        FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id),
    ADD CONSTRAINT fk_run_revision
        FOREIGN KEY (revision_id) REFERENCES scenario_revisions(id);
```

**Lý do:**
- Nếu xoá 1 scenario_definitions → cascade xoá revisions + runs liên quan → không để orphan row.
- Nếu không có FK → phải enforce tại code → rủi ro quên delete children.

**Rủi ro:** CASCADE trên production nếu gọi nhầm `DELETE FROM scenario_definitions` có thể xoá nhiều data. Mitigate: bảng có `is_archived` (soft delete), hardly delete thật.

### 3.2 CHECK constraints (MariaDB 10.2.1+ support)

```sql
ALTER TABLE scenario_definitions
    ADD CONSTRAINT chk_def_source CHECK (source_type IN ('builtin','user','cloned')),
    ADD CONSTRAINT chk_def_visibility CHECK (visibility IN ('private','org','public'));

ALTER TABLE scenario_revisions
    ADD CONSTRAINT chk_rev_status CHECK (static_validation_status IN ('pending','passed','failed'));

ALTER TABLE scenario_runs
    ADD CONSTRAINT chk_run_mode CHECK (mode IN ('production','test')),
    ADD CONSTRAINT chk_run_status CHECK (status IN ('running','completed','failed','cancelled'));
```

**Lý do:**
- Enforce enum values tại DB layer → ngăn chặn typo từ bất kỳ client nào (không chỉ code Python).
- Defense-in-depth — nếu ai đó insert thẳng qua Workbench với sai value, DB reject.

**Lưu ý:** MariaDB 10.2.1+ enforce CHECK. Nếu version cũ hơn, sẽ syntax OK nhưng không enforce.

---

## 4. 📋 Tổng hợp SQL — copy nguyên block gửi DBA

### Bundle TIER 1 + TIER 2 (khuyến nghị merge hết)

```sql
USE changchatbot;

-- ═════════════════════════════════════════════════════════════════
-- TIER 1: Fix bugs (typo + sai type + missing column)
-- ═════════════════════════════════════════════════════════════════

-- 1.1 Fix typo column name
ALTER TABLE scenario_definitions
    CHANGE COLUMN ` date_created` date_created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE scenario_revisions
    CHANGE COLUMN ` date_created` date_created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

-- 1.2 Fix sai type
ALTER TABLE scenario_revisions
    MODIFY COLUMN last_test_run_status    VARCHAR(16) NULL,
    MODIFY COLUMN last_test_run_at        DATETIME(6) NULL,
    MODIFY COLUMN static_validation_errors JSON NULL;

-- 1.3 Thêm column thiếu
ALTER TABLE scenario_revisions
    ADD COLUMN clone_source_revision_id BIGINT NULL AFTER parent_revision_id;

-- ═════════════════════════════════════════════════════════════════
-- TIER 2: Recommended (giữ code gọn)
-- ═════════════════════════════════════════════════════════════════

-- 2.1 Enum → VARCHAR
ALTER TABLE scenario_definitions
    MODIFY COLUMN source_type VARCHAR(16) NOT NULL,
    MODIFY COLUMN visibility  VARCHAR(16) NOT NULL DEFAULT 'private';

ALTER TABLE scenario_revisions
    MODIFY COLUMN static_validation_status VARCHAR(16) NOT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN status VARCHAR(16) NOT NULL,
    MODIFY COLUMN mode   VARCHAR(16) NOT NULL;

-- 2.2 Text fields → đúng type
ALTER TABLE scenario_revisions
    MODIFY COLUMN raw_yaml             LONGTEXT NOT NULL,
    MODIFY COLUMN normalized_spec_json JSON     NOT NULL,
    MODIFY COLUMN yaml_hash            CHAR(64) NOT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN runtime_policy_snapshot JSON NOT NULL;

-- 2.3 FK columns INT → BIGINT
ALTER TABLE scenario_definitions
    MODIFY COLUMN published_revision_id BIGINT NULL;

ALTER TABLE scenario_revisions
    MODIFY COLUMN parent_revision_id BIGINT NULL;

ALTER TABLE scenario_runs
    MODIFY COLUMN revision_id BIGINT NOT NULL;

-- 2.4 session_id INT → VARCHAR
ALTER TABLE scenario_runs
    MODIFY COLUMN session_id VARCHAR(64) NOT NULL;

-- 2.5 Thêm business key: code
ALTER TABLE scenario_definitions
    ADD COLUMN code VARCHAR(64) NOT NULL UNIQUE AFTER id;

-- 2.6 Thêm owner_code cho mock auth
ALTER TABLE scenario_definitions
    ADD COLUMN owner_code VARCHAR(64) NULL AFTER owner_id;

-- 2.7 Nullable audit columns
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

-- 2.8 UNIQUE (scenario_id, version_no)
ALTER TABLE scenario_revisions
    ADD UNIQUE KEY uq_rev_version (scenario_id, version_no);

-- 2.9 Indexes cho query pattern
ALTER TABLE scenario_definitions
    ADD INDEX idx_def_owner (owner_id, is_archived),
    ADD INDEX idx_def_source (source_type, is_archived);

ALTER TABLE scenario_revisions
    ADD INDEX idx_rev_hash (scenario_id, yaml_hash);

ALTER TABLE scenario_runs
    ADD INDEX idx_run_session (session_id);
```

### Bundle TIER 3 (optional, Phase 2)

```sql
-- Foreign keys
ALTER TABLE scenario_revisions
    ADD CONSTRAINT fk_rev_scenario
        FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id) ON DELETE CASCADE;

ALTER TABLE scenario_runs
    ADD CONSTRAINT fk_run_scenario
        FOREIGN KEY (scenario_id) REFERENCES scenario_definitions(id),
    ADD CONSTRAINT fk_run_revision
        FOREIGN KEY (revision_id) REFERENCES scenario_revisions(id);

-- CHECK constraints (MariaDB 10.2.1+)
ALTER TABLE scenario_definitions
    ADD CONSTRAINT chk_def_source CHECK (source_type IN ('builtin','user','cloned')),
    ADD CONSTRAINT chk_def_visibility CHECK (visibility IN ('private','org','public'));

ALTER TABLE scenario_revisions
    ADD CONSTRAINT chk_rev_status CHECK (static_validation_status IN ('pending','passed','failed'));

ALTER TABLE scenario_runs
    ADD CONSTRAINT chk_run_mode CHECK (mode IN ('production','test')),
    ADD CONSTRAINT chk_run_status CHECK (status IN ('running','completed','failed','cancelled'));
```

---

## 5. 📊 Tóm tắt thay đổi

### Bảng tổng hợp số lượng thay đổi

| Tier | Số ALTER statement | Số column thay đổi | Bắt buộc? |
|---|---|---|---|
| TIER 1 | 4 | 5 (rename 2, type 3) + 1 thêm | ✅ Phase 1 không work nếu không fix |
| TIER 2 | 10 | 15 column type + 2 column mới + 1 UNIQUE + 4 index | 🟡 Khuyến nghị để code gọn |
| TIER 3 | 4 | 3 FK + 5 CHECK | 🟢 Phase 2 làm cũng được |

### Rủi ro & backup

- Các bảng hiện **rỗng** (0 data) → ALTER an toàn, không cần backup row.
- Nếu DBA muốn backup trước để chắc:
  ```bash
  mysqldump -h 172.28.8.11 -u chatbotadmin -p \
    changchatbot scenario_definitions scenario_revisions scenario_runs \
    > backup_scenarios_$(date +%Y%m%d).sql
  ```

### Thời gian apply

- Bảng rỗng → mỗi ALTER < 1 giây.
- Tổng: < 1 phút.

---

## 6. ❓ FAQ cho DBA

### Q1: Tại sao không dùng UUID cho `id` thay vì BIGINT AUTO_INCREMENT?

**A:** DB hiện tại đã BIGINT AUTO_INCREMENT, respect decision đó. Code adapt được (thêm `code` làm business key). UUID tốt cho distributed system nhưng overhead 16 bytes + không sort naturally.

### Q2: `JSON` type có hỗ trợ đủ rộng không?

**A:** MariaDB 10.2+ có JSON alias cho LONGTEXT + validate JSON syntax. Functions `JSON_EXTRACT`, `JSON_VALID`, `JSON_OBJECT` đều work. Tương tự MySQL 5.7+.

### Q3: Tại sao không migrate hẳn `owner_id` sang int FK users?

**A:** Phase 1 mock auth không có users table integration. Phase 2 mới setup JWT + users table, lúc đó migrate. Hiện dùng `owner_code` string làm bridge.

### Q4: Có thể skip TIER 2 không?

**A:** Có, nhưng:
- SMALLINT enum: code phải có mapping layer (thêm ~50 dòng + test).
- TEXT json: không validate JSON tại DB; code phải trust input.
- INT cho FK: mismatch với BIGINT PK, implicit cast, index kém.
- Thiếu `code`: code phải generate URL từ int id → không human-readable.

→ Skip TIER 2 = code phức tạp hơn + perf kém. Nhưng vẫn ship được Phase 1.

### Q5: Bao lâu nữa sẽ cần thay đổi thêm?

**A:** Phase 2 (multi-user real auth):
- `owner_id` BIGINT FK users(id) → populate
- `created_by`, `updated_by` BIGINT FK users(id) → populate
- Có thể drop `owner_code` nếu toàn bộ migration xong

Không cần thêm column mới. Chỉ populate dữ liệu.

---

## 7. 🎯 Ask to DBA

1. Approve **TIER 1** (bugs) — ít nhất phải có để Phase 1 work.
2. Approve **TIER 2** (recommended) — khuyến nghị merge luôn để tiết kiệm code boilerplate.
3. **TIER 3** Phase 2 — có thể schedule sau.

Sau khi DBA chạy SQL xong, mình sẽ:
- Chạy `inspect_mysql_schema.py` lại để verify
- Viết `MysqlScenarioRepo` adapt với schema final
- Test integration trên dev → sync sang product_build

Cảm ơn DBA. 🙏
