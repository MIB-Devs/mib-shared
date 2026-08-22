import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mib_shared.http_client import TracedAsyncClient
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
        self.status = 200
        self.body: dict | None = None

    def serve(self, *kids: str) -> None:
        """Rotate: publish a different set of keys from now on."""
        self.keys = {kid: _jwk(kid) for kid in kids}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.status != 200:
                return httpx.Response(self.status)
            return httpx.Response(200, json=self.body or {"keys": list(self.keys.values())})

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
    """Trap 2: otherwise a stream of random kids becomes a stream of outbound calls."""
    identity = FakeIdentity("key-1")
    cache = identity.cache(ttl_seconds=300, min_refresh_interval=60)
    await cache.get("key-1")
    assert identity.calls == 1

    for i in range(20):
        with pytest.raises(KeyUnavailable):
            await cache.get(f"garbage-{i}")

    # Still one. The cooldown runs from the last ATTEMPT, including the
    # successful one above, so twenty unknown kids cost zero outbound calls
    # rather than twenty. The trade is documented on DEFAULT_MIN_REFRESH_INTERVAL:
    # a rotation within the cooldown is not seen immediately, which is why
    # identity must publish a key before signing with it.
    assert identity.calls == 1


@pytest.mark.anyio
async def test_a_failed_refresh_keeps_serving_cached_keys():
    """Trap 3: fail STATIC.

    An identity outage must not take every service's authentication with it —
    that is the entire benefit of verifying locally.
    """
    identity = FakeIdentity("key-1")
    cache = identity.cache(ttl_seconds=-1, min_refresh_interval=0)  # always stale
    await cache.get("key-1")

    identity.status = 503
    for _ in range(3):
        assert await cache.get("key-1") is not None
    assert identity.calls > 1  # it did try


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
    cache = identity.cache(ttl_seconds=-1, min_refresh_interval=0)  # always stale
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
    cache = identity.cache(ttl_seconds=-1, min_refresh_interval=0)  # always stale
    await cache.get("key-1")

    identity.body = {"keys": []}
    assert await cache.get("key-1") is not None


@pytest.mark.anyio
async def test_static_key_needs_no_network_at_all():
    static = StaticKey(public_key="-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----")
    assert await static.get() == static.public_key
    assert await static.get("any-kid") == static.public_key
