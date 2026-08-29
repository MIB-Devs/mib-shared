"""Tests for `OSSClient` (extracted for `mib-regulations#22`'s OSS-artifact
reads, reused later by `mib-ingestion`'s writer).

Every test drives a fake transport rather than the network, same approach
`test_embeddings.py` uses for the client built on the same `TracedAsyncClient`.
"""
from __future__ import annotations

import anyio
import httpx
import pytest

from mib_shared import RetryPolicy
from mib_shared.oss import OSSClient

ENDPOINT = "https://bucket.oss-ap-southeast-5.example.com"


def build(handler, **kwargs) -> OSSClient:
    kwargs.setdefault("retry", RetryPolicy(attempts=2, backoff_seconds=0.001))
    return OSSClient(
        endpoint=ENDPOINT,
        bucket="mib-regulations-content",
        access_key_id="test-key-id",
        access_key_secret="test-key-secret",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def get_object(client: OSSClient, key: str, **kwargs):
    async def run():
        async with client:
            return await client.get_object(key, **kwargs)

    return anyio.run(run)


def test_sends_a_signed_authorization_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["date"] = request.headers.get("date")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"hello")

    get_object(build(handler), "v1/html/some-id")

    assert seen["auth"].startswith("OSS test-key-id:")
    assert seen["date"]  # a real RFC 1123 date was sent
    assert seen["url"].endswith("/v1/html/some-id")


def test_a_leading_slash_on_the_key_does_not_double_up():
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, content=b"hello")

    get_object(build(handler), "/v1/html/some-id")
    assert urls[0].count("//v1") == 0


def test_a_successful_get_returns_the_response():
    response = get_object(build(lambda r: httpx.Response(200, content=b"<html></html>")), "k")
    assert response.status_code == 200
    assert response.content == b"<html></html>"


def test_a_404_is_returned_as_is_not_swallowed():
    """The caller decides what a missing object means (e.g. 'not ingested
    yet') — this client must not turn it into an error or a fallback call."""
    called = []
    response = get_object(
        build(lambda r: httpx.Response(404)), "k", fallback=lambda cause: called.append(cause)
    )
    assert response.status_code == 404
    assert called == []


def test_a_persistent_transport_failure_uses_the_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    response = get_object(build(handler), "k", fallback=lambda cause: "degraded")
    assert response == "degraded"


def test_a_persistent_5xx_status_is_routed_to_the_fallback():
    """Fixed by `mib-shared#18`: a failing *status* left once the retry budget
    is exhausted is now routed through the fallback the same as a transport
    error — a dependency that is up but unhealthy answers 503, which is
    exactly the case a caller's fallback exists to degrade around."""
    called = []

    def fallback(cause):
        called.append(cause)
        return "degraded"

    response = get_object(build(lambda r: httpx.Response(503)), "k", fallback=fallback)
    assert response == "degraded"
    assert called[0].status_code == 503


@pytest.mark.parametrize(
    "missing",
    ["endpoint", "bucket", "access_key_id", "access_key_secret"],
)
def test_construction_requires_every_field(missing):
    kwargs = {
        "endpoint": ENDPOINT,
        "bucket": "b",
        "access_key_id": "id",
        "access_key_secret": "secret",
    }
    kwargs[missing] = ""
    with pytest.raises(ValueError):
        OSSClient(**kwargs)
