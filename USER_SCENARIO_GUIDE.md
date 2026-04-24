# User Scenario Guide — Phase 1

> **Audience:** Dev/admin nội bộ FPT tạo scenario custom cho Chang Tool Web.
> **Scope:** Phase 1 — single-tenant laptop/local, mock auth, admin publish qua SQL.
> **Status:** Ship cho dev test nội bộ.

---

## 1. Tổng quan

Chang Tool Web cho phép tạo **scenario** (kịch bản browser automation) bằng file YAML, rồi chạy qua agent LLM + Playwright.

Phase 1 có 2 tier scenarios:

| Tier | Nguồn | Ai tạo | Ai chạy production |
|---|---|---|---|
| **Builtin** | YAML trong code (`LLM_base/scenarios/builtin/*.yaml`) | Dev commit vào git | Ai cũng chạy được |
| **User** | UI tạo → lưu SQLite | User tự tạo | Admin phải SQL publish trước |

User có thể:
- Tạo scenario mới từ đầu
- Clone builtin để sửa
- Sửa scenario nhiều lần (mỗi lần tạo 1 revision immutable)
- Gửi admin SQL để publish một revision

User **KHÔNG** thể:
- Publish scenario của chính mình (admin-only qua SQL)
- Xoá hoặc sửa builtin
- Xem/sửa scenario của user khác

---

## 2. Prerequisites

### 2.1 Services đang chạy

Cần 3 services:

```powershell
# Redis (1 lần)
docker start redis-phase1

# API (Terminal 1)
cd D:\research\ChangAI\web_brower\dev\deploy_server
Get-Content .env | ForEach-Object {
    if ($_ -match '^([A-Z_]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}
$env:PYTHONPATH = "$PWD\ai_tool_web"
cd ai_tool_web
python -m uvicorn api.app:app --host 127.0.0.1 --port 9000

# Worker (Terminal 2) — cần OPENAI_API_KEY thật trong .env
cd D:\research\ChangAI\web_brower\dev\deploy_server
Get-Content .env | ForEach-Object {
    if ($_ -match '^([A-Z_]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}
$env:PYTHONPATH = "$PWD\ai_tool_web"
cd ai_tool_web
python -m worker.browser_worker --count 1

# UI (Terminal 3)
cd D:\research\ChangAI\web_brower\dev\web_UI_test
npm run dev
```

Mở `http://localhost:5173` trong browser.

### 2.2 Verify health

```powershell
curl http://127.0.0.1:9000/v1/health
# Expect: {"status":"ok","workers_alive":1,"workers_busy":0,"queue_length":0}
```

`workers_alive >= 1` là điều kiện để scenario chạy được.

---

## 3. Workflow: tạo và chạy scenario mới

### Step 1. Mở trang manage

Trong UI main → bấm **📚 Manage scenarios** (sidebar trái).

### Step 2. Tạo scenario mới

Bấm **➕ New scenario** → modal mở với 3 tabs:

**Tab "📝 Paste YAML":** Tự viết scenario từ đầu.
**Tab "📁 Upload file":** Chọn file `.yaml` có sẵn.
**Tab "🌱 Clone from builtin":** Copy 1 builtin → sửa.

### Step 3. Viết YAML

Ví dụ tối thiểu:

```yaml
id: will_be_overridden
display_name: Tìm kiếm pháp luật
description: Tìm văn bản luật trên thuvienphapluat.vn
start_url: https://thuvienphapluat.vn
allowed_domains: [thuvienphapluat.vn]
max_steps_default: 10

inputs:
  - name: keyword
    type: string
    required: true
    source: context
    description: Từ khóa cần tìm

goal: |
  Tìm trang thuvienphapluat.vn, điền từ khóa {keyword} vào ô tìm kiếm,
  bấm nút Tìm kiếm, chờ trang kết quả, action=done.
```

→ Xem [§5 Schema](#5-yaml-schema-reference) để biết đủ field.

### Step 4. Validate

Bấm **✓ Validate** — UI gọi `/v1/scenarios/validate` (dry-run, không lưu DB).

Hiển thị:
- `[VALID]` xanh → parse + pydantic + security check pass
- `[VALIDATION FAILED]` vàng → có lỗi, xem detail dưới
- `[PARSE ERROR]` đỏ → YAML syntax sai, không lưu được

Sửa lỗi dựa trên error list, validate lại.

### Step 5. Save draft

Bấm **💾 Save draft** — UI gọi `POST /v1/scenarios`.

Backend:
- Tạo `scenario_definitions` row (source_type=user, owner_id=bạn)
- Tạo `scenario_revisions` row version_no=1
- id tự sinh theo `user_<your_id>_<slug(display_name)>`

Sau save: modal đóng, list refresh, scenario mới xuất hiện ở tab "👤 Mine" với badge `draft` (chưa publish).

### Step 6. Edit thêm nếu cần

Click scenario ở list → detail panel. Revision table hiển thị v1.

Muốn sửa? Hiện Phase 1 **chưa có inline edit từ UI**. Workflow hiện tại:
1. Mở modal ➕ New scenario → tab "Clone from builtin" không, nhưng để tạo bản mới thì paste YAML đã sửa vào tab Paste
2. Hoặc dùng API trực tiếp: `PUT /v1/scenarios/{id}` với raw_yaml mới

Phase 2 sẽ thêm inline edit.

### Step 7. Gửi admin publish

Click revision row muốn publish (có badge `LATEST`) → nút **📋 Copy publish SQL** hiện ra.

Bấm → SQL được copy vào clipboard:
```sql
UPDATE scenario_definitions SET published_revision_id = 42 WHERE id = 'user_hiepqn_tim_luat';
```

Gửi SQL này cho admin (Slack/chat). Admin chạy lệnh trên DB.

### Step 8. Verify published

Sau khi admin chạy SQL, reload trang → revision sẽ có badge `PUBLISHED`.

### Step 9. Chạy scenario

Quay về main UI (bấm × đóng trang manage). Phase 1 **dropdown main UI chỉ list builtin** — scenario của bạn không hiện ra đây.

**Workaround Phase 1:**
- Sau khi publish, user có thể **manually gọi API** để chạy:

```powershell
$body = @{
    scenario = "user_hiepqn_tim_luat"
    context = @{ keyword = "nghị định" }
    max_steps = 10
} | ConvertTo-Json

Invoke-RestMethod `
    -Uri "http://127.0.0.1:9000/v1/sessions" `
    -Method POST `
    -ContentType "application/json" `
    -Body $body
```

Session chạy xong xem qua `/v1/sessions/{id}/stream` SSE.

**Phase 2** sẽ:
- Dropdown main UI list cả user published scenarios
- UI nút "Test this revision" chạy trực tiếp từ trang manage (không cần publish trước)

---

## 4. Admin workflow

### 4.1 Publish revision

```sql
-- SQLite (Phase 1)
sqlite3 dev/deploy_server/scenarios.db
```

```sql
-- Verify revision OK (status='passed')
SELECT id, version_no, static_validation_status
FROM scenario_revisions
WHERE scenario_id = 'user_hiepqn_tim_luat'
ORDER BY version_no DESC;

-- Publish
UPDATE scenario_definitions
SET published_revision_id = 42, updated_at = datetime('now')
WHERE id = 'user_hiepqn_tim_luat';

-- Confirm
SELECT id, published_revision_id FROM scenario_definitions WHERE id = 'user_hiepqn_tim_luat';
```

### 4.2 Unpublish (rollback)

```sql
UPDATE scenario_definitions
SET published_revision_id = NULL, updated_at = datetime('now')
WHERE id = 'user_hiepqn_tim_luat';
```

### 4.3 Xem lịch sử revisions

```sql
SELECT r.id, r.version_no, r.static_validation_status,
       r.last_test_run_status, r.created_by, r.created_at
FROM scenario_revisions r
WHERE r.scenario_id = 'user_hiepqn_tim_luat'
ORDER BY r.version_no DESC;
```

### 4.4 Sửa builtin

Builtin không sửa được qua API. Flow:
1. Edit YAML ở `LLM_base/scenarios/builtin/<name>.yaml`
2. Manually insert revision mới vào SQLite:

```sql
-- Bump version_no
INSERT INTO scenario_revisions
  (scenario_id, version_no, raw_yaml, normalized_spec_json, yaml_hash,
   schema_version, static_validation_status, created_by, created_at)
VALUES
  ('chang_login', 2, '<raw YAML>', '<JSON>', '<sha256>',
   1, 'passed', 'admin', datetime('now'));

-- Publish revision mới
UPDATE scenario_definitions
SET published_revision_id = (SELECT MAX(id) FROM scenario_revisions WHERE scenario_id = 'chang_login'),
    updated_at = datetime('now')
WHERE id = 'chang_login';
```

Phase 2 sẽ có script auto sync YAML → DB.

### 4.5 Xoá scenario user (hard delete)

```sql
-- Soft delete (user đã archive)
UPDATE scenario_definitions SET is_archived = 1 WHERE id = 'user_xxx_yyy';

-- Hard delete (cẩn thận — cascade sang revisions nếu FK có ON DELETE CASCADE)
DELETE FROM scenario_definitions WHERE id = 'user_xxx_yyy';
```

---

## 5. YAML schema reference

### Minimal

```yaml
id: will_be_overridden          # user scenario sẽ auto-prefix, không cần viết đúng
display_name: Tên hiển thị
goal: Mô tả nhiệm vụ cho LLM agent
```

### Full schema

```yaml
# ── Identity ────────────────────────────────────────────────
id: will_be_overridden           # User scenario: id auto-gen `user_<owner>_<slug>`
display_name: Tên hiển thị       # Required, hiện trong dropdown UI
description: Mô tả ngắn          # Optional, hiện trong UI tooltip/detail

# ── Mode ────────────────────────────────────────────────────
mode: agent                      # 'agent' | 'flow' | 'hybrid'
                                 # agent: LLM đọc goal, tự quyết step (default)
                                 # flow:  chạy steps[] khai báo, không LLM
                                 # hybrid: flow trước, agent fallback

# ── Run config ──────────────────────────────────────────────
start_url: https://example.com   # URL đầu tiên worker mở
goal: "Nhiệm vụ {placeholder}"    # Mô tả cho LLM. {placeholder} từ context
max_steps_default: 20            # Giới hạn số step (server cap = 30)
allowed_domains:                 # Allowlist domain agent được nav tới
  - example.com
  - sub.example.com

# ── Inputs (G5 runtime) ─────────────────────────────────────
inputs:
  - name: keyword
    type: string                 # 'string' | 'secret' | 'number' | 'bool'
    required: true
    source: context              # 'context' | 'ask_user'
                                 # context: user gửi qua request body
                                 # ask_user: runtime agent sẽ hỏi qua ask event
    default: ""                  # Optional. Dùng nếu user không gửi
    description: "Từ khóa cần tìm"

  - name: otp
    type: string
    source: ask_user             # ← SSE stream gửi event 'ask' khi cần

# ── Flow steps (chỉ mode=flow/hybrid) ──────────────────────
steps:
  - action: wait_for
    target: { text_any: ["Tìm kiếm"], role: textbox }
    timeout_ms: 10000
  - action: fill
    target: { placeholder_any: ["Nhập từ khóa"] }
    value_from: keyword          # lấy từ inputs
  - action: click
    target: { text_any: ["Tìm kiếm"], role: button }

# ── Success/failure rules (chỉ mode=flow) ──────────────────
success:
  any_of:
    - { url_contains: "tim-kiem" }
    - { text_any: ["Kết quả"] }
failure:
  any_of:
    - { text_any: ["Không tìm thấy"] }
  code: SEARCH_FAILED
  message: "Không ra kết quả"

# ── Prompt tuning (mode=agent) ─────────────────────────────
system_prompt_extra: |
  Lưu ý: trang này có iframe, luôn chờ 2s sau khi click.

# ── Hooks (Python, whitelist only) ─────────────────────────
hooks:
  pre_check: chang_login.pre_check     # tên từ HOOK_REGISTRY
  post_step: chang_login.post_step
  final_capture: null
```

### Security rules

- `allowed_domains` **bắt buộc** nếu agent navigate ngoài `start_url`
- `inputs[].type=secret` hoặc tên chứa `password/pwd/secret/token` → `default` phải rỗng
- `hooks.*` phải là tên trong HOOK_REGISTRY (xem `GET /v1/hooks`)

---

## 6. Troubleshooting

### "401 Unauthenticated"

→ Header `X-User-Id` không gửi. Check `.env.local`:
```
VITE_USER_ID=hiepqn
```
Restart `npm run dev`.

### "Lỗi load scenarios: VITE_USER_ID chưa set"

→ Same above.

### Scenario validate fail với "Hook 'xxx' chưa register"

→ Hook name không có trong HOOK_REGISTRY. Xem list:
```bash
curl -H "X-User-Id: hiepqn" http://127.0.0.1:9000/v1/hooks
```
Hooks mới phải dev thêm qua `@hook('name')` decorator → rebuild + redeploy.

### "Scenario chưa có revision nào"

→ Scenario tồn tại ở DB nhưng chưa có revision (bug hiếm). Re-create scenario.

### Session kẹt "queued" mãi

→ Worker chưa chạy. Check:
```bash
curl http://127.0.0.1:9000/v1/health
# Cần: workers_alive >= 1
```
Nếu 0 → start worker (xem §2.1).

### Worker restart nhưng session cũ vẫn kẹt

Clear Redis state:
```bash
docker exec redis-phase1 redis-cli DEL pending_jobs
docker exec redis-phase1 redis-cli --scan --pattern "session:*" | xargs -I {} docker exec redis-phase1 redis-cli DEL {}
```

### OPENAI_API_KEY lỗi / rate limit

→ Check `.env` có key đúng. Restart worker sau khi đổi key.

### Scenario của tôi không hiện trong main UI dropdown

Phase 1 main UI **chỉ show builtin**. User scenarios chỉ xem được ở trang `/scenarios`.
Muốn chạy: dùng curl hoặc đợi Phase 2 có test-run button.

---

## 7. Limitations Phase 1

Biết trước để không ngạc nhiên:

- ❌ **Inline edit YAML ở UI** — phải create mới hoặc dùng curl PUT
- ❌ **Test run từ UI manage** — publish trước rồi chạy qua main UI
- ❌ **User scenarios trong main dropdown** — chỉ builtin
- ❌ **Publish từ UI** — admin SQL only
- ❌ **Auto sync builtin YAML → DB** — admin SQL thủ công
- ❌ **Multi-user/org** — single tenant, mock auth
- ❌ **Visual builder** — chỉ textarea YAML
- ❌ **SSO/JWT** — chỉ header X-User-Id

Phase 2 sẽ khép dần các items này.

---

## 8. API reference (quick)

| Method | Path | Description |
|---|---|---|
| POST | `/v1/scenarios/validate` | Dry-run validate YAML |
| POST | `/v1/scenarios` | Tạo mới (rev 1) |
| POST | `/v1/scenarios/clone` | Clone từ scenario khác |
| GET | `/v1/scenarios` | List (filter source_type/archived) |
| GET | `/v1/scenarios/{id}` | Detail (metadata + rev summary) |
| PUT | `/v1/scenarios/{id}` | Tạo revision mới |
| DELETE | `/v1/scenarios/{id}` | Archive soft |
| GET | `/v1/scenarios/{id}/revisions` | List revisions |
| GET | `/v1/scenarios/{id}/revisions/{rev_id}` | Full revision content |
| GET | `/v1/hooks` | List HOOK_REGISTRY |

Tất cả yêu cầu header `X-User-Id: <your_id>`.

Full spec: xem `api/routes/user_scenarios.py` + `api/routes/user_hooks.py`.

---

## 9. Feedback

Phase 1 là MVP. Góp ý về UX/bug/missing feature → note vào file này dưới §10 hoặc message team.

## 10. Known issues / TODO

- [ ] Inline edit UI (Phase 2)
- [ ] Test run button (Phase 2)
- [ ] User scenarios trong main dropdown (Phase 2)
- [ ] Diff viewer giữa 2 revisions (Phase 2)
- [ ] Upload file UX trong NewScenarioModal chưa preview YAML trước save
- [ ] "Lưu" khi validation fail hiện save OK với badge FAILED — có thể confuse, cần UI hint rõ hơn
