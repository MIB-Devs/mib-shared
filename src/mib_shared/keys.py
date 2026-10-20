"""Verification keys, fetched from mib-identity and cached (FR-SSO-02).

The point of the whole design is that a service verifies a token **without
calling mib-identity per request**. That only holds if the public key is cached
locally, which turns key management into a caching problem with three specific
traps:

1. **Rotation.** Identity will eventually sign with a new key. A cache that only
   refreshes on a timer rejects every token for up to its TTL after a rotation,
   so an unknown `kid` has to trigger a refresh.
2. **Unknown-kid stampede.** If any unrecognised `kid` triggers a fetch, an
   attacker sending random ones turns every request into an outbound call. So
   forced refreshes are rate limited.
3. **Identity being down.** Verification must keep working — that is the entire
   benefit of local verification. So a failed refresh serves the cached keys
   (fail-static) and only rejects when there is nothing cached at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import anyio
import jwt
from jwt import PyJWK

from mib_shared.http_client import TracedAsyncClient
from mib_shared.telemetry import get_logger

logger = get_logger(__name__)

DEFAULT_TTL_SECONDS = 600.0
# The floor between fetch ATTEMPTS. It is what stops random kids being used as an
# amplifier, and it has a consequence worth stating: because the cooldown starts
# at the last attempt - including a successful one - a key rotated immediately
# after a fetch is not picked up for up to this long, and tokens signed with it
# are refused meanwhile.
#
# The operational rule that makes that harmless: **publish the new key, then
# start signing with it.** Identity should add a key to the JWKS and wait longer
# than this interval before issuing tokens against it. Then no cooldown length
# can cause a rejection, and this value only bounds how much an attacker can
# amplify.
DEFAULT_MIN_REFRESH_INTERVAL = 30.0


class KeyUnavailable(Exception):
    """No usable key for this token, and none could be fetched."""


@dataclass
class JWKSCache:
    """Caches mib-identity's published JWKS.

    ``jwks_url`` is the published key endpoint. Nothing here knows what a user or
    a capability is — it holds keys, which is why it belongs in a mechanics-only
    library (§8.3).

    The fetch always goes to ``jwks_url`` (a trailing slash on it is dropped), not
    to an injected ``client``'s own ``base_url``.
    """

    jwks_url: str
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    min_refresh_interval: float = DEFAULT_MIN_REFRESH_INTERVAL
    client: TracedAsyncClient | None = None

    _keys: dict[str, PyJWK] = field(default_factory=dict, init=False)
    _fetched_at: float = field(default=0.0, init=False)
    _last_attempt: float = field(default=0.0, init=False)
    _lock: anyio.Lock = field(default_factory=anyio.Lock, init=False)

    def __post_init__(self) -> None:
        """Strip the trailing slash (FastAPI 307s ``/jwks/`` away and the fetch does not follow) and
        build the traced client: timeout, bounded retry and traceparent (FR-BE-21, FR-BE-25).
        """
        self.jwks_url = self.jwks_url.rstrip("/")
        self._client = self.client or TracedAsyncClient(self.jwks_url, service="mib-identity-jwks")

    @property
    def _expired(self) -> bool:
        return (time.monotonic() - self._fetched_at) > self.ttl_seconds

    @property
    def has_keys(self) -> bool:
        """Whether this process has ever successfully fetched.

        The distinction readiness needs. Fail-static means an identity outage does
        not stop a service verifying tokens — but that only holds for a process
        holding keys. One that has *never* fetched cannot verify anything, and
        reporting itself ready would mean accepting traffic it must refuse.

        So: false here is a readiness failure; identity going down later is not.
        """
        return bool(self._keys)

    async def warm(self) -> bool:
        """Fetch at startup. Returns whether keys are available afterwards.

        Best effort, and deliberately not fatal: a service that cannot reach
        identity while starting should come up and report itself unready rather
        than crash-looping, so an operator sees `/ready` explaining the problem
        instead of a container restarting every few seconds.

        Also removes a first-request cost — otherwise the first authenticated
        request of a deploy pays for the fetch.
        """
        try:
            await self._refresh()
        except Exception as exc:  # pragma: no cover - _refresh already fails soft
            logger.warning("jwks_warmup_failed", url=self.jwks_url, error=type(exc).__name__)
        return self.has_keys

    async def get(self, kid: str | None) -> PyJWK:
        """The key for this ``kid``, fetching only when it might help.

        An unrecognised kid fetches even while the cache is fresh, or a rotation stays invisible
        until the TTL lapses.
        """
        key = self._keys.get(kid) if kid else self._single_key()
        if key is not None and not self._expired:
            return key

        await self._refresh(unknown_kid=key is None)

        key = self._keys.get(kid) if kid else self._single_key()
        if key is None:
            raise KeyUnavailable(
                f"no verification key for kid={kid!r}; "
                f"{len(self._keys)} key(s) cached from {self.jwks_url}"
            )
        return key

    def _single_key(self) -> PyJWK | None:
        """Tokens without a ``kid`` are only resolvable when one key exists.

        Guessing among several would mean trying each until one verifies, which
        turns a missing header into a signature oracle.
        """
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    async def _refresh(self, *, unknown_kid: bool = False) -> None:
        """Fetch and replace the cached keys, under the lock.

        A fresh cache stops the refresh unless the kid was unknown, so rotation works. Rate limited
        to ``min_refresh_interval``, so unknown kids cannot become a stream of outbound calls.
        The URL is absolute because a bare ``GET ""`` requests ``/jwks/`` and FastAPI's 307 is not
        followed
        (#36). On a failed, unparseable or empty response it keeps verifying with the cached keys:
        an identity outage must not take every service's authentication with it. A fetched set
        replaces the cache rather than merging, so a removed key stops being accepted.
        """
        async with self._lock:
            if self._keys and not self._expired and not unknown_kid:
                return
            since_attempt = time.monotonic() - self._last_attempt
            if self._keys and since_attempt < self.min_refresh_interval:
                return
            self._last_attempt = time.monotonic()

            assert self._client is not None
            response = await self._client.get(
                self.jwks_url,
                fallback=lambda cause: cause,
            )
            if not hasattr(response, "status_code") or response.status_code != 200:
                logger.warning(
                    "jwks_refresh_failed",
                    url=self.jwks_url,
                    cached_keys=len(self._keys),
                    reason=type(response).__name__
                    if not hasattr(response, "status_code")
                    else response.status_code,
                    location=getattr(response, "headers", {}).get("location"),
                )
                return

            try:
                keys = self._parse(response.json())
            except Exception:
                logger.warning("jwks_unparseable", url=self.jwks_url, cached_keys=len(self._keys))
                return

            if not keys:
                logger.warning("jwks_empty", url=self.jwks_url, cached_keys=len(self._keys))
                return

            self._keys = keys
            self._fetched_at = time.monotonic()
            logger.info("jwks_refreshed", url=self.jwks_url, keys=len(keys))

    @staticmethod
    def _parse(document: Any) -> dict[str, PyJWK]:
        """Keys by ``kid``; one malformed entry is logged and skipped, not fatal to the document."""
        keys: dict[str, PyJWK] = {}
        for entry in (document or {}).get("keys", []):
            kid = entry.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = PyJWK.from_dict(entry)
            except (jwt.PyJWKError, jwt.InvalidKeyError, KeyError, ValueError):
                logger.warning("jwks_key_rejected", kid=kid)
        return keys

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


@dataclass
class StaticKey:
    """A single PEM public key, for a service configured without a JWKS endpoint.

    Same interface as JWKSCache so callers do not branch on which one they hold.
    """

    public_key: str

    async def get(self, kid: str | None = None) -> str:  # noqa: ARG002 - parity
        return self.public_key

    async def aclose(self) -> None:
        return None
