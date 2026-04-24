# Plan: User-Configurable Scenario System

> **Status:** DRAFT v3 — tất cả quyết định Phase 1 đã chốt, sẵn sàng code
> **Created:** 2026-04-23
> **Updated:** 2026-04-24 (chốt auth=mock, editor=textarea, no deadline; đóng G1-G7)
> **Context:** Mở rộng product để user có thể tự tùy chỉnh YAML scenario → input qua UI → chạy.

---

## 0. Mục tiêu & bối cảnh

### Hiện tại (sau khi merge commit `4658522`)
- Scenarios dạng declarative YAML (flow v2) — chứa `inputs[]`, `steps[]`, `success`, `failure`
- Lưu trong Docker image tại `LLM_base/scenarios/builtin/*.yaml`
- Seed vào Redis lúc API startup (idempotent)
- Admin CRUD qua `POST/PUT/DELETE /v1/scenarios` với `X-Admin-Token`
- UI laptop (web_UI_test) render form động theo `spec.inputs[]`

### Hướng đi mong muốn
- User **không phải admin** cũng tạo/sửa scenario được
- UI cho user upload/paste YAML, validate, rồi dùng luôn
- Dùng form động (đã có) để input context → chạy

### Câu hỏi chưa chốt (ảnh hưởng lớn đến plan)

| # | Câu hỏi | Ảnh hưởng |
|---|---|---|
| Q1 | "User" là ai — end-user non-tech, hay dev/admin nội bộ FPT? | Quyết định mode authoring (wizard vs editor) |
| Q2 | Multi-tenant (nhiều org) hay 1 org nhiều user? | DB schema, auth model |
| Q3 | User tạo scenario mới hay chỉ tùy chỉnh template? | Độ phức tạp UI khác nhiều |
| Q4 | Hooks Python (như `chang_login.pre_check`) — cho user custom không? | Security — hooks là code executable |
| Q5 | Auth: dùng SSO FPT luôn hay mock `X-User-Id` giai đoạn đầu? | Tiến độ Phase 1 |
| Q6 | `allowed_domains` — user tự khai báo hay admin whitelist trước? | Security domain isolation |
| Q7 | YAML-only hay cho phép JSON tương đương? | UX editor |
| Q8 | Deadline Phase 1? | Scope cắt giảm tới đâu |

**Quyết định đã chốt:**
- Q1: ✅ dev/admin nội bộ FPT (biết YAML) → Phase 1 đi thẳng YAML editor, không wizard
- Q2: single-tenant trước, multi-tenant tính sau
- Q3: tạo mới + clone builtin để sửa; clone = fork-and-forget (không track upstream)
- Q4: ✅ không cho custom hooks, chỉ chọn từ HOOK_REGISTRY whitelist
- Q6: admin whitelist domain → scenario user dùng domain phải subset
- Q7: YAML-first lưu song song normalized JSON (xem §1.1)
- **Publish & builtin update:** không có API publish; admin (người vận hành) thực hiện
  qua SQL trực tiếp. Builtin cũng read-only qua API, admin sửa qua SQL/migration.
  Phase 2 mới cân nhắc self-serve publish qua API.

**Đã chốt session 2026-04-24:**
- Q5 (auth): ✅ `AUTH_PROVIDER=mock` — chỉ laptop dev, 1 user. Header `X-User-Id` = `hiepqn`.
  Plug shared_secret/JWT khi nào có nhu cầu share.
- Q8 (deadline): ✅ không gấp. Test từng giai đoạn (interfaces → impl → routes → UI), gate mỗi bước.
- Phase 1 deploy env: laptop-only. Không cần thiết kế 2 kịch bản; giữ pluggable để sau mở rộng.
- Editor UI: ✅ textarea thường. Không dùng Monaco (tiết kiệm ~2MB bundle + 0.5 ngày dev).
  Syntax validate chỉ server-side, client chỉ gửi text + hiển thị lỗi backend trả về.

---

## 1. Kiến trúc đề xuất

### 1.1 Storage — nguyên tắc & flow

**Nguyên tắc chốt:**
- YAML là format authoring, user sửa trên UI
- DB là source of truth cho scenario
- Runtime **không** chạy trực tiếp YAML, mà chạy normalized spec đã validate
- Redis chỉ làm cache đọc + runtime/session state, **không** phải primary store

**Flow chuẩn:**
```
User/UI
  → upload/paste/clone YAML
  → Backend parse + static-validate + normalize
  → lưu DB (cả raw YAML + normalized JSON, như 1 revision mới)
  → user test run (dùng revision bất kỳ, owner-only)
  → admin publish qua SQL (set published_revision_id)
  → Runtime đọc published revision để chạy production
```

**Tại sao lưu cả raw YAML + normalized JSON:**
- Raw YAML: editor round-trip (user thấy lại đúng format mình nhập), debugging parse lỗi
- Normalized JSON: runtime chạy không phải parse lại, ổn định khi parser/schema đổi

### 1.2 Schema — 3 bảng

#### `scenario_definitions` — metadata 1 scenario

| Field | Type | Ghi chú |
|---|---|---|
| `id` | varchar(64) PK | ví dụ `builtin_tim_kiem_luat`, `user_abc_xyz` |
| `name` | varchar | display name |
| `owner_id` | varchar(64) nullable | NULL cho builtin |
| `org_id` | varchar(64) nullable | Phase 2+ |
| `source_type` | varchar(16) | `builtin` \| `user` \| `cloned` |
| `visibility` | varchar(16) | `private` \| `org` \| `public` |
| `published_revision_id` | bigint nullable | FK → scenario_revisions; NULL = chưa publish |
| `is_archived` | bool default false | soft hide |
| `created_at` / `updated_at` | timestamptz | |

**Không có field `status`** — trạng thái lifecycle suy ra từ `published_revision_id` (NULL/set) và `is_archived`.

#### `scenario_revisions` — mỗi save = 1 revision immutable

| Field | Type | Ghi chú |
|---|---|---|
| `id` | bigint PK | |
| `scenario_id` | varchar(64) FK | |
| `version_no` | int | tăng dần trong cùng scenario_id |
| `raw_yaml` | text | user input gốc |
| `normalized_spec_json` | jsonb | sau parse+validate+normalize |
| `yaml_hash` | char(64) | sha256(raw_yaml) — detect save không đổi gì |
| `parent_revision_id` | bigint nullable FK self | rev trước trong CÙNG scenario (edit chain) |
| `clone_source_revision_id` | bigint nullable FK self | rev gốc khi clone từ scenario khác (set 1 lần ở rev 1 của cloned scenario, immutable) |
| `schema_version` | int | DSL version của spec |
| `static_validation_status` | varchar(16) | `pending` \| `passed` \| `failed` |
| `static_validation_errors` | jsonb nullable | |
| `last_test_run_at` | timestamptz nullable | |
| `last_test_run_status` | varchar(16) nullable | `passed` \| `failed` |
| `last_test_run_id` | bigint nullable FK → scenario_runs | |
| `created_by` | varchar(64) | |
| `created_at` | timestamptz | |

**`parent_revision_id` vs `clone_source_revision_id` (G4):**
- `parent_revision_id`: chain edit trong cùng 1 scenario. Rev 1 → parent=NULL. Rev 2 → parent=rev 1. Dùng để diff 2 rev liên tiếp.
- `clone_source_revision_id`: chỉ set ở rev 1 của scenario `source_type='cloned'`, trỏ về rev gốc của scenario khác. Immutable — đánh dấu "ai đẻ ra mình".

Unique: `(scenario_id, version_no)`.

**Save semantics:**
- Mỗi lần user bấm "Save" → tạo revision mới với `version_no = max+1`.
- Nếu `yaml_hash` trùng revision mới nhất → reject no-op save (client hiển thị "không có thay đổi").
- **Không** update-in-place revision đã tồn tại. Revision là immutable.
- Ngoại lệ: `last_test_run_*` fields được update khi có test run mới (đây là metadata, không phải spec content).

#### `scenario_runs` — mỗi session chạy ghi 1 row

| Field | Type | Ghi chú |
|---|---|---|
| `id` | bigint PK | |
| `scenario_id` | varchar(64) FK | |
| `revision_id` | bigint FK | **pin cứng** — session chạy rev nào thì rev đó |
| `session_id` | varchar | link sang runtime session |
| `mode` | varchar(16) | `production` \| `test` |
| `started_by` | varchar(64) | |
| `runtime_policy_snapshot` | jsonb | allowed_domains + quota + hook whitelist tại thời điểm start |
| `status` | varchar(16) | `running` \| `completed` \| `failed` \| `cancelled` |
| `created_at` | timestamptz | |

**Runtime policy snapshot** lấy tại thời điểm session start (không phải publish), vì org policy có thể đổi giữa publish và run.

### 1.3 Storage engine — pluggable

Tách **schema** (cố định) khỏi **engine** (có thể swap):

```
ScenarioRepository (interface)
  ├── SqliteScenarioRepo    # Phase 1 mặc định — file, 0 ops cost
  └── PostgresScenarioRepo  # Phase 2+ — nếu infra sẵn / scale
```

Schema y hệt giữa 2 engine (SQL chuẩn, jsonb → TEXT/JSON ở SQLite). Service layer chỉ phụ thuộc interface → đổi engine = đổi DI, không rewrite logic.

**Không dùng Redis làm store.** Redis chỉ:
- Cache read (§5)
- Runtime session state (đã có)
- Rate limit counter

### 1.4 Ba nguồn scenario — cùng một model

| Nguồn | `source_type` | `owner_id` | Edit qua API | Publish |
|---|---|---|---|---|
| Builtin | `builtin` | NULL | ❌ (admin sửa qua SQL) | admin set `published_revision_id` qua SQL |
| User tạo | `user` | user_xxx | ✅ owner | admin set qua SQL |
| Clone từ builtin | `cloned` | user_xxx | ✅ owner, `parent_revision_id` trỏ về builtin rev | admin set qua SQL |

Từ góc nhìn query/code: **cùng một bảng, cùng một model** — chỉ khác nguồn gốc. Clone = copy revision content sang scenario mới, ghi `parent_revision_id`.

**Seed builtin (G2):** Auto-run lúc API startup nếu DB rỗng.
```python
# api/app.py startup hook
if await repo.count_builtin() == 0:
    run_sql_script("scripts/seed_builtin.sql")  # idempotent, chỉ chạy lần đầu
```
Sau đó admin chỉ can thiệp qua SQL trực tiếp để update builtin. Không auto-sync từ YAML file → DB (tránh surprise override).

### 1.5 Endpoint run — production vs test, không cần flag

```
POST /v1/sessions
  body: { scenario_id, revision_id?: bigint, inputs: {...} }
  
  - revision_id không truyền → dùng published_revision_id (production).
    Nếu scenario chưa publish → 409 Conflict.
  - revision_id truyền rõ → "test run":
      * caller PHẢI là owner của scenario
      * ghi scenario_runs.mode = 'test'
  - revision_id = published_revision_id → mode = 'production'
```

Owner-only cho test run là quan trọng: draft có thể đang sai/có side effect. Non-owner không được chạy draft của người khác.

### 1.6 Tầng UI — authoring mode

| Mode | Phase | UX |
|---|---|---|
| **A. Upload YAML** | 1 | Drag-drop file `.yaml` → validate → save draft |
| **B. Monaco editor** | 1 | Paste/edit YAML với syntax + JSON schema auto-complete |
| **C. Visual builder** | 3 (nếu có demand) | Drag-drop steps, wizard — chưa làm |

User = dev/admin FPT biết YAML → Phase 1 đi thẳng A+B, bỏ C.

---

## 2. Roadmap 3 Phase

### Phase 1 — MVP (1-2 tuần)

**Mục tiêu:** User upload/paste YAML → static validate → save draft → test run → (admin publish qua SQL) → production run.

**Backend (`dev/deploy_server/`):**

```
# Scenario CRUD
POST   /v1/scenarios/validate     → static validate YAML, không lưu DB
POST   /v1/scenarios              → tạo scenario mới + revision đầu
PUT    /v1/scenarios/{id}         → tạo revision mới (owner-only)
GET    /v1/scenarios              → list (filter by owner/source_type/is_archived)
GET    /v1/scenarios/{id}         → metadata + latest revision
GET    /v1/scenarios/{id}/revisions          → list revisions (paginate default 20 mới nhất)
GET    /v1/scenarios/{id}/revisions/{rev_id} → full revision content
DELETE /v1/scenarios/{id}         → soft archive (set is_archived=true)

# Session run — gộp production & test vào 1 endpoint
POST   /v1/sessions
  body: { scenario_id, revision_id?, inputs }
  - revision_id omitted → production (dùng published_revision_id)
  - revision_id set     → test run (owner-only)
  - cả 2 mode đều chạy browser thật; chỉ khác scenario_runs.mode
  - G5: inputs validate server-side theo revision.normalized_spec_json.inputs[]
    (check required, type coerce); fail → 400 với error detail

# Hooks registry — phục vụ UI hint/validate
GET    /v1/hooks                   → list tên hook trong HOOK_REGISTRY (G7)
  response: [{name, description, accepts}]

# KHÔNG có trong Phase 1:
# - POST /v1/scenarios/{id}/publish  (admin làm qua SQL)
# - PUT/DELETE lên builtin           (read-only qua API)
# - Dry-run compile (không chạy browser) → Phase 2+ nếu cần
```

**Save semantics — cho phép lưu revision lỗi validate:**
- `POST /scenarios` và `PUT /scenarios/{id}` **luôn tạo revision**, kể cả khi static validate fail.
- Revision fail: `static_validation_status='failed'` + `static_validation_errors` chứa chi tiết.
- Rule: revision `failed` **không thể** trở thành `published_revision_id` (enforce ở publish path = SQL script kiểm tra trước UPDATE; Phase 2 enforce ở API).
- Lý do giữ: user save dở rồi quay lại sửa tiếp tiện hơn phải paste lại; revision immutable nên rác cũng không ảnh hưởng.
- Ngoại lệ: YAML parse fail (syntax lỗi nặng) → hard-reject 400, không tạo revision (không có nội dung để lưu).

**Data model:** xem §1.2. Phase 1 chạy SQLite (file-based, 0 ops cost). Schema y hệt Phase 2 Postgres → migration chỉ là đổi connection, không rewrite.

**Async driver (G1):** FastAPI async → dùng `aiosqlite` (không phải `sqlite3` sync), tránh block event loop. Requirements: `aiosqlite>=0.19`. SQL giữ chuẩn (không dùng SQLite-only syntax) để Phase 2 paste sang Postgres không đổi.

**Worker DB access (G6):** Worker cập nhật `scenario_runs.status` khi session done/failed/cancelled. Phase 1: worker mở direct DB connection (SQLite cùng file với API — chấp nhận vì single-node). Phase 2 Postgres: dùng shared connection pool qua env `DB_URL`.

**⚠️ SQLite constraints Phase 1 (cần chấp nhận):**
- **Chỉ 1 replica API.** SQLite không share giữa pods. K8s không được auto-scale.
- **Downtime vài phút khi restart** chấp nhận được (lock file có thể mắc kẹt).
- **Backup qua `sqlite3 .dump`**, không phải pg_dump network.
- → Nếu deploy môi trường yêu cầu HA / >10 user concurrent → **nhảy thẳng Phase 2 Postgres**, skip SQLite.
- DB infra cụ thể: **chờ user cung cấp connection thông tin, sẽ adjust sau.**

**Auth Phase 1 (chốt):**
- `AUTH_PROVIDER=mock` — đọc `X-User-Id` header (default `hiepqn` cho local test)
- Guard: refuse khởi động nếu `ENV=production` và `AUTH_PROVIDER=mock` (fail-safe)
- Các provider `shared_secret`/`jwt` giữ interface sẵn nhưng chưa implement Phase 1.

**Frontend (`dev/web_UI_test/`):**
- Trang `/scenarios` — list (filter: mine / builtin / archived)
- Modal "➕ New scenario":
  - Tab 1: Upload YAML file (drag-drop hoặc `<input type="file">`)
  - Tab 2: Paste YAML — **textarea thường** (font monospace, tab-size=2). Không syntax highlight, không auto-complete. Lỗi validate hiển thị panel dưới editor.
  - Tab 3: Clone từ builtin (dropdown chọn source)
  - Nút "✓ Validate" → static validate không lưu → show lỗi inline (line number nếu parser trả về)
  - Nút "💾 Save draft" → POST tạo revision (kể cả khi validate fail, badge warning)
  - Nút "▶ Test run" → chạy revision đang edit (owner-only; disable nếu revision fail validate)
- Revision history panel hiển thị rõ trạng thái từng rev:
  - Badge `[PUBLISHED]` ở revision đang là `published_revision_id`
  - Badge `[LATEST]` ở revision mới nhất
  - Badge `[FAILED]` ở revision fail validate
  - Nút "Copy publish SQL" ở mỗi rev passed → copy vào clipboard lệnh SQL sẵn sàng gửi admin:
    ```sql
    UPDATE scenario_definitions SET published_revision_id = <rev_id>
    WHERE id = '<scenario_id>';
    ```
- **Không có nút "Publish"** trong Phase 1 UI. Document rõ trong `USER_SCENARIO_GUIDE.md`:
  "Scenario sẵn sàng production → copy SQL → gửi admin để chạy."
- Sidebar chính: dropdown scenario chỉ hiện scenarios có `published_revision_id IS NOT NULL`

**Rủi ro Phase 1 (đã mitigate):**
- ~~Redis làm DB → mất khi flush~~ → dùng SQLite, không còn rủi ro này
- Admin bottleneck publish → OK vì Phase 1 ít user; UI copy-SQL giảm friction
- Không có audit ai publish → mitigated bởi revision immutable + `created_by` field

**Phụ thuộc kiểm tra trước khi code:**
- [ ] `LLM_base/scenarios/spec.py` — có Pydantic model không? Có `.model_dump()` → JSON chuẩn không?
  - Nếu có → "normalize" = `yaml.safe_load() → SpecModel(**data) → model_dump_json()`, ~0 effort.
  - Nếu không → cần viết normalize layer, thêm 1-2 ngày scope.
- [ ] Runtime hiện đọc YAML trực tiếp hay đã parse qua layer trung gian? (Quyết định có phải đổi runtime code không.)

---

### Phase 2 — Postgres + Auth thật + Self-serve publish (3-4 tuần)

**Mục tiêu:** Scale hạ tầng + bỏ bottleneck admin publish.

**Storage:**
- Swap SQLite → Postgres. Schema giữ nguyên (§1.2). Chỉ đổi `ScenarioRepository` impl.
- Migration: dump SQLite → restore Postgres; service layer không đổi.
- Redis cache strategy:
  - Read: Redis hit → return; miss → Postgres → cache 5m
  - Write: Postgres commit → invalidate Redis key

**Auth:** `AUTH_PROVIDER=jwt`, plug FPT SSO. Middleware extract `user_id`, `org_id`.

**Self-serve publish:**
```
POST /v1/scenarios/{id}/publish
  body: { revision_id }
  - caller phải là owner (Phase 2) hoặc org admin (Phase 2+)
  - revision_id phải thuộc scenario này và static_validation_status='passed'
  - warning nếu last_test_run_status != 'passed' (không block, chỉ warn UI)
```

**Frontend:**
- `/scenarios/mine` — của user
- `/scenarios/org` — share trong org (visibility='org')
- `/scenarios/builtin` — read-only catalog
- "Clone from builtin" → tạo scenario `source_type='cloned'`, editable
- Nút "Publish this revision" trong revision history panel
- Diff viewer giữa 2 revision

---

### Phase 3 — Visual Builder + Marketplace (6-10 tuần, nếu có demand)

**Chỉ làm khi:**
- >= 50 user active
- Feedback: "YAML quá khó"

**Components:**
- Drag-drop canvas cho steps
- Input schema designer (click thêm field, chọn type/required)
- Success/failure rules form
- Test panel: mock snapshot → xem action nào match target

**Marketplace:**
- Scenario `visibility='public'` → cross-org share
- Rating / comment / fork
- Admin approval trước khi public

---

## 3. Security (tính từ Phase 1)

### 3.1 Domain allowlist
- User khai `spec.allowed_domains` trong YAML
- Server MUST intersect với org allowlist trước khi chạy
- Không cho user tự đặt domain bất kỳ

### 3.2 Hooks Python
- User-created **KHÔNG** được set `hooks.pre_check`/`post_step` tuỳ ý
- Chỉ chọn từ HOOK_REGISTRY whitelist (tên hook)
- Hooks mới = admin deploy code (qua git + redeploy)

### 3.3 Action safety
- `goto`, `open_link` URL ngoài allowlist → runtime block
- Không để action mở domain trừ khi trong allowed_domains

### 3.4 Rate limit / quota
- Max scenarios per user: 100
- Max scenarios per org: 500
- Max concurrent sessions per user: 5
- `MAX_STEPS_CAP` enforce server-side (đã có)

### 3.5 Credential protection
- Field `spec.inputs[]` có `type: password` hoặc `sensitive: true` → `default` **bắt buộc rỗng**, validator hard-reject nếu có giá trị.
- Với field type khác: validator chạy regex secret-detection (AWS key, JWT, bearer token, password-like) → **warning** hiển thị trong UI editor, không hard-block (regex fail-open nếu hard-block).
- Credentials chỉ truyền qua `context` runtime, scrub khỏi log.
- Document rõ trong `USER_SCENARIO_GUIDE.md`: "YAML được version control trong DB revisions, coi như công khai nội bộ. Không commit secret."

### 3.6 Soft-delete retention
- 30 ngày trước khi xoá hẳn
- Admin có thể restore

---

## 4. Concrete file changes (Phase 1)

### Backend (`dev/deploy_server/`)

| File | Thay đổi |
|---|---|
| `ai_tool_web/store/scenario_repo.py` | **MỚI** — interface `ScenarioRepository` + `SqliteScenarioRepo` |
| `ai_tool_web/store/migrations/001_init.sql` | **MỚI** — schema 3 bảng (§1.2) |
| `ai_tool_web/auth/providers.py` | **MỚI** — `AuthProvider` interface + `Mock`/`SharedSecret` impl |
| `ai_tool_web/api/routes/scenarios.py` | Rewrite: validate, CRUD, revisions endpoints |
| `ai_tool_web/api/routes/sessions.py` | Sửa: nhận `revision_id?`, check owner cho test mode |
| `ai_tool_web/services/scenario_service.py` | Rewrite dùng repo + normalize logic |
| `ai_tool_web/models.py` | Models cho revision, run, pagination |
| `LLM_base/scenarios/spec.py` | Thêm `schema_version`, giữ tương thích |
| `ai_tool_web/api/app.py` | Wire auth middleware từ env `AUTH_PROVIDER` |
| `scripts/seed_builtin.sql` | **MỚI** — SQL insert builtin scenarios với id cố định |

### Frontend (`dev/web_UI_test/`)

| File | Thay đổi |
|---|---|
| `src/pages/ScenariosPage.jsx` | **MỚI** — list + filter (mine/builtin/archived) |
| `src/pages/ScenarioDetailPage.jsx` | **MỚI** — metadata + revision history + test run |
| `src/components/YamlEditor.jsx` | **MỚI** — textarea monospace + validate button |
| `src/components/RevisionList.jsx` | **MỚI** — list revisions, badges (PUBLISHED/LATEST/FAILED) |
| `src/components/ScenarioModal.jsx` | **MỚI** — create (upload/paste/clone) |
| `src/App.jsx` | Thêm route, sidebar link "Manage scenarios" |
| `package.json` | **không thêm deps mới** — dùng textarea native, skip Monaco |
| `.env.example` | `VITE_USER_ID=hiepqn` (mock auth header) |

### Infrastructure (product K8s)

| File | Thay đổi |
|---|---|
| `product_build/chang-browser-api/Dockerfile` | Mount volume cho SQLite file; copy `seed_builtin.sql` |
| `product_build/chang-browser-api/.env.example` | Thêm `AUTH_PROVIDER`, `ENV`, `DB_URL` (sqlite path) |
| K8s manifest (user tự xử lý) | PVC cho SQLite file (Phase 1); Phase 2 đổi sang Postgres Secret |

---

## 5. Redis impact

**Role:** chỉ cache đọc + runtime session state. **Không** phải primary store (primary = SQLite Phase 1 / Postgres Phase 2).

| Key | TTL | Purpose |
|---|---|---|
| `scenario:{id}` | 5m | cache scenario_definitions row |
| `scenario:{id}:rev:{rev_id}` | 5m | cache 1 revision (immutable → có thể TTL dài hơn) |
| `scenario:{id}:published` | 5m | cache published revision content (hot path cho runtime) |
| `scenarios:by_owner:{user_id}` | 5m | cache list |
| `scenarios:builtin` | 30m | cache builtin list (ít đổi) |

**Write path:** SQL commit → invalidate Redis keys liên quan (không write-through).

**Ước lượng:** scenario content ~10KB × cap 1000 scenarios × 3 rev hot ≈ 30MB. Không đáng kể.

---

## 6. Thứ tự commit đề xuất (Phase 1)

1. **Interfaces** — `ScenarioRepository`, `AuthProvider` (chỉ abstract, chưa impl) — **GATE: bạn review interface trước khi qua bước 2**
2. **Schema + SQLite impl** — migrations SQL, `SqliteScenarioRepo` (async qua `aiosqlite`), seed script builtin
2.5. **Migration Redis → SQLite** (G3) — script one-shot đọc `scenario:*` keys trong Redis hiện tại, insert vào `scenario_definitions` + rev 1 `normalized_spec_json`. Chạy 1 lần khi migrate. Redis sau đó giữ role cache.
3. **Auth impl** — `MockAuthProvider` only (guard `ENV=production` fail-start nếu mock)
4. **Backend service layer** — normalize YAML, validate logic, revision management, inputs validation (G5)
5. **Backend API routes** — scenarios CRUD + revisions + sessions endpoint + `GET /v1/hooks` (G7)
6. **Backend tests** — unit (SQLite `:memory:` fake), integration — **GATE: test pass trước khi làm UI**
7. **Frontend scaffolding** — route, sidebar link, empty page
8. **Frontend editor** — textarea + validate button + upload file
9. **Frontend CRUD UX** — list, detail, revision history, test run button
10. **E2E integration test** — create → validate → save rev → test run → (SQL publish) → production run
11. **Security checklist** — domain intersect, hook whitelist, quota, password field guard
12. **Docs** — API.md + `USER_SCENARIO_GUIDE.md` (nhấn mạnh: publish qua admin SQL)

Interface-first (bước 1) đảm bảo Phase 2 swap SQLite→Postgres chỉ là thêm 1 file, không rewrite.
Gate review ở bước 1 và bước 6 (trước khi code UI) — theo yêu cầu "test từng giai đoạn".

---

## 7. Open decisions

**Đã chốt (session 2026-04-23):**
- [x] Q1: user = dev/admin FPT biết YAML
- [x] Q3: clone = fork-and-forget, không track upstream
- [x] Q4: hooks whitelist only, không custom Python
- [x] Storage: pluggable repo, Phase 1 SQLite, Phase 2 Postgres
- [x] Draft/published: lưu `published_revision_id` (nullable), revision immutable
- [x] Publish flow Phase 1: admin làm qua SQL, không có API
- [x] Builtin: read-only qua API, admin sửa qua SQL
- [x] Run endpoint: gộp 1 endpoint, `revision_id` optional, owner-only cho test

**Đã chốt (session 2026-04-24):**
- [x] Q5: `AUTH_PROVIDER=mock`, `X-User-Id=hiepqn` cho laptop test
- [x] Q8: không deadline, test từng giai đoạn với gate review ở bước 1 & 6
- [x] Editor: textarea thường, không Monaco (skip ~2MB bundle + 0.5d dev)
- [x] G1: dùng `aiosqlite` cho async SQLite
- [x] G2: auto-run seed builtin khi DB rỗng lúc startup
- [x] G3: thêm bước 2.5 migration Redis → SQLite one-shot
- [x] G4: tách `parent_revision_id` (chain edit) + `clone_source_revision_id` (gốc khi clone)
- [x] G5: inputs validate server-side từ `revision.normalized_spec_json.inputs[]`
- [x] G6: worker mở direct DB connection để update `scenario_runs.status`
- [x] G7: thêm `GET /v1/hooks` cho UI hint

**Deferred (không làm Phase 1):**
- Public marketplace: Phase 3+
- Billing/quota per session: Phase 3
- Scenario diff UI: Phase 2

---

## 8. Non-goals Phase 1 (explicitly out of scope)

Các mục sau **không** làm ở Phase 1, để chặn scope creep:

- Multi-tenant / org isolation (single-tenant)
- SSO integration (chờ Phase 2, AUTH_PROVIDER pluggable đã có sẵn)
- Visual builder / wizard UI (Phase 3 nếu có demand từ non-tech user)
- Public marketplace / sharing cross-org (Phase 3+)
- Self-serve publish qua API (admin SQL Phase 1, API Phase 2)
- Auto-sync builtin từ file YAML → DB (admin SQL thủ công Phase 1)
- Upstream tracking cho cloned builtin (fork-and-forget)
- Custom Python hooks (không bao giờ — whitelist only, hooks mới = admin deploy code)
- Billing / quota per-session (Phase 3)
- Scenario diff UI (Phase 2)
- Cross-org public visibility (Phase 3+)

---

## 9. Tham khảo

- Current scenario spec: [`LLM_base/scenarios/spec.py`](LLM_base/scenarios/spec.py)
- Current scenario service: [`ai_tool_web/services/scenario_service.py`](ai_tool_web/services/scenario_service.py)
- Current admin route: [`ai_tool_web/api/routes/scenarios.py`](ai_tool_web/api/routes/scenarios.py)
- Builtin YAML examples: [`LLM_base/scenarios/builtin/`](LLM_base/scenarios/builtin/)
- Redis key schema: trong các file `ai_tool_web/store/*.py`
