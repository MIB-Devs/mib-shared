from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from mib_shared.telemetry import get_logger
from mib_shared.tracing import REQUEST_ID_HEADER, TRACE_ID_HEADER, context_from_scope

logger = get_logger(__name__)

# Status codes that map to a stable, client-readable code. Anything not listed
# is an internal error and says nothing more than that.
_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    503: "unavailable",
}


class ErrorEnvelope(BaseModel):
    """Standard error shape across all services (FR-BE-07, FR-BE-12).

    ``trace_id`` is the reference a user can quote to support, so it is present
    on every error regardless of cause, and echoed in the headers too for
    clients that never read the body.
    """

    error_code: str
    message: str
    request_id: str | None = None
    trace_id: str | None = None
    details: list[dict] | None = None


def _envelope(
    request: Request,
    error_code: str,
    message: str,
    details: list[dict] | None = None,
) -> ErrorEnvelope:
    ctx = context_from_scope(request.scope)
    return ErrorEnvelope(
        error_code=error_code,
        message=message,
        request_id=ctx.request_id if ctx else None,
        trace_id=ctx.trace_id if ctx else None,
        details=details,
    )


def _response(status_code: int, env: ErrorEnvelope) -> JSONResponse:
    headers = {}
    if env.trace_id:
        headers[TRACE_ID_HEADER] = env.trace_id
    if env.request_id:
        headers[REQUEST_ID_HEADER] = env.request_id
    return JSONResponse(
        status_code=status_code,
        content=env.model_dump(exclude_none=True),
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Give a service the one error shape every client can rely on.

    Validation errors return field-level detail but never the input values. The unhandled-exception
    handler passes ``trace_id`` and ``request_id`` explicitly: ServerErrorMiddleware sits outside
    TracingMiddleware, so the contextvars are already unbound and the 500 log line would otherwise
    lack the reference the user quotes (FR-BE-12).
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODE_BY_STATUS.get(exc.status_code, "error")
        detail = exc.detail if isinstance(exc.detail, str) else code
        return _response(exc.status_code, _envelope(request, code, detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(p) for p in err.get("loc", ())), "reason": err.get("msg", "")}
            for err in exc.errors()
        ]
        return _response(
            422,
            _envelope(request, "validation_error", "The request body failed validation.", details),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        env = _envelope(request, "internal_error", "An unexpected error occurred.")
        logger.exception(
            "unhandled_exception",
            trace_id=env.trace_id,
            request_id=env.request_id,
            path=request.url.path,
            method=request.method,
            error_code=env.error_code,
        )
        return _response(500, env)


__all__ = ["ErrorEnvelope", "install_error_handlers", "HTTPException"]
