# Phase 1 Acceptance Test — User-Configurable Scenario

> **Scope:** Test plan chi tiết cho deliverable Phase 1 theo [PLAN_USER_SCENARIO_CUSTOMIZATION.md](PLAN_USER_SCENARIO_CUSTOMIZATION.md).
> **Audience:** Dev tự test sau mỗi bước hoàn thành.
> **Style:** Checklist + concrete curl/SQL snippets — tick từng box khi pass.

---

## 0. Prerequisites

### 0.1 Env setup (1 lần)

```bash
# Backend deps
cd dev/deploy_server
pip install -r requirements.txt     # đã bao gồm aiosqlite

# Frontend deps
cd dev/web_UI_test
npm install
```

### 0.2 Config

**`dev/deploy_server/.env`:**
```
AUTH_PROVIDER=mock
ENV=development
DB_URL=sqlite:///./scenarios.db
REDIS_URL=redis://localhost:6379/0
ADMIN_TOKEN=04b27a0186187057415fdb84c4d0dfc099f34146f418b4e0
```

**`dev/web_UI_test/.env.local`:**
```
VITE_USER_ID=hiepqn
VITE_ADMIN_TOKEN=04b27a0186187057415fdb84c4d0dfc099f34146f418b4e0
```

### 0.3 Startup

```bash
# Terminal 1 — Redis
redis-server

# Terminal 2 — API (port 9000)
cd dev/deploy_server && bash start_api.sh

# Terminal 3 — Worker
cd dev/deploy_server && bash start_worker.sh 1

# Terminal 4 — Frontend
cd dev/web_UI_test && npm run dev
```

### 0.4 Biến shell

```bash
export SERVER=http://localhost:9000
export ADMIN_TOKEN=04b27a0186187057415fdb84c4d0dfc099f34146f418b4e0
export USER_ID=hiepqn
export DB=dev/deploy_server/scenarios.db
```

---

## 1. Startup + Migration (T1)

### T1.1 Fresh DB auto-migrate + seed

- [ ] Xoá `scenarios.db` nếu có
- [ ] `bash start_api.sh`
- [ ] Verify file `scenarios.db` được tạo
- [ ] Log xuất hiện: `Seeded N builtin scenarios`
- [ ] `sqlite3 $DB "SELECT id, source_type FROM scenario_definitions"` → có rows builtin

### T1.2 Restart → idempotent seed

- [ ] Stop API, restart
- [ ] Log xuất hiện: `Seeded 0 builtin scenarios` (hoặc tương đương không insert lại)
- [ ] `SELECT COUNT(*) FROM scenario_definitions WHERE source_type='builtin'` giữ nguyên

### T1.3 Production + mock = refuse start

- [ ] Set `ENV=production` + `AUTH_PROVIDER=mock`
- [ ] Start API → **FAIL** với message rõ ràng (vd: "mock auth không cho production")
- [ ] Revert `ENV=development`, start OK

---

## 2. Scenario CRUD qua API (T2)

### T2.1 Validate-only (dry run)

```bash
cat > /tmp/scn_test.json << 'EOF'
{
  "id": "user_search_law",
  "name": "Tìm kiếm pháp luật test",
  "raw_yaml": "id: user_search_law\ndisplay_name: Test\nstart_url: https://thuvienphapluat.vn\nallowed_domains: [thuvienphapluat.vn]\ninputs:\n  - name: keyword\n    type: string\n    required: true\n    source: context\ngoal: Tìm {keyword}\n"
}
EOF

curl -X POST -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d @/tmp/scn_test.json "$SERVER/v1/scenarios/validate"
```

- [ ] Response 200 với `{status: "valid"}` hoặc `422` với `errors: [...]`
- [ ] KHÔNG có row mới trong `scenario_definitions`

### T2.2 Create scenario + revision 1

```bash
curl -X POST -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d @/tmp/scn_test.json "$SERVER/v1/scenarios"
```

- [ ] Response 201 với full spec, `version: 1`
- [ ] `sqlite3 $DB "SELECT id, owner_id, source_type FROM scenario_definitions WHERE id='user_search_law'"` → row với `owner_id=hiepqn, source_type=user`
- [ ] `SELECT version_no, static_validation_status FROM scenario_revisions WHERE scenario_id='user_search_law'` → `(1, 'passed')`

### T2.3 Duplicate id → reject

```bash
curl -X POST -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d @/tmp/scn_test.json "$SERVER/v1/scenarios"
```

- [ ] Response 409 Conflict với message rõ

### T2.4 List với filter

```bash
# Mine
curl -H "X-User-Id: $USER_ID" "$SERVER/v1/scenarios?source_type=user"

# Builtin
curl -H "X-User-Id: $USER_ID" "$SERVER/v1/scenarios?source_type=builtin"

# Archived
curl -H "X-User-Id: $USER_ID" "$SERVER/v1/scenarios?is_archived=true"
```

- [ ] Mỗi filter trả đúng subset
- [ ] Non-owner không thấy scenario private của user khác

### T2.5 Update → revision 2

```bash
# Edit raw_yaml tí, gọi PUT
curl -X PUT -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d '{"raw_yaml": "id: user_search_law\n# edited\n..."}' \
  "$SERVER/v1/scenarios/user_search_law"
```

- [ ] Response 200 với `version: 2`
- [ ] `SELECT version_no FROM scenario_revisions WHERE scenario_id='user_search_law' ORDER BY version_no` → `1, 2`
- [ ] `parent_revision_id` của v2 = id của v1

### T2.6 Save same YAML → no-op reject

- [ ] Gọi PUT lại với YAML không đổi
- [ ] Response 409 hoặc 400 với message "no changes" / "duplicate yaml_hash"
- [ ] KHÔNG tạo revision 3

### T2.7 Save YAML validate fail → revision vẫn tạo

```bash
# YAML valid parse nhưng có semantic error (vd: action 'bogus')
curl -X PUT -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d '{"raw_yaml": "id: user_search_law\nsteps:\n  - action: BOGUS_ACTION\n"}' \
  "$SERVER/v1/scenarios/user_search_law"
```

- [ ] Response 200 với warning, `static_validation_status: "failed"`
- [ ] `SELECT static_validation_status, static_validation_errors FROM scenario_revisions WHERE scenario_id='user_search_law' ORDER BY id DESC LIMIT 1` → failed + errors JSON

### T2.8 Save YAML parse fail → hard reject

```bash
curl -X PUT -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d '{"raw_yaml": "!!! not yaml !!!"}' \
  "$SERVER/v1/scenarios/user_search_law"
```

- [ ] Response 400 với parse error + line/column
- [ ] KHÔNG tạo revision

### T2.9 Non-owner không update được

```bash
curl -X PUT -H "X-User-Id: other_user" -H "Content-Type: application/json" \
  -d '{"raw_yaml": "..."}' "$SERVER/v1/scenarios/user_search_law"
```

- [ ] Response 403

### T2.10 Delete = archive soft

```bash
curl -X DELETE -H "X-User-Id: $USER_ID" "$SERVER/v1/scenarios/user_search_law"
```

- [ ] Response 204
- [ ] `SELECT is_archived FROM scenario_definitions WHERE id='user_search_law'` → `1`
- [ ] List mặc định không có; `?is_archived=true` có

### T2.11 Builtin read-only qua API

```bash
curl -X DELETE -H "X-User-Id: admin" "$SERVER/v1/scenarios/search_thuvienphapluat"
```

- [ ] Response 403 với message "builtin không xoá qua API"

---

## 3. Revision history (T3)

### T3.1 List revisions

```bash
curl -H "X-User-Id: $USER_ID" "$SERVER/v1/scenarios/user_search_law/revisions"
```

- [ ] Response list, newest-first (id DESC)
- [ ] Mỗi item có `id, version_no, yaml_hash, static_validation_status, created_at`

### T3.2 Get specific revision

```bash
curl -H "X-User-Id: $USER_ID" "$SERVER/v1/scenarios/user_search_law/revisions/1"
```

- [ ] Response full revision content (raw_yaml + normalized_spec_json)

### T3.3 UI badge hiển thị đúng

- [ ] Mở UI `/scenarios/user_search_law`
- [ ] Rev mới nhất có badge `[LATEST]`
- [ ] Rev đang publish có badge `[PUBLISHED]` (sau khi T6.1)
- [ ] Rev fail validate có badge `[FAILED]`

### T3.4 Copy publish SQL

- [ ] Bấm nút "Copy publish SQL" ở 1 rev passed
- [ ] Paste vào editor → đúng dạng `UPDATE scenario_definitions SET published_revision_id=<ID> WHERE id='<SCENARIO_ID>';`

---

## 4. Clone (T4)

### T4.1 Clone từ builtin

```bash
curl -X POST -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d '{"from_scenario_id": "search_thuvienphapluat", "new_id": "user_my_search"}' \
  "$SERVER/v1/scenarios/clone"
```

- [ ] Response 201 với scenario mới, `source_type: "cloned"`
- [ ] `SELECT clone_source_revision_id FROM scenario_revisions WHERE scenario_id='user_my_search'` → ID của rev gốc builtin

### T4.2 Edit cloned → rev 2 (không chain lên builtin)

- [ ] PUT edit cloned scenario
- [ ] `parent_revision_id` của v2 = v1 cloned (KHÔNG phải rev builtin)
- [ ] `clone_source_revision_id` vẫn giữ nguyên ở rev 1 (immutable)

---

## 5. Test run (owner-only) (T5)

### T5.1 Owner test run own draft

```bash
# Lấy rev_id mới nhất
REV_ID=$(sqlite3 $DB "SELECT MAX(id) FROM scenario_revisions WHERE scenario_id='user_search_law'")

curl -X POST -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d "{\"scenario_id\": \"user_search_law\", \"revision_id\": $REV_ID, \"inputs\": {\"keyword\": \"nghị định\"}}" \
  "$SERVER/v1/sessions"
```

- [ ] Response 201 với `session_id`
- [ ] `SELECT mode, revision_id FROM scenario_runs WHERE session_id='...'` → `test, $REV_ID`
- [ ] Stream SSE → thấy events step/done

### T5.2 Non-owner test run → 403

```bash
curl -X POST -H "X-User-Id: stranger" -H "Content-Type: application/json" \
  -d "{\"scenario_id\": \"user_search_law\", \"revision_id\": $REV_ID, \"inputs\": {}}" \
  "$SERVER/v1/sessions"
```

- [ ] Response 403

### T5.3 Worker update scenario_runs.status khi done

- [ ] Chờ session done
- [ ] `SELECT status FROM scenario_runs WHERE session_id='...'` → `completed`

---

## 6. Production run (qua SQL publish) (T6)

### T6.1 Admin publish qua SQL

```bash
# Trong MySQL Workbench / sqlite3:
sqlite3 $DB "UPDATE scenario_definitions SET published_revision_id=$REV_ID WHERE id='user_search_law'"
```

- [ ] `SELECT published_revision_id FROM scenario_definitions WHERE id='user_search_law'` → ID đã set

### T6.2 Production session dùng published

```bash
curl -X POST -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d '{"scenario_id": "user_search_law", "inputs": {"keyword": "nghị định"}}' \
  "$SERVER/v1/sessions"
```

- [ ] Response 201
- [ ] `mode=production` trong scenario_runs
- [ ] `revision_id` = published_revision_id

### T6.3 Chưa publish → 409

```bash
# Unpublish trước
sqlite3 $DB "UPDATE scenario_definitions SET published_revision_id=NULL WHERE id='user_search_law'"

curl -X POST -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d '{"scenario_id": "user_search_law", "inputs": {}}' "$SERVER/v1/sessions"
```

- [ ] Response 409 với message "scenario chưa publish"

---

## 7. Inputs validation (G5) (T7)

### T7.1 Thiếu required input → 400

```bash
curl -X POST -H "X-User-Id: $USER_ID" -H "Content-Type: application/json" \
  -d '{"scenario_id": "user_search_law", "inputs": {}}' "$SERVER/v1/sessions"
```

- [ ] Response 400 với detail: missing `keyword`

### T7.2 Type mismatch

- [ ] Scenario có `inputs: [{name: count, type: number}]`
- [ ] Submit `{count: "abc"}` → 400 type error

### T7.3 Extra fields → silently drop (hoặc reject?)

- [ ] Submit `{keyword: "x", extra: "y"}` → quyết định: drop hay warn (TBD spec)

---

## 8. Security guards (§3) (T8)

### T8.1 Allowed domain ngoài whitelist → reject

- [ ] YAML có `allowed_domains: ['evil.com']`
- [ ] Tạo scenario → validate fail với error domain

### T8.2 Hook không trong registry → reject

- [ ] YAML có `hooks.pre_check: nonexistent_hook`
- [ ] Validate fail với error hook whitelist

### T8.3 Password field có default value → reject

- [ ] YAML có `inputs: [{name: pwd, type: password, default: "abc"}]`
- [ ] Validate fail với error credential protection

### T8.4 Quota max 100 scenarios/user

- [ ] Seed 100 scenario cho user
- [ ] POST thứ 101 → response 429 Too Many Requests

---

## 9. UI end-to-end (T9)

### T9.1 Scenarios page

- [ ] Mở `/scenarios` → thấy list (mine + builtin)
- [ ] Filter tabs hoạt động
- [ ] Click scenario → mở detail page

### T9.2 Create modal

- [ ] Click "➕ New scenario"
- [ ] Tab Upload: drag YAML file → textarea auto-fill
- [ ] Tab Paste: textarea monospace, tab=2
- [ ] Tab Clone: dropdown chọn builtin → content copy
- [ ] Validate button → show errors inline
- [ ] Save → đóng modal, list refresh

### T9.3 Detail page

- [ ] Metadata hiển thị đúng
- [ ] Revision history panel scroll được
- [ ] Test run button disable khi rev `failed`
- [ ] Test run button chạy, stream events vào chat panel

### T9.4 Main test UI tích hợp

- [ ] Dropdown scenario load từ API
- [ ] Chọn scenario mới publish → form render `inputs[]`
- [ ] `type: password` → input type password (che)
- [ ] `type: number` → input type number
- [ ] `type: bool` → checkbox
- [ ] `source: ask_user` field KHÔNG render
- [ ] Submit → session chạy, events stream

### T9.5 Ask event flow

- [ ] Scenario có `inputs: [{source: ask_user}]`
- [ ] Submit → session chạy
- [ ] Khi tới step ask → SSE `ask` event
- [ ] Chat panel hiện câu hỏi
- [ ] User trả lời → `POST /resume` → session tiếp tục

---

## 10. Hooks registry (G7) (T10)

### T10.1 List hooks

```bash
curl -H "X-User-Id: $USER_ID" "$SERVER/v1/hooks"
```

- [ ] Response list `[{name, description, accepts}]`
- [ ] Có ít nhất `chang_login.pre_check`, `chang_login.post_step` (từ HOOK_REGISTRY)

---

## 11. Redis cache (T11)

### T11.1 Cache hit/miss

- [ ] `redis-cli --scan --pattern 'scenario:*'` → xem keys
- [ ] Lần đầu `GET /v1/scenarios/{id}` → log cache miss
- [ ] Lần 2 → cache hit, TTL 5m

### T11.2 Invalidate on update

- [ ] PUT scenario → cache key `scenario:{id}` bị del
- [ ] GET tiếp → miss → fresh data

---

## 12. Automated tests (repo + auth + service + integration)

### T12.1 Unit tests pass

```bash
cd dev/deploy_server/ai_tool_web
python tests/test_scenario_repo.py     # 52 pass
python tests/test_mock_auth.py         # 12 pass
python tests/test_scenario_service.py  # TBD bước 4
```

- [ ] All pass, exit 0

### T12.2 Integration test E2E

```bash
python tests/test_scenario_e2e.py
```

- [ ] Suite cover: create → validate → save rev → test run → publish SQL → production run

---

## 13. Non-goals — verify KHÔNG có

Xác nhận các feature sau **KHÔNG** tồn tại trong Phase 1 (khỏi test nhầm):

- [ ] Không có `POST /v1/scenarios/{id}/publish` endpoint
- [ ] Không có nút "Publish" trong UI
- [ ] Không có SSO/JWT (chỉ mock)
- [ ] Không có visual builder
- [ ] Không có org sharing
- [ ] Không có diff viewer
- [ ] Không có MySQL/Postgres (SQLite only)

---

## 14. Acceptance criteria — Phase 1 "done"

Phase 1 được coi là **done** khi:

- [ ] Tất cả T1-T11 pass trên laptop local
- [ ] Automated tests T12 pass 100%
- [ ] UI demo flow (§ demo đầu file) chạy mượt end-to-end
- [ ] `USER_SCENARIO_GUIDE.md` viết xong với ví dụ cụ thể
- [ ] `API.md` cập nhật với endpoints mới
- [ ] Migration script Redis→SQLite chạy thành công 1 lần với data cũ (T6)
- [ ] Code review pass, git commit clean, push GitHub

---

## 15. Appendix — các SQL useful

```sql
-- Inspect scenarios
SELECT id, owner_id, source_type, published_revision_id, is_archived
FROM scenario_definitions;

-- Latest revision per scenario
SELECT d.id, r.version_no, r.static_validation_status
FROM scenario_definitions d
JOIN scenario_revisions r ON r.scenario_id = d.id
WHERE r.id IN (SELECT MAX(id) FROM scenario_revisions GROUP BY scenario_id);

-- Publish
UPDATE scenario_definitions SET published_revision_id = ? WHERE id = ?;

-- Unpublish
UPDATE scenario_definitions SET published_revision_id = NULL WHERE id = ?;

-- Recent runs
SELECT r.id, r.scenario_id, r.mode, r.status, r.created_at
FROM scenario_runs r
ORDER BY r.id DESC LIMIT 20;

-- Quota check
SELECT owner_id, COUNT(*) AS n
FROM scenario_definitions
WHERE is_archived=0 GROUP BY owner_id;
```

---

## 16. Test progression — gate theo bước

Test sẽ khả thi theo bước build:

| Bước | Test khả thi | Gate |
|---|---|---|
| 1. Interfaces | Import check | — |
| 2. SQLite impl | **T12.1 repo** ✅ đã pass | — |
| 3. MockAuth | **T12.1 auth** ✅ đã pass | — |
| 4. Service layer | T7, T8 validate logic | — |
| 5. API routes | **T2, T3, T4, T5** | — |
| 6. Backend tests | **T12.2 integration** | **GATE 2** |
| 7-9. Frontend | **T9** UI | — |
| 10-11. E2E | **T1, T6, T10, T11** | — |
| 12. Security | **T8** hardening | — |
| Docs | **T14** guide viết xong | **GATE 3 = ship** |
