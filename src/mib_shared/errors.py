from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorEnvelope(BaseModel):
    """Standard error shape across all services (FR-BE-07, FR-BE-12)."""

    error_code: str
    message: str
    request_id: str | None = None
    trace_id: str | None = None


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        trace_id = request.headers.get("traceparent", "").split("-")[1:2]
        env = ErrorEnvelope(
            error_code="internal_error",
            message="An unexpected error occurred.",
            trace_id=trace_id[0] if trace_id else None,
        )
        # The trace_id is echoed in a header too, so the UI can show a reference
        # the user can quote to support (FR-BE-12).
        headers = {"x-trace-id": env.trace_id or ""}
        return JSONResponse(status_code=500, content=env.model_dump(), headers=headers)
