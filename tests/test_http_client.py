import httpx
import pytest

from mib_shared.http_client import (
    DEFAULT_TIMEOUT,
    RetryBudgetExceeded,
    RetryPolicy,
    TracedAsyncClient,
    TracedClient,
)
from mib_shared.tracing import new_trace_context, trace_context

INBOUND = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
NO_BACKOFF = RetryPolicy(attempts=3, backoff_seconds=0.0)


def _recording_transport(*statuses: int):
    """A transport that returns the given statuses in order, recording requests."""
    seen: list[httpx.Request] = []
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = remaining.pop(0) if remaining else 200
        return httpx.Response(status)

    return httpx.MockTransport(handler), seen


def _failing_transport(exc: Exception):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise exc

    return httpx.MockTransport(handler), seen


# --- timeouts ---------------------------------------------------------------

def test_a_timeout_is_applied_even_when_the_caller_supplies_none():
    client = TracedAsyncClient("http://svc")
    assert client._client.timeout == DEFAULT_TIMEOUT


def test_a_scalar_timeout_is_accepted():
    client = TracedAsyncClient("http://svc", timeout=1.5)
    assert client._client.timeout.read == 1.5


def test_a_retry_policy_must_allow_at_least_one_attempt():
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)


# --- trace propagation (FR-BE-25) ------------------------------------------

@pytest.mark.anyio
async def test_traceparent_is_propagated_as_a_child_span():
    transport, seen = _recording_transport(200)
    ctx = new_trace_context(INBOUND)
    async with TracedAsyncClient("http://svc", transport=transport) as client:
        with trace_context(ctx):
            await client.get("/thing")

    sent = seen[0].headers["traceparent"]
    assert sent.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    # Same trace, different span — the next hop is a child, not a repeat.
    assert ctx.span_id not in sent


@pytest.mark.anyio
async def test_an_explicit_traceparent_from_the_caller_wins():
    transport, seen = _recording_transport(200)
    explicit = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    async with TracedAsyncClient("http://svc", transport=transport) as client:
        with trace_context(new_trace_context(INBOUND)):
            await client.get("/thing", headers={"traceparent": explicit})
    assert seen[0].headers["traceparent"] == explicit


@pytest.mark.anyio
async def test_no_traceparent_is_invented_outside_a_trace():
    transport, seen = _recording_transport(200)
    async with TracedAsyncClient("http://svc", transport=transport) as client:
        await client.get("/thing")
    assert "traceparent" not in seen[0].headers


# --- bounded retries (FR-BE-21) --------------------------------------------

@pytest.mark.anyio
async def test_a_transport_error_is_retried_up_to_the_budget():
    transport, seen = _failing_transport(httpx.ConnectError("refused"))
    async with TracedAsyncClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        with pytest.raises(RetryBudgetExceeded) as excinfo:
            await client.get("/thing")
    assert len(seen) == 3
    assert excinfo.value.attempts == 3
    assert isinstance(excinfo.value.last, httpx.ConnectError)


@pytest.mark.anyio
async def test_a_retryable_status_is_retried_then_returned():
    transport, seen = _recording_transport(503, 503, 503)
    async with TracedAsyncClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        resp = await client.get("/thing")
    # The budget is spent, so the last response is handed back rather than raised:
    # the caller decides whether a 503 from a sibling is fatal to their own request.
    assert len(seen) == 3
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_a_recovered_call_stops_retrying():
    transport, seen = _recording_transport(503, 200)
    async with TracedAsyncClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        resp = await client.get("/thing")
    assert len(seen) == 2
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_a_client_error_is_not_retried():
    transport, seen = _recording_transport(404, 404, 404)
    async with TracedAsyncClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        resp = await client.get("/thing")
    assert len(seen) == 1
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_post_is_not_retried_by_default():
    # Repeating a POST that already created a subscription is worse than failing.
    transport, seen = _failing_transport(httpx.ConnectError("refused"))
    async with TracedAsyncClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        with pytest.raises(RetryBudgetExceeded):
            await client.post("/subscriptions")
    assert len(seen) == 1


@pytest.mark.anyio
async def test_post_is_retried_when_declared_idempotent():
    transport, seen = _failing_transport(httpx.ConnectError("refused"))
    async with TracedAsyncClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        with pytest.raises(RetryBudgetExceeded):
            await client.request("POST", "/webhooks", idempotent=True)
    assert len(seen) == 3


def test_backoff_grows_and_is_capped():
    policy = RetryPolicy(attempts=5, backoff_seconds=1.0, max_backoff_seconds=4.0)
    # Jittered, so assert the band rather than an exact value.
    assert 0.5 <= policy.delay_for(1) <= 1.0
    assert 1.0 <= policy.delay_for(2) <= 2.0
    assert 2.0 <= policy.delay_for(10) <= 4.0


# --- defined fallback (FR-BE-21) -------------------------------------------

@pytest.mark.anyio
async def test_the_fallback_replaces_the_failure():
    transport, _ = _failing_transport(httpx.ConnectError("refused"))
    async with TracedAsyncClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        # Stands in for degrading to lexical search when retrieval is down.
        result = await client.get("/embed", fallback=lambda _cause: "lexical")
    assert result == "lexical"


@pytest.mark.anyio
async def test_the_fallback_is_told_what_failed():
    transport, _ = _failing_transport(httpx.ConnectError("refused"))
    causes = []
    async with TracedAsyncClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        await client.get("/embed", fallback=lambda cause: causes.append(cause))
    assert isinstance(causes[0], httpx.ConnectError)


# --- the sync client behaves the same --------------------------------------

def test_sync_client_retries_and_propagates():
    transport, seen = _recording_transport(503, 200)
    with TracedClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        with trace_context(new_trace_context(INBOUND)):
            resp = client.get("/thing")
    assert resp.status_code == 200
    assert len(seen) == 2
    assert seen[0].headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")


def test_sync_client_falls_back():
    transport, _ = _failing_transport(httpx.ConnectError("refused"))
    with TracedClient("http://svc", transport=transport, retry=NO_BACKOFF) as client:
        assert client.get("/thing", fallback=lambda _c: None) is None
