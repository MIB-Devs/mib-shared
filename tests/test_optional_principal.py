"""`optional_principal` and cache warm-up (FR-SSO-02, FR-REG-05).

Both exist so four services do not each invent them. The behaviour worth pinning
is the asymmetry: **no credential is anonymous, a bad credential is a 401.**

Note there is no `from __future__ import annotations` here, deliberately. It would
turn the endpoint's annotation into a string, and FastAPI resolves those against
module globals — where the per-test dependency built inside a fixture does not
exist. The parameter then silently becomes a query field and every request 422s.
Services are unaffected: they build dependencies at module scope in `app/deps.py`,
which is resolvable either way.
"""
from typing import Annotated

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mib_shared import JWKSCache, Principal, optional_principal
from mib_shared.http_client import TracedAsyncClient

AUDIENCE = "mib-users"
ISSUER = "mib-identity"


class Identity:
    """A stand-in for mib-identity: one key, and it counts who asks for it."""

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.calls = 0
        self.reachable = True

    def token(self, *, audience: str = AUDIENCE, sub: str = "user-1") -> str:
        return jwt.encode(
            {
                "sub": sub,
                "aud": audience,
                "iss": ISSUER,
                "iat": 1,
                "exp": 9_999_999_999,
                "capabilities": ["regulations.search"],
            },
            self._key,
            algorithm="RS256",
            headers={"kid": "k1"},
        )

    def cache(self) -> JWKSCache:
        document = jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        document.update({"kid": "k1", "alg": "RS256", "use": "sig"})

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if not self.reachable:
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": [document]})

        client = TracedAsyncClient(
            "http://identity.test/jwks", transport=httpx.MockTransport(handler)
        )
        return JWKSCache(jwks_url="http://identity.test/jwks", client=client)


@pytest.fixture
def identity() -> Identity:
    return Identity()


@pytest.fixture
def app(identity: Identity) -> FastAPI:
    caller = optional_principal(keys=identity.cache(), audience=AUDIENCE, issuer=ISSUER)
    application = FastAPI()

    @application.get("/regulation")
    async def regulation(who: Annotated[Principal | None, Depends(caller)]) -> dict:
        # The shape a tiered-preview endpoint actually uses.
        if who is None:
            return {"view": "preview", "user": None}
        return {"view": "full", "user": who.user_id}

    return application


def test_no_credential_gets_the_public_view(app):
    with TestClient(app) as client:
        assert client.get("/regulation").json() == {"view": "preview", "user": None}


def test_a_valid_token_gets_the_full_view(app, identity):
    with TestClient(app) as client:
        resp = client.get(
            "/regulation", headers={"Authorization": f"Bearer {identity.token()}"}
        )
    assert resp.json() == {"view": "full", "user": "user-1"}


def test_a_token_that_does_not_verify_is_a_401_not_an_anonymous_view(app):
    """The asymmetry, and the reason this function exists rather than a
    try/except in each service.

    A caller presenting a credential is asserting an identity. Silently serving
    the visitor view when that assertion fails gives the user a page that has
    forgotten them with no explanation, never teaches the front end to refresh,
    and hands someone probing with junk tokens a uniformly friendly response.
    """
    with TestClient(app) as client:
        resp = client.get("/regulation", headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401


def test_a_token_for_another_audience_is_a_401(app, identity):
    """An administrator token must not quietly become an anonymous visitor on a
    public page either — that would hide a misrouted client (FR-SSO-11)."""
    token = identity.token(audience="mib-administrators")
    with TestClient(app) as client:
        resp = client.get("/regulation", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_an_empty_bearer_value_is_treated_as_no_credential(app):
    """`Authorization: Bearer` with nothing after it is a broken client, not an
    assertion of identity."""
    with TestClient(app) as client:
        resp = client.get("/regulation", headers={"Authorization": "Bearer "})
    assert resp.status_code == 200
    assert resp.json()["view"] == "preview"


def test_the_public_view_costs_no_call_to_identity(app, identity):
    """A visitor must not make the anonymous path depend on identity being up —
    otherwise an identity outage takes the public site down with it."""
    with TestClient(app) as client:
        client.get("/regulation")
    assert identity.calls == 0


# --- warm-up and readiness -------------------------------------------------


@pytest.mark.anyio
async def test_a_fresh_cache_holds_no_keys(identity):
    """Which is what makes it a readiness signal: a process that has never
    fetched cannot verify anything."""
    cache = identity.cache()
    assert cache.has_keys is False


@pytest.mark.anyio
async def test_warming_makes_keys_available_before_the_first_request(identity):
    cache = identity.cache()
    assert await cache.warm() is True
    assert cache.has_keys is True
    assert identity.calls == 1


@pytest.mark.anyio
async def test_warming_against_an_unreachable_identity_does_not_raise(identity):
    """It must report the problem, not crash the process.

    A service that cannot reach identity while starting should come up and report
    itself unready, so an operator reads `/ready` instead of watching a container
    restart every few seconds.
    """
    identity.reachable = False
    cache = identity.cache()
    assert await cache.warm() is False
    assert cache.has_keys is False


@pytest.mark.anyio
async def test_an_outage_after_warming_does_not_clear_the_keys(identity):
    """Fail-static. Once a process holds keys, identity going down must not stop
    it verifying — that is the entire benefit of verifying locally, and the
    reason `has_keys` is the readiness signal rather than reachability.
    """
    cache = identity.cache()
    await cache.warm()
    identity.reachable = False
    assert cache.has_keys is True
    assert await cache.get("k1") is not None
