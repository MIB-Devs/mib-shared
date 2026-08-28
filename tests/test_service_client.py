"""Tests for `SiblingClient` (extracted from `mib-ai`'s and `mib-regulations`'s
duplicated sibling-service scaffolds, plus the lower half of `mib-rag`'s
regulations client).
"""
from __future__ import annotations

import anyio
import httpx
import pytest

from mib_shared import RetryPolicy, SiblingClient, SiblingUnavailable

BASE = "https://retrieval.example"


class TargetUnavailable(SiblingUnavailable):
    """A caller's own subclass, the way each adopting module defines one."""


def build(handler, *, base_url: str | None = BASE, **kwargs) -> SiblingClient:
    kwargs.setdefault("retry", RetryPolicy(attempts=2, backoff_seconds=0.001))
    return SiblingClient(
        caller="mib-ai",
        target="mib-retrieval",
        base_url=base_url,
        unavailable=TargetUnavailable,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_get_returns_the_underlying_response(monkeypatch):
    monkeypatch.setenv("MIB_SERVICE_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async def go():
        client = build(handler)
        response = await client.get("v1/retrieve")
        await client.aclose()
        return response

    response = anyio.run(go)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_credential_and_trace_headers_are_attached(monkeypatch):
    monkeypatch.setenv("MIB_SERVICE_TOKEN", "test-token")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async def go():
        client = build(handler)
        await client.get("v1/retrieve")
        await client.aclose()

    anyio.run(go)

    request = seen[0]
    assert request.headers["x-mib-service"] == "mib-ai"
    assert request.headers["x-mib-service-token"] == "test-token"
    assert "traceparent" in request.headers


def test_a_caller_supplied_header_is_preserved(monkeypatch):
    monkeypatch.setenv("MIB_SERVICE_TOKEN", "test-token")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async def go():
        client = build(handler)
        await client.get("v1/retrieve", headers={"x-extra": "yes"})
        await client.aclose()

    anyio.run(go)
    assert seen[0].headers["x-extra"] == "yes"


def test_get_is_retried_on_a_transport_failure_then_falls_back(monkeypatch):
    monkeypatch.setenv("MIB_SERVICE_TOKEN", "test-token")
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    async def go():
        client = build(handler)
        return await client.get("v1/retrieve", fallback=lambda exc: "degraded")

    result = anyio.run(go)
    assert result == "degraded"
    assert attempts["n"] == 2  # build()'s RetryPolicy(attempts=2)


def test_post_is_attempted_once_on_a_transport_failure(monkeypatch):
    monkeypatch.setenv("MIB_SERVICE_TOKEN", "test-token")
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    async def go():
        client = build(handler)
        return await client.post("v1/retrieve", fallback=lambda exc: "degraded")

    result = anyio.run(go)
    assert result == "degraded"
    assert attempts["n"] == 1


def test_no_fallback_raises_on_a_transport_failure(monkeypatch):
    monkeypatch.setenv("MIB_SERVICE_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async def go():
        client = build(handler)
        await client.get("v1/retrieve")

    with pytest.raises(httpx.HTTPError):
        anyio.run(go)


def test_request_returns_the_raw_response(monkeypatch):
    monkeypatch.setenv("MIB_SERVICE_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async def go():
        client = build(handler)
        response = await client.request("GET", "v1/thing/1")
        await client.aclose()
        return response

    response = anyio.run(go)
    assert isinstance(response, httpx.Response)
    assert response.status_code == 404


def test_unconfigured_base_url_raises_the_callers_subclass_lazily():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never be called")

    client = build(handler, base_url=None)

    async def go():
        await client.get("v1/retrieve")

    with pytest.raises(TargetUnavailable):
        anyio.run(go)


def test_unconfigured_base_url_does_not_raise_at_construction():
    client = SiblingClient(caller="mib-ai", target="mib-retrieval", base_url=None)
    assert client is not None


def test_aclose_before_any_call_is_a_no_op():
    client = SiblingClient(caller="mib-ai", target="mib-retrieval", base_url=None)
    anyio.run(client.aclose)
