import json

import httpx
import pytest
import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mib_shared.errors import install_error_handlers
from mib_shared.telemetry import configure_logging
from mib_shared.tracing import TracingMiddleware

INBOUND = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


class Payload(BaseModel):
    quantity: int


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TracingMiddleware)
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("the database ate it")

    @app.get("/missing")
    async def missing() -> dict:
        raise HTTPException(status_code=404, detail="No such regulation.")

    @app.post("/order")
    async def order(payload: Payload) -> dict:
        return {"ok": payload.quantity}

    return app


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=5.0
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.anyio
async def test_unhandled_error_returns_the_envelope_with_a_resolvable_trace_id():
    resp = await _request("GET", "/boom", headers={"traceparent": INBOUND})
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "internal_error"
    assert body["trace_id"] == TRACE_ID
    assert body["request_id"]
    # Also in the headers, for clients that never read an error body.
    assert resp.headers["x-trace-id"] == TRACE_ID


@pytest.mark.anyio
async def test_unhandled_error_does_not_leak_the_cause():
    resp = await _request("GET", "/boom")
    assert "database ate it" not in resp.text
    assert resp.json()["message"] == "An unexpected error occurred."


@pytest.mark.anyio
async def test_http_exception_uses_the_same_shape():
    resp = await _request("GET", "/missing", headers={"traceparent": INBOUND})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "not_found"
    assert body["message"] == "No such regulation."
    assert body["trace_id"] == TRACE_ID


@pytest.mark.anyio
async def test_validation_error_reports_fields_without_echoing_values():
    resp = await _request(
        "POST", "/order", json={"quantity": "not-a-number"}, headers={"traceparent": INBOUND}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "validation_error"
    assert body["trace_id"] == TRACE_ID
    assert body["details"][0]["field"].endswith("quantity")
    assert "not-a-number" not in resp.text


@pytest.mark.anyio
async def test_envelope_omits_absent_fields_rather_than_sending_nulls():
    resp = await _request("GET", "/missing")
    assert "details" not in resp.json()


# --- the 500 has to be greppable by the reference it hands out --------------

@pytest.mark.anyio
async def test_the_unhandled_error_log_line_carries_the_trace_id(capsys):
    """Regression: the log line holding the stack had no trace_id.

    ServerErrorMiddleware runs outside TracingMiddleware, so the contextvars are
    already unbound when this handler logs. The envelope reads the ASGI scope and
    survives; the log line did not. A user quoting a reference from the error page
    would find no line matching it — with the traceback in the one line that was
    missing it.
    """
    structlog.reset_defaults()
    configure_logging("INFO")

    resp = await _request("GET", "/boom", headers={"traceparent": INBOUND})
    assert resp.status_code == 500
    assert resp.json()["trace_id"] == TRACE_ID

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "unhandled_exception"
    # The same id the client was handed, so one grep finds both.
    assert line["trace_id"] == TRACE_ID
    assert line["request_id"] == resp.json()["request_id"]
    # And the stack is actually there to be read once found.
    assert "RuntimeError: the database ate it" in line["exception"]
