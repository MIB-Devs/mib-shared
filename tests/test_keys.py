import asyncio
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mib_shared.http_client import RetryPolicy, TracedAsyncClient
from mib_shared.keys import JWKSCache, KeyUnavailable, StaticKey


def _jwk(kid: str) -> dict:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    document = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    document.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return document


class FakeIdentity:
    """Stands in for mib-identity's published key endpoint, counting calls."""

    def __init__(self, *kids: str):
        self.keys = {kid: _jwk(kid) for kid in kids}
        self.calls = 0
        self.requested: list[str] = []
        self.status = 200
        self.body: dict | None = None
        self.headers: dict[str, str] = {}

    def serve(self, *kids: str) -> None:
        """Rotate: publish a different set of keys from now on."""
        self.keys = {kid: _jwk(kid) for kid in kids}

    def transport(self) -> httpx.MockTransport:
        """Answers a trailing-slash path with FastAPI's 307 (#36), which the client does not follow.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            path = request.url.path
            self.requested.append(path)
            if path.endswith("/") and path != "/":
                target = str(request.url.copy_with(path=path.rstrip("/")))
                return httpx.Response(307, headers={"location": target})
            if self.status != 200:
                return httpx.Response(self.status)
            return httpx.Response(
                200, json=self.body or {"keys": list(self.keys.values())}, headers=self.headers
            )

        return httpx.MockTransport(handler)

    def cache(self, **kwargs) -> JWKSCache:
        client = TracedAsyncClient("http://identity.test/jwks", transport=self.transport())
        return JWKSCache(jwks_url="http://identity.test/jwks", client=client, **kwargs)


@pytest.mark.anyio
async def test_the_key_is_fetched_once_and_then_cached():
    """The whole point: no network hop per request (FR-SSO-02)."""
    identity = FakeIdentity("key-1")
    cache = identity.cache(ttl_seconds=300)

    for _ in range(5):
        assert await cache.get("key-1") is not None
    assert identity.calls == 1


@pytest.mark.anyio
async def test_refresh_requests_the_exact_url_without_a_trailing_slash():
    """httpx turns `base_url` + `GET ""` into `…/jwks/`; FastAPI 307s that away (#36)."""
    identity = FakeIdentity("key-1")
    cache = identity.cache(ttl_seconds=300)

    assert await cache.get("key-1") is not None
    assert identity.requested == ["/jwks"]


@pytest.mark.anyio
async def test_a_trailing_slash_on_the_configured_url_is_dropped():
    identity = FakeIdentity("key-1")
    client = TracedAsyncClient("http://identity.test/jwks/", transport=identity.transport())
    cache = JWKSCache(jwks_url="http://identity.test/jwks/", client=client)

    assert await cache.get("key-1") is not None
    assert identity.requested == ["/jwks"]


@pytest.mark.anyio
async def test_an_unknown_kid_triggers_a_refresh_so_rotation_works():
    """Trap 1: a timer-only cache rejects every token for its TTL after rotation."""
    identity = FakeIdentity("old-key")
    cache = identity.cache(ttl_seconds=300, min_refresh_interval=0)
    await cache.get("old-key")
    assert identity.calls == 1

    identity.serve("new-key")
    assert await cache.get("new-key") is not None
    assert identity.calls == 2


@pytest.mark.anyio
async def test_unknown_kids_cannot_be_used_as_a_fetch_amplifier():
    """Trap 2: otherwise a stream of random kids becomes a stream of outbound calls.

    The cooldown runs from the last attempt, including the successful one, so twenty unknown kids
    cost no outbound call. The trade (DEFAULT_MIN_REFRESH_INTERVAL): a rotation within the cooldown
    is not seen at once, so identity must publish a key before signing with it.
    """
    identity = FakeIdentity("key-1")
    cache = identity.cache(ttl_seconds=300, min_refresh_interval=60)
    await cache.get("key-1")
    assert identity.calls == 1

    for i in range(20):
        with pytest.raises(KeyUnavailable):
            await cache.get(f"garbage-{i}")

    assert identity.calls == 1


@pytest.mark.anyio
async def test_a_failed_refresh_keeps_serving_cached_keys():
    """Trap 3: fail STATIC.

    An identity outage must not take every service's authentication with it —
    that is the entire benefit of verifying locally.
    """
    identity = FakeIdentity("key-1")
    cache = identity.cache(ttl_seconds=-1, min_refresh_interval=0)
    await cache.get("key-1")

    identity.status = 503
    for _ in range(3):
        assert await cache.get("key-1") is not None
    assert identity.calls > 1


@pytest.mark.anyio
async def test_with_nothing_cached_a_failed_refresh_fails_closed():
    identity = FakeIdentity("key-1")
    identity.status = 503
    cache = identity.cache()
    with pytest.raises(KeyUnavailable):
        await cache.get("key-1")


@pytest.mark.anyio
async def test_a_withdrawn_key_stops_being_accepted():
    """Replace, not merge — otherwise a revoked key is honoured forever."""
    identity = FakeIdentity("key-1")
    cache = identity.cache(ttl_seconds=-1, min_refresh_interval=0)
    await cache.get("key-1")

    identity.serve("key-2")
    await cache.get("key-2")
    with pytest.raises(KeyUnavailable):
        await cache.get("key-1")


@pytest.mark.anyio
async def test_a_token_without_a_kid_resolves_only_when_one_key_exists():
    identity = FakeIdentity("only-key")
    cache = identity.cache(ttl_seconds=300)
    assert await cache.get(None) is not None


@pytest.mark.anyio
async def test_a_token_without_a_kid_is_refused_when_several_keys_exist():
    """Trying each key until one verifies would turn a missing header into a
    signature oracle, so it is refused instead."""
    identity = FakeIdentity("key-1", "key-2")
    cache = identity.cache(ttl_seconds=300, min_refresh_interval=0)
    with pytest.raises(KeyUnavailable):
        await cache.get(None)


@pytest.mark.anyio
async def test_one_malformed_entry_does_not_discard_the_document():
    identity = FakeIdentity("good-key")
    identity.body = {"keys": [{"kid": "broken", "kty": "nonsense"}, *identity.keys.values()]}
    cache = identity.cache(ttl_seconds=300)
    assert await cache.get("good-key") is not None


@pytest.mark.anyio
async def test_an_empty_document_does_not_wipe_the_cache():
    identity = FakeIdentity("key-1")
    cache = identity.cache(ttl_seconds=-1, min_refresh_interval=0)
    await cache.get("key-1")

    identity.body = {"keys": []}
    assert await cache.get("key-1") is not None


@pytest.mark.anyio
async def test_static_key_needs_no_network_at_all():
    static = StaticKey(public_key="-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----")
    assert await static.get() == static.public_key
    assert await static.get("any-kid") == static.public_key


class Clock:
    """Replaces `time.monotonic` in `mib_shared.keys`, so a test moves time by hand."""

    def __init__(self, monkeypatch) -> None:
        self.now = 1000.0
        monkeypatch.setattr("mib_shared.keys.time.monotonic", lambda: self.now)


async def _calls_after(identity: FakeIdentity, cache: JWKSCache, clock: Clock, wait: float) -> int:
    """Fetch once, let `wait` seconds pass, ask for the key again, and count the fetches."""
    await cache.get("key-1")
    clock.now += wait
    await cache.get("key-1")
    return identity.calls


@pytest.mark.anyio
async def test_a_max_age_shorter_than_the_ttl_shortens_it(monkeypatch):
    identity = FakeIdentity("key-1")
    identity.headers = {"cache-control": "public, max-age=60"}
    clock = Clock(monkeypatch)
    cache = identity.cache(ttl_seconds=600, min_refresh_interval=30)

    assert await _calls_after(identity, cache, clock, wait=90) == 2


@pytest.mark.anyio
async def test_a_max_age_is_not_waited_out_before_it_lapses(monkeypatch):
    identity = FakeIdentity("key-1")
    identity.headers = {"cache-control": "max-age=60"}
    clock = Clock(monkeypatch)
    cache = identity.cache(ttl_seconds=600, min_refresh_interval=30)

    assert await _calls_after(identity, cache, clock, wait=45) == 1


@pytest.mark.anyio
async def test_a_max_age_longer_than_the_ceiling_is_clamped(monkeypatch):
    identity = FakeIdentity("key-1")
    identity.headers = {"cache-control": "max-age=999999"}
    clock = Clock(monkeypatch)
    cache = identity.cache(ttl_seconds=600, min_refresh_interval=30, max_ttl_seconds=900)

    assert await _calls_after(identity, cache, clock, wait=901) == 2


@pytest.mark.anyio
async def test_a_max_age_longer_than_the_ttl_but_under_the_ceiling_is_honoured(monkeypatch):
    identity = FakeIdentity("key-1")
    identity.headers = {"cache-control": "max-age=1200"}
    clock = Clock(monkeypatch)
    cache = identity.cache(ttl_seconds=600, min_refresh_interval=30, max_ttl_seconds=3600)

    assert await _calls_after(identity, cache, clock, wait=700) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("header", [None, "", "public", "max-age=abc", "max-age=-5", "s-maxage=5"])
async def test_without_a_usable_max_age_the_ttl_applies(monkeypatch, header):
    identity = FakeIdentity("key-1")
    if header is not None:
        identity.headers = {"cache-control": header}
    clock = Clock(monkeypatch)
    cache = identity.cache(ttl_seconds=600, min_refresh_interval=30)

    assert await _calls_after(identity, cache, clock, wait=599) == 1
    assert await _calls_after(identity, cache, clock, wait=2) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("header", ["no-store", "no-cache", "max-age=0", "private, max-age=0"])
async def test_no_store_and_zero_are_floored_by_the_refresh_interval(monkeypatch, header):
    """An issuer's 0 must not turn every request into a call to identity."""
    identity = FakeIdentity("key-1")
    identity.headers = {"cache-control": header}
    clock = Clock(monkeypatch)
    cache = identity.cache(ttl_seconds=600, min_refresh_interval=30)

    await cache.get("key-1")
    for _ in range(20):
        clock.now += 1
        await cache.get("key-1")
    assert identity.calls == 1

    clock.now += 30
    await cache.get("key-1")
    assert identity.calls == 2


@pytest.mark.anyio
async def test_the_lifetime_follows_the_latest_response(monkeypatch):
    identity = FakeIdentity("key-1")
    identity.headers = {"cache-control": "max-age=60"}
    clock = Clock(monkeypatch)
    cache = identity.cache(ttl_seconds=600, min_refresh_interval=30)
    await cache.get("key-1")

    identity.headers = {}
    clock.now += 61
    await cache.get("key-1")
    assert identity.calls == 2

    clock.now += 300
    await cache.get("key-1")
    assert identity.calls == 2


INTERVAL = 0.05


async def _until(condition, timeout: float = 2.0) -> bool:
    """Poll `condition` until it holds or `timeout` passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.005)
    return condition()


@pytest.mark.anyio
async def test_a_warm_that_missed_keeps_trying_until_identity_answers():
    """A service started just before identity becomes ready without a request (FR-SSO-02)."""
    identity = FakeIdentity("key-1")
    identity.status = 503
    cache = identity.cache(min_refresh_interval=INTERVAL)

    assert await cache.warm() is False
    await asyncio.sleep(INTERVAL * 3)
    assert not cache.has_keys
    identity.status = 200

    assert await _until(lambda: cache.has_keys)
    await cache.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("body", [{"keys": []}, {"keys": "not-a-list"}])
async def test_an_empty_or_unparseable_document_keeps_the_retrying(body):
    identity = FakeIdentity("key-1")
    identity.body = body
    cache = identity.cache(min_refresh_interval=INTERVAL)

    assert await cache.warm() is False
    await asyncio.sleep(INTERVAL * 4)

    assert identity.calls > 1
    assert not cache.has_keys
    identity.body = None
    assert await _until(lambda: cache.has_keys)
    await cache.aclose()


@pytest.mark.anyio
async def test_the_retrying_stops_after_the_first_success():
    identity = FakeIdentity("key-1")
    identity.status = 503
    cache = identity.cache(min_refresh_interval=INTERVAL)
    await cache.warm()
    identity.status = 200
    assert await _until(lambda: cache.has_keys)
    calls = identity.calls

    await asyncio.sleep(INTERVAL * 3)

    assert identity.calls == calls
    assert await _until(lambda: cache._retry_task is None or cache._retry_task.done())
    await cache.aclose()


@pytest.mark.anyio
async def test_a_warm_that_succeeds_starts_no_background_task():
    identity = FakeIdentity("key-1")
    cache = identity.cache(min_refresh_interval=INTERVAL)

    assert await cache.warm() is True
    assert cache._retry_task is None
    await asyncio.sleep(INTERVAL * 3)
    assert identity.calls == 1


@pytest.mark.anyio
async def test_attempts_are_a_full_interval_apart_even_when_a_request_fetched_between():
    identity = FakeIdentity("key-1")
    identity.status = 503
    stamps: list[float] = []
    inner = identity.transport().handler

    def stamped(request: httpx.Request) -> httpx.Response:
        stamps.append(time.monotonic())
        return inner(request)

    client = TracedAsyncClient(
        "http://identity.test/jwks",
        transport=httpx.MockTransport(stamped),
        retry=RetryPolicy(attempts=1),
    )
    cache = JWKSCache(
        jwks_url="http://identity.test/jwks", client=client, min_refresh_interval=INTERVAL
    )
    await cache.warm()
    await asyncio.sleep(INTERVAL * 0.6)
    with pytest.raises(KeyUnavailable):
        await cache.get("key-1")
    await asyncio.sleep(INTERVAL * 4)
    await cache.aclose()

    after_request = stamps[1:]
    assert len(after_request) >= 3
    gaps = [b - a for a, b in zip(after_request, after_request[1:], strict=False)]
    assert all(gap >= INTERVAL * 0.9 for gap in gaps)


@pytest.mark.anyio
async def test_aclose_while_retrying_returns_at_once_and_leaves_no_task():
    identity = FakeIdentity("key-1")
    identity.status = 503
    cache = identity.cache(min_refresh_interval=60)
    await cache.warm()
    task = cache._retry_task
    assert task is not None and not task.done()

    started = time.monotonic()
    await cache.aclose()

    assert time.monotonic() - started < 1
    assert task.done() and task.cancelled()
    assert cache._retry_task is None


@pytest.mark.anyio
async def test_identity_failing_after_a_success_keeps_the_cached_keys():
    identity = FakeIdentity("key-1")
    identity.status = 503
    cache = identity.cache(min_refresh_interval=INTERVAL)
    await cache.warm()
    identity.status = 200
    assert await _until(lambda: cache.has_keys)

    identity.status = 503
    await asyncio.sleep(INTERVAL * 3)

    assert cache.has_keys
    assert await cache.get("key-1") is not None
    await cache.aclose()


@pytest.mark.anyio
async def test_the_readiness_check_makes_no_outbound_call():
    identity = FakeIdentity("key-1")
    identity.status = 503
    cache = identity.cache(min_refresh_interval=60)
    await cache.warm()
    calls = identity.calls

    for _ in range(5):
        assert cache.has_keys is False

    assert identity.calls == calls
    await cache.aclose()
