"""
api/app.py — FastAPI application (Phase 1b entry point).

Run: uvicorn api.app:app --host 0.0.0.0 --port 8000

Routes are split across api/routes/*.py.
Background tasks: recovery_loop (dead worker detection).
"""

import asyncio
import logging
import logging.handlers
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.recovery import recovery_loop
from api.routes import browser, cancel, health, result, resume, scenarios, screenshots, sessions, stream
from config import LOG_DIR
from services import scenario_service
from store.redis_client import get_async_redis

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

# Register route modules
for _router_module in (health, sessions, stream, resume, cancel, browser, screenshots, result, scenarios):
    app.include_router(_router_module.router)


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

    # Seed builtin scenarios (idempotent — không đè spec đã có)
    try:
        created = await scenario_service.seed_async(redis)
        if created:
            _log.info("Seeded %d builtin scenarios", created)
    except Exception as e:
        _log.error("Failed to seed scenarios: %s", e)

    _log.info("API started. Recovery loop running.")


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
