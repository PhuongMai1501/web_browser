# Wire Phase 1 backend vào `api/app.py`

> **Status:** Backend code + tests xong (GATE 2 pass). File này hướng dẫn
> apply vào `api/app.py` chính để chạy cùng uvicorn.

---

## 1. Chuẩn bị

### 1.1 Dependencies

Thêm vào `requirements.txt`:
```
aiosqlite>=0.22
```

(PyYAML + FastAPI đã có.)

### 1.2 Env mới cho `.env`

```
# Phase 1 user-scenario system
AUTH_PROVIDER=mock           # mock | shared_secret (chưa impl) | jwt (Phase 2)
ENV=development              # development | production
SCENARIO_DB_PATH=./scenarios.db
```

**Warning:** `AUTH_PROVIDER=mock` + `ENV=production` → API refuse start (guard §0).

---

## 2. Thay đổi trong `api/app.py`

### 2.1 Import (thêm vào đầu file)

```python
import os

from store.sqlite_scenario_repo import SqliteScenarioRepo
from auth.mock_provider import MockAuthProvider
from auth.providers import AuthProvider
from services.builtin_seeder import seed_builtin_from_yaml
from api.exception_handlers import register_scenario_exception_handlers
from api.routes import user_scenarios, user_hooks
```

### 2.2 Đăng ký exception handlers (sau khi tạo `app`)

```python
app = FastAPI(title="AI Tool Web", version="2.0.0")
# ... CORSMiddleware ...

register_scenario_exception_handlers(app)
```

### 2.3 Rename/disable old scenarios router

File `api/routes/scenarios.py` cũ (X-Admin-Token) sẽ conflict prefix với `user_scenarios`. 3 option:

**(A) Tắt hẳn** — nếu không ai dùng `/v1/scenarios` cũ qua API:
```python
# BEFORE
for _router_module in (health, sessions, stream, resume, cancel, browser,
                       screenshots, result, scenarios):    # <-- scenarios ở đây
    app.include_router(_router_module.router)

# AFTER
for _router_module in (health, sessions, stream, resume, cancel, browser,
                       screenshots, result):   # bỏ scenarios
    app.include_router(_router_module.router)

# Thêm router Phase 1 mới
app.include_router(user_scenarios.router)
app.include_router(user_hooks.router)
```

**(B) Đổi prefix** old → `/v1/admin/scenarios` cho backward compat:
```python
# Trong api/routes/scenarios.py, sửa router definition:
router = APIRouter(prefix="/v1/admin/scenarios", tags=["scenarios-admin"])
```

**(C) Dual path:** chạy song song, new users dùng X-User-Id, old vẫn X-Admin-Token trên prefix khác nhau. Phức tạp — KHUYẾN NGHỊ option (A).

### 2.4 Startup hook — init repo + auth + seed

```python
@app.on_event("startup")
async def _startup_scenario_v2():
    # Auth provider
    env = os.getenv("ENV", "development")
    provider_name = os.getenv("AUTH_PROVIDER", "mock")
    if provider_name == "mock":
        provider = MockAuthProvider()
    else:
        raise ValueError(f"Unsupported AUTH_PROVIDER={provider_name}")

    if provider.must_fail_production() and env == "production":
        raise RuntimeError(
            f"AUTH_PROVIDER={provider.name} không cho ENV=production. "
            f"Set AUTH_PROVIDER=shared_secret hoặc jwt trước."
        )
    app.state.auth_provider = provider
    _log.info("Auth provider: %s", provider.name)

    # Scenario repository (SQLite Phase 1)
    db_path = os.getenv("SCENARIO_DB_PATH", "./scenarios.db")
    repo = SqliteScenarioRepo(db_path)
    await repo.init()
    app.state.scenario_repo = repo
    _log.info("Scenario repo initialized: %s", db_path)

    # Auto-seed builtin nếu DB rỗng (G2)
    if await repo.count_builtin() == 0:
        try:
            n = await seed_builtin_from_yaml(repo)
            _log.info("Seeded %d builtin scenarios vào SQLite", n)
        except Exception as e:
            _log.error("Seed builtin fail: %s", e)


@app.on_event("shutdown")
async def _shutdown_scenario_v2():
    repo = getattr(app.state, "scenario_repo", None)
    if repo is not None:
        await repo.close()
```

---

## 3. Verify wire-in

### 3.1 Start lại API

```bash
cd dev/deploy_server
bash start_api.sh
```

Log mong đợi:
```
Auth provider: mock
Scenario repo initialized: ./scenarios.db
Seeded N builtin scenarios vào SQLite
API started. Recovery loop running.
```

### 3.2 Smoke test qua curl

```bash
export SERVER=http://localhost:9000

# 1. Auth required
curl -i $SERVER/v1/scenarios
# → 401 Unauthenticated

# 2. List với auth
curl -H "X-User-Id: hiepqn" $SERVER/v1/scenarios | python3 -m json.tool
# → list builtin scenarios

# 3. Validate
curl -X POST -H "X-User-Id: hiepqn" -H "Content-Type: application/json" \
  -d '{"raw_yaml": "id: x\ndisplay_name: Test\ngoal: test"}' \
  $SERVER/v1/scenarios/validate
# → {"parse_ok":true, "validation_ok":true, ...}

# 4. Create
curl -X POST -H "X-User-Id: hiepqn" -H "Content-Type: application/json" \
  -d '{"raw_yaml": "id: x\ndisplay_name: My First\ngoal: Do X"}' \
  $SERVER/v1/scenarios
# → 201 với definition JSON

# 5. Hooks
curl -H "X-User-Id: hiepqn" $SERVER/v1/hooks
# → [...]
```

### 3.3 Inspect DB

```bash
sqlite3 ./scenarios.db "SELECT id, source_type, published_revision_id FROM scenario_definitions;"
sqlite3 ./scenarios.db "SELECT scenario_id, version_no, static_validation_status FROM scenario_revisions;"
```

---

## 4. Production readiness checklist

- [ ] `AUTH_PROVIDER` set (không để default mock)
- [ ] `ENV=production` — guard sẽ chặn mock auth
- [ ] `SCENARIO_DB_PATH` trỏ persistent volume (PVC trong K8s)
- [ ] Builtin YAML nằm trong Docker image ở `LLM_base/scenarios/builtin/`
- [ ] Old `/v1/scenarios` router đã tắt hoặc rename (§2.3)
- [ ] Frontend UI update sang `X-User-Id` header (Phase 1 bước 7-9)

---

## 5. Rollback plan

Nếu sau khi wire thấy API fail:

1. **Revert app.py** — git checkout file cũ
2. **Xoá SQLite DB** — `rm ./scenarios.db` (data user chưa dùng production)
3. Old Redis-based scenario system vẫn chạy (không đụng)
4. Restart API

Risk thấp vì Phase 1 SQLite độc lập hoàn toàn với flow production cũ
(Redis scenario + worker không thay đổi).

---

## 6. Phase 1 bước tiếp theo (sau khi wire xong)

- Bước 7-9: Frontend UI (`/scenarios` page, Monaco/textarea editor, clone modal)
- Bước 10: E2E test tích hợp UI + backend
- Bước 11: Security hardening (quota, domain whitelist check)
- Bước 12: Docs cho user — `USER_SCENARIO_GUIDE.md`
