"""Token verification, capability checks and service-to-service credentials.

Three requirements meet here:

- **FR-SSO-02** — a service verifies an access token locally against the
  published key, with no network hop per request.
- **FR-SSO-03** — the token carries capabilities as a claim, but *entitlement is
  per capability, never a plan name*. This module refuses to look at plan names,
  because the moment one endpoint checks `plan == "pro"` the plans table stops
  being configuration.
- **FR-BE-20** — being on the internal Compose network is not authentication. A
  service-to-service call presents a credential or it is refused.

Mechanics only (§8.3): nothing here knows what a regulation, a subscription or a
summary is.
"""
from __future__ import annotations

import hmac
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Protocol

import jwt
import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mib_shared.telemetry import get_logger

logger = get_logger(__name__)

# Asymmetric only. HS256 with a public key is the classic algorithm-confusion
# attack — the verifier treats the *public* key as a shared secret, so anyone
# holding it can mint tokens. `none` needs no explanation. Neither is ever
# accepted, regardless of what the token's header asks for.
ALLOWED_ALGORITHMS = ("RS256", "RS512", "EdDSA")

SERVICE_HEADER = "x-mib-service"
SERVICE_TOKEN_HEADER = "x-mib-service-token"

_bearer = HTTPBearer(auto_error=False)
# auto_error=False so a missing header reaches our handler and gets the shared
# 401 with a challenge, rather than FastAPI's own 403 with a different shape.
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller and what their token says they may do."""

    user_id: str
    capabilities: frozenset[str]
    audience: str | None = None
    token_id: str | None = None
    expires_at: int | None = None
    claims: dict[str, Any] = field(default_factory=dict, repr=False)

    def has(self, capability: str) -> bool:
        return capability in self.capabilities


class KeyProvider(Protocol):
    """Anything that can produce a verification key — JWKSCache or StaticKey."""

    async def get(self, kid: str | None = None) -> Any: ...


def _unauthenticated(detail: str) -> HTTPException:
    # 401 with WWW-Authenticate, so a client can tell "log in again" from
    # "you may not do this" without parsing prose.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def verify_access_token(
    token: str,
    *,
    keys: KeyProvider,
    audience: str,
    issuer: str | None = None,
    leeway_seconds: float = 30.0,
) -> Principal:
    """Verify locally and return the caller. Raises 401 on anything suspect.

    ``audience`` is required, not optional. A separate administrator audience is
    what makes an admin token refused by public endpoints and vice versa
    (FR-SSO-11), and that only holds if every call states which audience it is.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _unauthenticated("Malformed token.") from None

    if header.get("alg") not in ALLOWED_ALGORITHMS:
        # Logged, because a token arriving with an unexpected algorithm is either
        # a misconfigured issuer or someone probing.
        logger.warning("token_algorithm_refused", algorithm=header.get("alg"))
        raise _unauthenticated("Unsupported token algorithm.")

    try:
        key = await keys.get(header.get("kid"))
    except Exception as exc:
        logger.warning("token_key_unavailable", kid=header.get("kid"), error=type(exc).__name__)
        raise _unauthenticated("Token cannot be verified.") from None

    material = getattr(key, "key", key)
    try:
        claims = jwt.decode(
            token,
            material,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=audience,
            issuer=issuer,
            leeway=leeway_seconds,
            options={
                "require": ["exp", "iat", "sub", "aud"],
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": issuer is not None,
            },
        )
    except jwt.ExpiredSignatureError:
        raise _unauthenticated("Token has expired.") from None
    except jwt.InvalidAudienceError:
        # Deliberately the same message as any other rejection: which audience a
        # service expects is not something an unauthenticated caller needs told.
        raise _unauthenticated("Token is not valid here.") from None
    except jwt.PyJWTError:
        raise _unauthenticated("Token is not valid here.") from None

    principal = Principal(
        user_id=str(claims["sub"]),
        capabilities=frozenset(claims.get("capabilities", ())),
        audience=audience,
        token_id=claims.get("jti"),
        expires_at=claims.get("exp"),
        claims=claims,
    )
    # Put the caller on every subsequent log line for this request, next to the
    # trace_id (NFR-16). Never the token, and never the capability list.
    structlog.contextvars.bind_contextvars(user_id=principal.user_id)
    return principal


def bearer_principal(
    *,
    keys: KeyProvider,
    audience: str,
    issuer: str | None = None,
):
    """FastAPI dependency: authenticate the caller, no capability required.

    Use for endpoints that need to know *who* without needing an entitlement —
    a profile read, say.
    """

    async def _dependency(credentials: BearerCredentials) -> Principal:
        if credentials is None or not credentials.credentials:
            raise _unauthenticated("Authentication required.")
        return await verify_access_token(
            credentials.credentials, keys=keys, audience=audience, issuer=issuer
        )

    return _dependency


def require_capability(
    capability: str,
    *,
    keys: KeyProvider,
    audience: str,
    issuer: str | None = None,
):
    """FastAPI dependency: authenticate, then require one capability (FR-SSO-03).

    By capability, never by plan name. Plans are versioned rows whose capability
    set changes without a release (FR-PAY-09); an endpoint that checks a plan
    name silently makes that untrue.
    """

    async def _dependency(credentials: BearerCredentials) -> Principal:
        if credentials is None or not credentials.credentials:
            raise _unauthenticated("Authentication required.")
        principal = await verify_access_token(
            credentials.credentials, keys=keys, audience=audience, issuer=issuer
        )
        if not principal.has(capability):
            # 403, not 401: the caller is authenticated and simply may not do
            # this. Re-authenticating would not help, and telling them to log in
            # again sends them round a loop.
            logger.info(
                "capability_denied",
                capability=capability,
                user_id=principal.user_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {capability!r} capability.",
            )
        return principal

    return _dependency


# --- service to service (FR-BE-20) -----------------------------------------


@dataclass(frozen=True)
class ServiceCaller:
    """An authenticated sibling service."""

    name: str


def _service_credentials(env: dict[str, str] | None = None) -> dict[str, str]:
    """Caller name → shared token, from ``MIB_SERVICE_TOKEN_<NAME>`` variables.

    Read from the environment rather than a config object so this stays free of
    domain settings, and so rotating one caller's token does not require touching
    every service's configuration.
    """
    source = env if env is not None else os.environ
    prefix = "MIB_SERVICE_TOKEN_"
    creds: dict[str, str] = {}
    for name, value in source.items():
        if name.startswith(prefix) and value:
            creds[name[len(prefix) :].lower().replace("_", "-")] = value
    return creds


def require_service(
    allowed: Iterable[str],
    *,
    env: dict[str, str] | None = None,
):
    """FastAPI dependency: only these named services may call this endpoint.

    Being on the internal network proves nothing — a compromised container is on
    it too, and so is anything that gets a foothold anywhere in the stack
    (FR-BE-20). This is the check that makes ``mib-retrieval`` callable *only* by
    ``mib-ai`` (FR-BE-22) rather than by whatever happens to resolve its DNS name.
    """
    permitted = frozenset(allowed)

    async def _dependency(request: Request) -> ServiceCaller:
        name = (request.headers.get(SERVICE_HEADER) or "").strip().lower()
        presented = request.headers.get(SERVICE_TOKEN_HEADER) or ""
        credentials = _service_credentials(env)
        expected = credentials.get(name)

        # One rejection for every failure mode: unknown caller, no credential
        # configured, wrong token, or a caller that is authentic but not allowed
        # here. Distinguishing them in the response would let a caller enumerate
        # which services exist and which are configured.
        ok = bool(name) and bool(presented) and expected is not None
        if ok:
            ok = hmac.compare_digest(presented, expected)  # constant time
        if ok:
            ok = name in permitted

        if not ok:
            logger.warning(
                "service_call_refused",
                claimed_service=name or None,
                credential_presented=bool(presented),
                allowed=sorted(permitted),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This endpoint is not callable by this service.",
            )

        structlog.contextvars.bind_contextvars(calling_service=name)
        return ServiceCaller(name=name)

    return _dependency


def service_call_headers(service_name: str, token: str | None = None) -> dict[str, str]:
    """Headers a service attaches when calling a sibling.

    Pass to ``TracedAsyncClient``; the traceparent is added there, so one request
    stays one trace across the hop (FR-BE-25).
    """
    value = token if token is not None else os.environ.get("MIB_SERVICE_TOKEN", "")
    return {SERVICE_HEADER: service_name, SERVICE_TOKEN_HEADER: value}


def has_capability(principal: Principal, capability: str) -> bool:
    """Kept for callers written against the scaffold's function form."""
    return principal.has(capability)
