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
from api.routes import browser, cancel, health, resume, screenshots, sessions, stream
from config import LOG_DIR
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
for _router_module in (health, sessions, stream, resume, cancel, browser, screenshots):
    app.include_router(_router_module.router)


@app.on_event("startup")
async def _startup():
    redis = get_async_redis()
    asyncio.create_task(recovery_loop(redis))
    _log.info("API started. Recovery loop running.")
