import base64
import hashlib
import hmac
import json
import time
from typing import Annotated

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI

from mib_shared.auth import (
    Principal,
    ServiceCaller,
    bearer_principal,
    require_capability,
    require_service,
    service_call_headers,
    verify_access_token,
)
from mib_shared.keys import StaticKey

AUDIENCE = "mib-regulations"
ADMIN_AUDIENCE = "mib-admin"
ISSUER = "https://identity.mib.test"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization

    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def keys(keypair):
    return StaticKey(public_key=keypair[1])


def make_token(
    private_pem: str,
    *,
    sub="user-1",
    aud=AUDIENCE,
    iss=ISSUER,
    capabilities=("regulations.search",),
    exp_delta=900,
    iat_delta=0,
    algorithm="RS256",
    **extra,
):
    now = int(time.time())
    claims = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now + iat_delta,
        "exp": now + exp_delta,
        "jti": "token-1",
        "capabilities": list(capabilities),
        **extra,
    }
    return jwt.encode(claims, private_pem, algorithm=algorithm)


# --- verification ----------------------------------------------------------

@pytest.mark.anyio
async def test_a_valid_token_authenticates_with_no_call_to_identity(keypair, keys):
    # StaticKey makes the point structurally: there is no client here to call.
    principal = await verify_access_token(
        make_token(keypair[0]), keys=keys, audience=AUDIENCE, issuer=ISSUER
    )
    assert principal.user_id == "user-1"
    assert principal.has("regulations.search")
    assert principal.token_id == "token-1"


@pytest.mark.anyio
async def test_an_expired_token_is_refused(keypair, keys):
    token = make_token(keypair[0], exp_delta=-60)
    with pytest.raises(Exception) as excinfo:
        await verify_access_token(token, keys=keys, audience=AUDIENCE, issuer=ISSUER)
    assert excinfo.value.status_code == 401


@pytest.mark.anyio
async def test_a_token_for_another_audience_is_refused(keypair, keys):
    """FR-SSO-11: an administrator token must not work on a public endpoint."""
    token = make_token(keypair[0], aud=ADMIN_AUDIENCE)
    with pytest.raises(Exception) as excinfo:
        await verify_access_token(token, keys=keys, audience=AUDIENCE, issuer=ISSUER)
    assert excinfo.value.status_code == 401
    # And the message must not disclose which audience this service wanted.
    assert ADMIN_AUDIENCE not in str(excinfo.value.detail)
    assert AUDIENCE not in str(excinfo.value.detail)


@pytest.mark.anyio
async def test_a_token_from_another_issuer_is_refused(keypair, keys):
    token = make_token(keypair[0], iss="https://not-our-identity.test")
    with pytest.raises(Exception) as excinfo:
        await verify_access_token(token, keys=keys, audience=AUDIENCE, issuer=ISSUER)
    assert excinfo.value.status_code == 401


@pytest.mark.anyio
async def test_a_tampered_signature_is_refused(keypair, keys):
    token = make_token(keypair[0])
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{sig[:-4]}AAAA"
    with pytest.raises(Exception) as excinfo:
        await verify_access_token(tampered, keys=keys, audience=AUDIENCE, issuer=ISSUER)
    assert excinfo.value.status_code == 401


@pytest.mark.anyio
async def test_the_algorithm_confusion_attack_is_refused(keypair, keys):
    """HS256 signed with the PUBLIC key: the classic confusion attack.

    A verifier that honours the header's `alg` treats the published public key as
    a shared secret, so anyone who can read it can mint tokens with any claims
    they like. The algorithm allowlist is what stops it.

    Forged by hand rather than with `jwt.encode`, which refuses to use a PEM as an
    HMAC secret - a real attacker is under no such constraint, so a test that
    relied on that guard would be testing the wrong side of the library.
    """
    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(
        json.dumps(
            {
                "sub": "attacker",
                "aud": AUDIENCE,
                "iss": ISSUER,
                "iat": int(time.time()),
                "exp": int(time.time()) + 900,
                "capabilities": ["regulations.search", "admin.everything"],
            }
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = b64(hmac.new(keypair[1].encode(), signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + signature).decode()

    with pytest.raises(Exception) as excinfo:
        await verify_access_token(forged, keys=keys, audience=AUDIENCE, issuer=ISSUER)
    assert excinfo.value.status_code == 401


@pytest.mark.anyio
async def test_an_unsigned_token_is_refused(keys):
    unsigned = jwt.encode(
        {"sub": "attacker", "aud": AUDIENCE, "iss": ISSUER, "exp": int(time.time()) + 900},
        key="",
        algorithm="none",
    )
    with pytest.raises(Exception) as excinfo:
        await verify_access_token(unsigned, keys=keys, audience=AUDIENCE, issuer=ISSUER)
    assert excinfo.value.status_code == 401


@pytest.mark.anyio
async def test_a_token_without_a_subject_is_refused(keypair, keys):
    now = int(time.time())
    token = jwt.encode(
        {"aud": AUDIENCE, "iss": ISSUER, "iat": now, "exp": now + 900},
        keypair[0],
        algorithm="RS256",
    )
    with pytest.raises(Exception) as excinfo:
        await verify_access_token(token, keys=keys, audience=AUDIENCE, issuer=ISSUER)
    assert excinfo.value.status_code == 401


@pytest.mark.anyio
async def test_garbage_is_refused_without_raising_something_unhelpful(keys):
    with pytest.raises(Exception) as excinfo:
        await verify_access_token("not-a-token", keys=keys, audience=AUDIENCE)
    assert excinfo.value.status_code == 401


@pytest.mark.anyio
async def test_small_clock_skew_is_tolerated(keypair, keys):
    # Issued 10s in the "future" by a host whose clock runs fast.
    token = make_token(keypair[0], iat_delta=10)
    principal = await verify_access_token(
        token, keys=keys, audience=AUDIENCE, issuer=ISSUER, leeway_seconds=30
    )
    assert principal.user_id == "user-1"


# --- capability enforcement (FR-SSO-03) ------------------------------------

def _app(keys):
    """The Annotated form, which is what services should copy.

    `principal: Principal = Depends(...)` also works, but puts a call in an
    argument default, which bugbear flags for the usual reason - it is evaluated
    once at import. Harmless for a dependency factory, but not worth arguing with
    a linter over when the Annotated form is clearer anyway.
    """
    app = FastAPI()

    search_dep = require_capability(
        "regulations.search", keys=keys, audience=AUDIENCE, issuer=ISSUER
    )
    Searcher = Annotated[Principal, Depends(search_dep)]
    Summariser = Annotated[
        Principal,
        Depends(require_capability("ai.summary", keys=keys, audience=AUDIENCE, issuer=ISSUER)),
    ]
    AnyUser = Annotated[
        Principal, Depends(bearer_principal(keys=keys, audience=AUDIENCE, issuer=ISSUER))
    ]

    @app.get("/search")
    async def search(principal: Searcher) -> dict:
        return {"user": principal.user_id}

    @app.get("/summary")
    async def summary(principal: Summariser) -> dict:
        return {"user": principal.user_id}

    @app.get("/me")
    async def me(principal: AnyUser) -> dict:
        return {"user": principal.user_id}

    return app


async def _get(app, path, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=5.0
    ) as client:
        return await client.get(path, headers=headers or {})


@pytest.mark.anyio
async def test_a_capability_the_token_carries_is_allowed(keypair, keys):
    token = make_token(keypair[0], capabilities=("regulations.search",))
    resp = await _get(_app(keys), "/search", {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"user": "user-1"}


@pytest.mark.anyio
async def test_a_capability_the_token_lacks_is_403_not_401(keypair, keys):
    # Authenticated but not entitled. 401 would tell the client to log in again,
    # which sends them round a loop that cannot succeed.
    token = make_token(keypair[0], capabilities=("regulations.search",))
    resp = await _get(_app(keys), "/summary", {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_no_token_is_401_with_a_challenge(keys):
    resp = await _get(_app(keys), "/search")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.anyio
async def test_authentication_without_a_capability_requirement(keypair, keys):
    token = make_token(keypair[0], capabilities=())
    resp = await _get(_app(keys), "/me", {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_a_plan_name_claim_grants_nothing(keypair, keys):
    """Entitlement is per capability, never plan name (FR-SSO-03).

    A token claiming the most generous plan in the world still cannot reach an
    endpoint whose capability it does not carry.
    """
    token = make_token(keypair[0], capabilities=(), plan="enterprise", tier="unlimited")
    resp = await _get(_app(keys), "/summary", {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# --- service to service (FR-BE-20, FR-BE-22) -------------------------------

SERVICE_ENV = {"MIB_SERVICE_TOKEN_MIB_AI": "ai-secret", "MIB_SERVICE_TOKEN_MIB_WEB": "web-secret"}


def _internal_app():
    app = FastAPI()
    Caller = Annotated[ServiceCaller, Depends(require_service({"mib-ai"}, env=SERVICE_ENV))]

    @app.get("/internal/v1/retrieve")
    async def retrieve(caller: Caller) -> dict:
        return {"caller": caller.name}

    return app


@pytest.mark.anyio
async def test_the_permitted_service_with_its_credential_is_allowed():
    resp = await _get(
        _internal_app(),
        "/internal/v1/retrieve",
        {"x-mib-service": "mib-ai", "x-mib-service-token": "ai-secret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"caller": "mib-ai"}


@pytest.mark.anyio
async def test_no_credential_is_refused_even_from_the_internal_network():
    """The acceptance criterion: network membership is not authentication."""
    resp = await _get(_internal_app(), "/internal/v1/retrieve", {"x-mib-service": "mib-ai"})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_a_wrong_credential_is_refused():
    resp = await _get(
        _internal_app(),
        "/internal/v1/retrieve",
        {"x-mib-service": "mib-ai", "x-mib-service-token": "not-the-secret"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_an_authentic_but_unauthorised_service_is_refused():
    """FR-BE-22: mib-retrieval is callable by mib-ai only.

    mib-web holds a valid credential of its own and still may not call this.
    """
    resp = await _get(
        _internal_app(),
        "/internal/v1/retrieve",
        {"x-mib-service": "mib-web", "x-mib-service-token": "web-secret"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_borrowing_another_services_name_is_refused():
    resp = await _get(
        _internal_app(),
        "/internal/v1/retrieve",
        {"x-mib-service": "mib-ai", "x-mib-service-token": "web-secret"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_every_refusal_reads_the_same():
    """A caller must not be able to enumerate which services exist."""
    bodies = set()
    for headers in (
        {},
        {"x-mib-service": "mib-ai"},
        {"x-mib-service": "nonexistent", "x-mib-service-token": "x"},
        {"x-mib-service": "mib-web", "x-mib-service-token": "web-secret"},
        {"x-mib-service": "mib-ai", "x-mib-service-token": "wrong"},
    ):
        resp = await _get(_internal_app(), "/internal/v1/retrieve", headers)
        assert resp.status_code == 403
        bodies.add(resp.text)
    assert len(bodies) == 1


def test_outbound_headers_name_the_caller_and_carry_the_token():
    headers = service_call_headers("mib-ai", token="ai-secret")
    assert headers == {"x-mib-service": "mib-ai", "x-mib-service-token": "ai-secret"}
