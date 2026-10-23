"""API error type and handlers. Error bodies: ``{"error": {"code", "message"}}``."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

logger = logging.getLogger("rootsignal.api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def _body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message), status_code=exc.status,
                            headers=exc.headers)

    @app.exception_handler(OperationalError)
    async def database_unavailable(_: Request, exc: OperationalError) -> JSONResponse:
        # Retryable for OTLP exporters (503); details stay in the server log.
        logger.error("database unavailable: %s", exc.orig)
        return JSONResponse(_body("unavailable", "database unavailable, retry later"),
                            status_code=503, headers={"Retry-After": "5"})
