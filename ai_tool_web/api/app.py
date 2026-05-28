"""
api/app.py — FastAPI application (Phase 1b entry point).

Run: uvicorn api.app:app --host 0.0.0.0 --port 8000

Routes are split across api/routes/*.py.
Background tasks: recovery_loop (dead worker detection).
"""

import asyncio
import logging
import logging.handlers
import os
from pathlib import Path

# Auto-load .env từ dev/deploy_server/.env (parent của ai_tool_web).
# Không-op nếu đã được PowerShell start_api_local.ps1 load sẵn.
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).resolve().parents[2] / ".env"
    if _env_file.exists():
        load_dotenv(_env_file, override=False)
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.exception_handlers import register_scenario_exception_handlers
from api.recovery import recovery_loop
from api.routes import (
    admin, auth, browser, cancel, health, result, resume,
    scenario_generate, scenarios, screenshots, sessions,
    stream, tasks, user_hooks, user_scenarios,
)
from auth.mock_provider import MockAuthProvider
from config import LOG_DIR
from services import scenario_service
from services.builtin_seeder import seed_builtin_from_yaml
from store.redis_client import get_async_redis
from store.scenario_repo import ScenarioRepository
# SqliteScenarioRepo / MysqlScenarioRepo lazy-import trong startup theo STORAGE_BACKEND
# để không buộc cài aiosqlite hoặc aiomysql khi chỉ dùng 1 backend.

_LOG_FORMAT = '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'

def _setup_logging() -> None:
    log_file = LOG_DIR / "system" / "api.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ),
    ]
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=handlers)

_setup_logging()
_log = logging.getLogger(__name__)

app = FastAPI(title="AI Tool Web", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Phase 3: restrict to specific origins
    allow_methods=["*"],
    allow_headers=["*"],
)

# Map scenario service exceptions → HTTP responses (Phase 1 user-scenario).
register_scenario_exception_handlers(app)

# Register route modules.
for _router_module in (
    health, sessions, stream, resume, cancel, browser, screenshots, result,
    tasks,                              # /v1/tasks/{task_id}/run — task-centric primary
    auth,                               # /v1/auth/me — admin status check
    admin,                              # /v1/admin/* — session/worker monitor (admin-only)
    user_scenarios, user_hooks,         # Phase 1 user CRUD (X-User-Id + SQLite)
    scenario_generate,                  # POST /v1/scenarios/generate (LLM-assisted)
):
    app.include_router(_router_module.router)

# Legacy `scenarios` router (X-Admin-Token CRUD, Redis) — remount dưới prefix
# /v1/admin để không conflict path với user_scenarios mới. Phase 2 sẽ deprecate
# hoàn toàn. `include_router(prefix=...)` sẽ CỘNG thêm prefix vào router prefix
# gốc ("/v1/scenarios" → "/v1/admin/v1/scenarios"), nên cần override qua param
# bằng cách mount với prefix thay thế toàn bộ router prefix.
# Cách đơn giản: tạo shim router mới, copy các route rồi include với prefix mới.
from fastapi import APIRouter as _APIRouter

_legacy_admin_router = _APIRouter(prefix="/v1/admin/scenarios", tags=["scenarios-admin-legacy"])
# Copy routes từ scenarios.router, strip path prefix "/v1/scenarios" khỏi path.
for _r in scenarios.router.routes:
    _path = _r.path
    if _path.startswith("/v1/scenarios"):
        _new_path = _path[len("/v1/scenarios"):] or "/"
    else:
        _new_path = _path
    _legacy_admin_router.add_api_route(
        _new_path,
        _r.endpoint,
        methods=list(_r.methods - {"HEAD"}),
        response_model=_r.response_model,
        status_code=_r.status_code,
        name=_r.name,
    )
app.include_router(_legacy_admin_router)


@app.on_event("startup")
async def _startup():
    # Import hooks để register vào HOOK_REGISTRY trước khi seed/validate spec.
    # Nếu fail (thiếu browser_adapter trong env test) → log warning, API vẫn boot.
    try:
        import scenarios.hooks  # noqa: F401
    except Exception as e:
        _log.warning("Không load được scenarios.hooks: %s", e)

    redis = get_async_redis()
    asyncio.create_task(recovery_loop(redis))

    # ── Legacy Redis seed (worker còn đọc từ Redis) ──────────────────────────
    try:
        created = await scenario_service.seed_async(redis)
        if created:
            _log.info("Seeded %d builtin scenarios vào Redis (legacy)", created)
    except Exception as e:
        _log.error("Failed to seed Redis scenarios: %s", e)

    # ── Phase 1: Auth provider (guard production) ────────────────────────────
    env = os.getenv("ENV", "development")
    provider_name = os.getenv("AUTH_PROVIDER", "mock")
    if provider_name == "mock":
        auth_provider = MockAuthProvider()
    else:
        raise ValueError(
            f"Unsupported AUTH_PROVIDER='{provider_name}'. "
            f"Supported: mock. (shared_secret/jwt chưa implement.)"
        )
    if auth_provider.must_fail_production() and env == "production":
        raise RuntimeError(
            f"AUTH_PROVIDER={auth_provider.name} không cho ENV=production. "
            f"Chuyển sang shared_secret/jwt hoặc ENV=development."
        )
    app.state.auth_provider = auth_provider
    _log.info("Auth provider: %s (ENV=%s)", auth_provider.name, env)

    # ── Phase 1: Scenario repository + auto-seed builtin (G2) ────────────────
    # Backend chọn qua STORAGE_BACKEND: 'sqlite' (default, dev) | 'mysql' (prod)
    backend = os.getenv("STORAGE_BACKEND", "sqlite").lower()
    repo: ScenarioRepository
    if backend == "mysql":
        from store.mysql_scenario_repo import MysqlScenarioRepo
        repo = MysqlScenarioRepo(
            host=os.environ["MYSQL_HOST"],
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            db=os.environ["MYSQL_DB"],
            pool_size=int(os.getenv("MYSQL_POOL_SIZE", "5")),
        )
        await repo.init()
        app.state.scenario_repo = repo
        _log.info(
            "Scenario repo initialized (mysql): %s:%s/%s",
            os.environ["MYSQL_HOST"],
            os.getenv("MYSQL_PORT", "3306"),
            os.environ["MYSQL_DB"],
        )
    elif backend == "sqlite":
        from store.sqlite_scenario_repo import SqliteScenarioRepo
        db_path = os.getenv("SCENARIO_DB_PATH", "./scenarios.db")
        repo = SqliteScenarioRepo(db_path)
        await repo.init()
        app.state.scenario_repo = repo
        _log.info("Scenario repo initialized (sqlite): %s", db_path)
    else:
        raise ValueError(
            f"Unsupported STORAGE_BACKEND='{backend}'. Use 'sqlite' or 'mysql'."
        )

    if await repo.count_builtin() == 0:
        try:
            n = await seed_builtin_from_yaml(repo)
            _log.info("Seeded %d builtin scenarios vào SQLite", n)
        except Exception as e:
            _log.error("SQLite builtin seed fail: %s", e)

    # scenario_images + scenario_input_fields repos DROPPED 2026-05-28.
    # Xem DB_CLEANUP_REVIEW.md. Visual hint giờ dùng GDrive URL trực tiếp
    # trong YAML image_hint; input fields edit qua raw YAML.

    _log.info("API started. Recovery loop running.")


@app.on_event("shutdown")
async def _shutdown():
    repo = getattr(app.state, "scenario_repo", None)
    if repo is not None:
        try:
            await repo.close()
            _log.info("Scenario repo closed")
        except Exception as e:
            _log.error("Error closing scenario repo: %s", e)

    # scenario_image_repo + input_field_repo DROPPED 2026-05-28.


@app.get("/v1/debug/test-upload")
async def debug_test_upload():
    """Kiểm tra kết nối upload server từ bên trong container."""
    import os
    import tempfile

    from services.artifact_uploader import ArtifactUploader, _upload_enabled

    if not _upload_enabled():
        return {
            "status": "disabled",
            "reason": "UPLOAD_URL / UPLOAD_KEY / UPLOAD_SECRET chưa set",
            "env": {
                "UPLOAD_URL": bool(os.getenv("UPLOAD_URL")),
                "UPLOAD_KEY": bool(os.getenv("UPLOAD_KEY")),
                "UPLOAD_SECRET": bool(os.getenv("UPLOAD_SECRET")),
            },
        }

    # Tạo file PNG 1x1 tạm
    PNG_1X1 = bytes([
        0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,
        0x00,0x00,0x00,0x0d,0x49,0x48,0x44,0x52,
        0x00,0x00,0x00,0x01,0x00,0x00,0x00,0x01,
        0x08,0x02,0x00,0x00,0x00,0x90,0x77,0x53,
        0xde,0x00,0x00,0x00,0x0c,0x49,0x44,0x41,
        0x54,0x08,0xd7,0x63,0xf8,0xcf,0xc0,0x00,
        0x00,0x00,0x02,0x00,0x01,0xe2,0x21,0xbc,
        0x33,0x00,0x00,0x00,0x00,0x49,0x45,0x4e,
        0x44,0xae,0x42,0x60,0x82,
    ])

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(PNG_1X1)
        tmp_path = f.name

    try:
        uploader = ArtifactUploader()
        cdn_url = uploader.upload_screenshot(tmp_path, "debug-test", step=0)
    finally:
        from pathlib import Path
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()

    if cdn_url:
        return {"status": "ok", "cdn_url": cdn_url}
    return {"status": "failed", "cdn_url": None,
            "hint": "Kiểm tra UPLOAD_URL / KEY / SECRET và log worker"}


@app.get("/v1/debug/test-cdn-fetch")
async def debug_test_cdn_fetch(url: str, mode: str = "direct"):
    """Test isolated download CDN từ bên trong API pod.

    Diagnose BUG-006 — vision_matcher fail download CDN. Test trong pod
    với 2 mode để xác định pod có direct egress hay phải qua proxy:

    - mode=direct: Session.trust_env=False → bypass proxy env vars,
      đi direct qua egress K8s pod
    - mode=proxy: default behavior — đi qua HTTP_PROXY env nếu có

    Usage:
      curl 'https://<API>/v1/debug/test-cdn-fetch?url=https://cdn.fstats.ai/...&mode=direct' \\
        -H 'X-User-Id: hiepqn'

    Response: success/fail + size + elapsed + error type/msg.

    Diagnosis:
    - direct OK + proxy OK → cả 2 path work (lạ, em đoán không xảy ra)
    - direct OK + proxy FAIL → fix vision_matcher trust_env=False đúng
    - direct FAIL + proxy OK → pod KHÔNG có direct egress, phải dùng proxy
    - direct FAIL + proxy FAIL → network/firewall block hoàn toàn
    """
    import os
    import time
    import requests

    started = time.monotonic()
    proxy_env = {
        "HTTP_PROXY": os.getenv("HTTP_PROXY", ""),
        "HTTPS_PROXY": os.getenv("HTTPS_PROXY", ""),
        "NO_PROXY": os.getenv("NO_PROXY", ""),
    }

    # Timeout ngắn (5s) để pod trả JSON error trước khi Ingress 504
    try:
        if mode == "direct":
            session = requests.Session()
            session.trust_env = False
            resp = session.get(url, timeout=(5, 5))
        else:
            # Default: trust_env=True → đọc env HTTP_PROXY → qua proxy
            resp = requests.get(url, timeout=(5, 5))

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "success": resp.status_code == 200,
            "status_code": resp.status_code,
            "size_bytes": len(resp.content),
            "elapsed_ms": elapsed_ms,
            "mode": mode,
            "proxy_env": proxy_env,
            "content_type": resp.headers.get("content-type", ""),
        }
    except requests.exceptions.ConnectTimeout as e:
        return {
            "success": False,
            "error_type": "ConnectTimeout",
            "error_msg": str(e)[:300],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "mode": mode,
            "proxy_env": proxy_env,
            "diagnosis": (
                "Pod không thể connect TCP — direct egress block hoặc proxy fail"
            ),
        }
    except requests.exceptions.ProxyError as e:
        return {
            "success": False,
            "error_type": "ProxyError",
            "error_msg": str(e)[:300],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "mode": mode,
            "proxy_env": proxy_env,
        }
    except Exception as e:
        return {
            "success": False,
            "error_type": type(e).__name__,
            "error_msg": str(e)[:300],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "mode": mode,
            "proxy_env": proxy_env,
        }


@app.get("/v1/debug/runner-logs")
async def debug_runner_logs(session_id: str = "", limit: int = 20):
    """List session.json files trong LLM_base/artifacts.
    Optional ?session_id=X để filter theo session_id field trong file content.
    """
    import json as _json
    from pathlib import Path as _Path

    here = _Path(__file__).resolve()
    llm_artifacts = here.parent.parent.parent / "LLM_base" / "artifacts"
    info: dict = {
        "scan_path": str(llm_artifacts),
        "exists": llm_artifacts.exists(),
    }
    if not llm_artifacts.exists():
        return info

    candidates = sorted(
        llm_artifacts.rglob("session.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    items = []
    matched = None
    for p in candidates:
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
            sid = data.get("session_id", "")
            entry = {
                "path": str(p),
                "mtime": p.stat().st_mtime,
                "session_id": sid,
                "scenario_id": data.get("scenario_id", ""),
                "total_steps": data.get("total_steps", 0),
                "size_bytes": p.stat().st_size,
            }
            items.append(entry)
            if session_id and sid == session_id:
                matched = entry
        except Exception as e:
            items.append({"path": str(p), "error": str(e)})

    info["count"] = len(items)
    info["items"] = items
    if session_id:
        info["match"] = matched
    return info


@app.get("/v1/debug/scenarios")
async def debug_scenarios(reseed: bool = False):
    """Diagnostic: list scenario:* keys trong Redis + HOOK_REGISTRY + import status.

    Query param `?reseed=true` để force re-seed builtin từ YAML vào Redis.
    """
    import importlib
    import sys
    import traceback

    redis = get_async_redis()

    scenario_keys = await redis.keys("scenario:*")
    scenario_keys = sorted([k.decode() if isinstance(k, bytes) else k for k in scenario_keys])

    # Hook registry diagnostics
    hooks_info: dict = {}
    try:
        from scenarios.hooks_registry import HOOK_REGISTRY
        hooks_info["registry_keys"] = sorted(HOOK_REGISTRY.keys())
        hooks_info["registry_count"] = len(HOOK_REGISTRY)
    except Exception as e:
        hooks_info["registry_error"] = f"{type(e).__name__}: {e}"

    # Re-import scenarios.hooks fresh, capture traceback
    try:
        if "scenarios.hooks" in sys.modules:
            importlib.reload(sys.modules["scenarios.hooks"])
        else:
            importlib.import_module("scenarios.hooks")
        hooks_info["scenarios_hooks_import"] = "ok"
    except Exception as e:
        hooks_info["scenarios_hooks_import"] = (
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )

    # MySQL repo
    repo = getattr(app.state, "scenario_repo", None)
    repo_info: dict = {}
    if repo is not None:
        try:
            repo_info = {
                "type": type(repo).__name__,
                "builtin_count": await repo.count_builtin(),
            }
        except Exception as e:
            repo_info = {"error": str(e)}

    # Force reseed
    reseeded = None
    if reseed:
        try:
            reseeded = await scenario_service.seed_async(redis)
        except Exception as e:
            reseeded = f"error: {type(e).__name__}: {e}\n{traceback.format_exc()}"

    # sys.path snapshot (top 10 entries)
    syspath = sys.path[:10]

    return {
        "redis_url_set": bool(os.getenv("REDIS_URL")),
        "redis_scenario_keys": scenario_keys,
        "redis_scenario_count": len(scenario_keys),
        "hooks": hooks_info,
        "scenario_repo": repo_info,
        "reseeded_count": reseeded,
        "syspath": syspath,
        "hint": (
            "Nếu hooks.registry_count=0 → import scenarios.hooks failed → "
            "validate_spec fail cho scenarios có hooks → seed skip silently. "
            "Check hooks.scenarios_hooks_import error cụ thể."
        ),
    }
