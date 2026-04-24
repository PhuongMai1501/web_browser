"""
api/exception_handlers.py — Map service exceptions → HTTP responses.

Đăng ký vào app:
    from api.exception_handlers import register_scenario_exception_handlers
    register_scenario_exception_handlers(app)
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.inputs_validator import InputValidationError
from services.user_scenario_service import (
    QuotaExceeded,
    ScenarioBadRequest,
    ScenarioConflict,
    ScenarioForbidden,
    ScenarioNotFound,
)


_log = logging.getLogger(__name__)


def register_scenario_exception_handlers(app: FastAPI) -> None:
    """Gắn 5 exception handler vào FastAPI app."""

    @app.exception_handler(ScenarioNotFound)
    async def _not_found(_: Request, exc: ScenarioNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ScenarioForbidden)
    async def _forbidden(_: Request, exc: ScenarioForbidden):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ScenarioConflict)
    async def _conflict(_: Request, exc: ScenarioConflict):
        return JSONResponse(
            status_code=409,
            content={"code": exc.code, "detail": exc.message},
        )

    @app.exception_handler(ScenarioBadRequest)
    async def _bad_request(_: Request, exc: ScenarioBadRequest):
        return JSONResponse(
            status_code=400,
            content={"detail": "Bad YAML", "errors": exc.errors},
        )

    @app.exception_handler(QuotaExceeded)
    async def _quota(_: Request, exc: QuotaExceeded):
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    @app.exception_handler(InputValidationError)
    async def _inputs(_: Request, exc: InputValidationError):
        return JSONResponse(
            status_code=400,
            content={"detail": "Input validation failed", "errors": exc.errors},
        )
