from __future__ import annotations

from dataclasses import dataclass

import jwt


@dataclass
class Principal:
    """The authenticated caller and the capabilities carried in their token."""

    user_id: str
    capabilities: frozenset[str]
    audience: str | None = None


def verify_token(token: str, public_key: str, *, algorithms: list[str] | None = None,
                 audience: str | None = None) -> Principal:
    """Verify an access token locally using the public key (FR-SSO-02).

    Services verify tokens WITHOUT a network hop to mib-identity. Identity is not
    entitlement — the token says who you are and carries capabilities as a claim,
    but each service still enforces the capability its endpoint requires (FR-SSO-03).
    """
    claims = jwt.decode(
        token,
        public_key,
        algorithms=algorithms or ["RS256", "EdDSA"],
        audience=audience,
    )
    return Principal(
        user_id=str(claims.get("sub")),
        capabilities=frozenset(claims.get("capabilities", [])),
        audience=claims.get("aud"),
    )


def has_capability(principal: Principal, capability: str) -> bool:
    return capability in principal.capabilities
