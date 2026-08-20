from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# W3C trace context (FR-BE-08, FR-BE-25). One request is one trace across every
# hop, which is the whole reason a four-service split stays debuggable (R-10).
TRACEPARENT_HEADER = "traceparent"
SCOPE_KEY = "mib_trace_context"
REQUEST_ID_HEADER = "x-request-id"
TRACE_ID_HEADER = "x-trace-id"

_VERSION = "00"
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ALL_ZERO_TRACE = "0" * 32
_ALL_ZERO_SPAN = "0" * 16

_current: ContextVar[TraceContext | None] = ContextVar("mib_trace_context", default=None)


@dataclass(frozen=True)
class TraceContext:
    """The identifiers that travel with one request.

    ``trace_id`` is stable for the whole request; ``span_id`` identifies this hop
    and changes each time the request crosses a service boundary.
    """

    trace_id: str
    span_id: str
    sampled: bool = True
    request_id: str | None = None

    def traceparent(self) -> str:
        return f"{_VERSION}-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"

    def child(self) -> TraceContext:
        """A new span in the same trace — used for each outbound call."""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=_new_span_id(),
            sampled=self.sampled,
            request_id=self.request_id,
        )


def _new_trace_id() -> str:
    return os.urandom(16).hex()


def _new_span_id() -> str:
    return os.urandom(8).hex()


def parse_traceparent(value: str | None) -> TraceContext | None:
    """Parse an inbound ``traceparent``, or return None if it is unusable.

    A malformed or all-zero header is treated as absent rather than as an error:
    an upstream sending junk should not cost the caller their request, and a new
    trace still gets started (W3C §3.2.2.3).
    """
    if not value:
        return None
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if not match:
        return None
    _, trace_id, span_id, flags = match.groups()
    if trace_id == _ALL_ZERO_TRACE or span_id == _ALL_ZERO_SPAN:
        return None
    return TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        sampled=bool(int(flags, 16) & 0x01),
    )


def new_trace_context(parent: str | None = None, *, request_id: str | None = None) -> TraceContext:
    """Continue the inbound trace if there is one, otherwise start a new one.

    Traefik initiates the trace at the edge (FR-BE-08), so in production ``parent``
    is normally present; a service called directly still gets a trace of its own.
    """
    inbound = parse_traceparent(parent)
    if inbound is not None:
        # Same trace, new span for this hop.
        return TraceContext(
            trace_id=inbound.trace_id,
            span_id=_new_span_id(),
            sampled=inbound.sampled,
            request_id=request_id,
        )
    return TraceContext(
        trace_id=_new_trace_id(),
        span_id=_new_span_id(),
        sampled=True,
        request_id=request_id,
    )


def current_context() -> TraceContext | None:
    return _current.get()


def context_from_scope(scope: Any) -> TraceContext | None:
    """The request's context, readable even after the contextvar was reset."""
    if isinstance(scope, dict):
        ctx = scope.get(SCOPE_KEY)
        if isinstance(ctx, TraceContext):
            return ctx
    return current_context()


def bind_context(ctx: TraceContext) -> Token:
    """Bind the context for this task and put its ids on every log line (NFR-16)."""
    structlog.contextvars.bind_contextvars(
        trace_id=ctx.trace_id,
        span_id=ctx.span_id,
        **({"request_id": ctx.request_id} if ctx.request_id else {}),
    )
    return _current.set(ctx)


def reset_context(token: Token) -> None:
    structlog.contextvars.unbind_contextvars("trace_id", "span_id", "request_id")
    _current.reset(token)


@contextmanager
def trace_context(ctx: TraceContext):
    token = bind_context(ctx)
    try:
        yield ctx
    finally:
        reset_context(token)


# --- crossing a queue boundary (FR-BE-13) ---------------------------------
# A background job is a different process, later in time, so the context has to
# be persisted on the job record and rebound when it runs. Without this the
# trace stops at the enqueue and the slow half of the work is invisible.

def trace_carrier() -> dict[str, str]:
    """The fields to persist on a job record so its trace can be re-attached."""
    ctx = current_context()
    if ctx is None:
        return {}
    carrier = {TRACEPARENT_HEADER: ctx.traceparent()}
    if ctx.request_id:
        carrier[REQUEST_ID_HEADER] = ctx.request_id
    return carrier


@contextmanager
def restored_trace(carrier: dict[str, Any] | None):
    """Re-attach a persisted trace while a job runs (FR-BE-13).

    The job is a child span of the request that enqueued it, so the trace_id
    survives the hop and the request_id still resolves to the original caller.
    """
    carrier = carrier or {}
    ctx = new_trace_context(
        carrier.get(TRACEPARENT_HEADER),
        request_id=carrier.get(REQUEST_ID_HEADER),
    )
    with trace_context(ctx) as bound:
        yield bound


class TracingMiddleware:
    """Pure-ASGI middleware: extract, bind, echo.

    Pure ASGI rather than BaseHTTPMiddleware so the context is bound in the same
    task that runs the endpoint — a contextvar set in a wrapper task is not
    visible to the handler, which would silently drop trace_id from every log
    line the request produces.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(REQUEST_ID_HEADER) or os.urandom(8).hex()
        ctx = new_trace_context(headers.get(TRACEPARENT_HEADER), request_id=request_id)
        # Also stashed on the scope, because Starlette's ServerErrorMiddleware sits
        # OUTSIDE this one: an unhandled exception is turned into a response after
        # this context manager has exited, when the contextvar is already reset.
        # Without this, the 500 that most needs a trace reference would be the one
        # response without one.
        scope[SCOPE_KEY] = ctx

        async def send_with_trace(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Echoed on every response, success included: the reference a user
                # quotes to support has to exist before anything goes wrong
                # (FR-BE-12).
                out = MutableHeaders(scope=message)
                out[TRACE_ID_HEADER] = ctx.trace_id
                out[REQUEST_ID_HEADER] = request_id
                out[TRACEPARENT_HEADER] = ctx.traceparent()
            await send(message)

        with trace_context(ctx):
            await self.app(scope, receive, send_with_trace)
