import json

import httpx
import pytest
import structlog
from fastapi import FastAPI

from mib_shared.tracing import (
    TracingMiddleware,
    current_context,
    new_trace_context,
    parse_traceparent,
    restored_trace,
    trace_carrier,
    trace_context,
)

INBOUND = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TracingMiddleware)

    @app.get("/echo")
    async def echo() -> dict:
        ctx = current_context()
        return {"trace_id": ctx.trace_id, "span_id": ctx.span_id, "request_id": ctx.request_id}

    return app


async def _get(app: FastAPI, path: str, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=5.0
    ) as client:
        return await client.get(path, headers=headers or {})


# --- header parsing ---------------------------------------------------------

def test_valid_traceparent_is_parsed():
    ctx = parse_traceparent(INBOUND)
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.span_id == "00f067aa0ba902b7"
    assert ctx.sampled is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        "00-tooshort-00f067aa0ba902b7-01",
        # All-zero ids are invalid per W3C and must not be continued.
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",
    ],
)
def test_unusable_traceparent_is_treated_as_absent(value):
    # An upstream sending junk must not cost the caller their request.
    assert parse_traceparent(value) is None


def test_unsampled_flag_is_preserved():
    ctx = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00")
    assert ctx.sampled is False


def test_new_context_continues_the_trace_but_starts_a_new_span():
    ctx = new_trace_context(INBOUND)
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.span_id != "00f067aa0ba902b7"


def test_new_context_without_a_parent_starts_a_trace():
    ctx = new_trace_context(None)
    assert len(ctx.trace_id) == 32
    assert len(ctx.span_id) == 16
    assert ctx.traceparent().startswith("00-")


def test_child_keeps_the_trace_and_changes_the_span():
    parent = new_trace_context(INBOUND)
    child = parent.child()
    assert child.trace_id == parent.trace_id
    assert child.span_id != parent.span_id


# --- middleware -------------------------------------------------------------

@pytest.mark.anyio
async def test_inbound_trace_is_continued_into_the_handler():
    resp = await _get(_app(), "/echo", {"traceparent": INBOUND})
    assert resp.status_code == 200
    assert resp.json()["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.anyio
async def test_trace_ids_are_echoed_on_a_successful_response():
    # The reference support asks for must exist before anything went wrong.
    resp = await _get(_app(), "/echo", {"traceparent": INBOUND})
    assert resp.headers["x-trace-id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert resp.headers["x-request-id"]
    assert resp.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")


@pytest.mark.anyio
async def test_a_supplied_request_id_is_kept():
    resp = await _get(_app(), "/echo", {"x-request-id": "abc123"})
    assert resp.json()["request_id"] == "abc123"
    assert resp.headers["x-request-id"] == "abc123"


@pytest.mark.anyio
async def test_a_request_without_a_traceparent_still_gets_a_trace():
    resp = await _get(_app(), "/echo")
    assert len(resp.json()["trace_id"]) == 32


@pytest.mark.anyio
async def test_context_does_not_leak_between_requests():
    app = _app()
    first = (await _get(app, "/echo")).json()["trace_id"]
    second = (await _get(app, "/echo")).json()["trace_id"]
    assert first != second
    assert current_context() is None


# --- log correlation (NFR-16) ----------------------------------------------

def test_bound_context_lands_on_every_log_line(capsys):
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    ctx = new_trace_context(INBOUND, request_id="req-1")
    with trace_context(ctx):
        structlog.get_logger("t").info("did_a_thing")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert line["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert line["request_id"] == "req-1"
    assert line["span_id"] == ctx.span_id


def test_ids_are_gone_once_the_context_exits(capsys):
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    with trace_context(new_trace_context(INBOUND)):
        pass
    structlog.get_logger("t").info("outside")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "trace_id" not in line


# --- across a queue hop (FR-BE-13) -----------------------------------------

def test_carrier_round_trip_keeps_one_trace_across_the_job_boundary():
    request_ctx = new_trace_context(INBOUND, request_id="req-9")
    with trace_context(request_ctx):
        # What gets persisted on the job record at enqueue time.
        carrier = trace_carrier()

    assert carrier["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert carrier["x-request-id"] == "req-9"

    # Later, in the worker process:
    with restored_trace(carrier) as job_ctx:
        assert job_ctx.trace_id == request_ctx.trace_id
        assert job_ctx.request_id == "req-9"
        # The job is its own span, so its work is distinguishable from the
        # request that enqueued it.
        assert job_ctx.span_id != request_ctx.span_id


def test_carrier_is_empty_outside_a_trace():
    assert trace_carrier() == {}


def test_a_job_with_no_carrier_still_gets_its_own_trace():
    with restored_trace(None) as ctx:
        assert len(ctx.trace_id) == 32
