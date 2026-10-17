"""The anonymous visitor's daily allowance (mib-ai#2, mib-regulations#34, mib-web#17).

A visitor who is not signed in gets a few full answers or searches a day. Three
services count it, so the mechanics live here and the numbers do not: the limit
is each caller's configuration (FR-RS-09).

**The guest token.** `mib-web` mints a random id, signs it and keeps it in a
cookie, then forwards it on the calls it makes for that visitor
(`GUEST_TOKEN_HEADER`). A service verifies the signature with the shared secret
and counts per id. The id proves nothing about who someone is: clearing the
cookie gives a fresh id and 2 more uses, a residual the client accepted
(`mib-web#17`, as with the watermark). A browser can also reach a service through
the gateway and send any token it was given, so a service must still keep its own
per-address backstop (`visitor_ip`).

**The count.** One Redis set per subject and Jakarta day, holding the id of each
use. A use that is already in the set is free, so reading the same document or
paging the same query twice costs nothing, and a failed answer is given back with
`release`. A refused use is taken out again, so the count never drifts above the
limit. Two uses racing at the edge can both be refused (each sees the other's
member); that fails toward the cost-safe side and the visitor asks again. When the
store cannot be reached, `AllowanceUnavailable` is raised and the caller closes
the anonymous path: the cost risk is worse than a lost trial.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

from starlette.requests import HTTPConnection

GUEST_TOKEN_HEADER = "X-Guest-Token"
GUEST_COOKIE = "mib_guest"

# Western Indonesia Time has no daylight saving, so a fixed offset is exact and needs no tzdata.
WIB = timezone(timedelta(hours=7))

_ID = re.compile(r"[0-9a-f]{32}")
# The key outlives the day by an hour, so a clock a little behind cannot leave a stale count.
_TTL_GRACE_SECONDS = 3600


def mint_guest_token(secret: str) -> str:
    """A new random guest id with its signature: `<32 hex>.<signature>`."""
    guest_id = secrets.token_hex(16)
    return f"{guest_id}.{_sign(guest_id, secret)}"


def verify_guest_token(token: str | None, secret: str | None) -> str | None:
    """The guest id the token carries, or None when it is absent, malformed or forged.

    With no secret configured nothing verifies: the anonymous path is closed rather
    than open to any id.
    """
    if not token or not secret:
        return None
    guest_id, _, signature = token.strip().partition(".")
    if not _ID.fullmatch(guest_id):
        return None
    if not hmac.compare_digest(signature, _sign(guest_id, secret)):
        return None
    return guest_id


def guest_id(connection: HTTPConnection, secret: str | None) -> str | None:
    """The verified guest id on a request, or None (no token, a bad one, or no secret)."""
    return verify_guest_token(connection.headers.get(GUEST_TOKEN_HEADER), secret)


def _sign(guest_id: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), guest_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def next_reset(now: datetime | None = None) -> datetime:
    """The next 00:00 WIB, as an aware UTC instant (`17:00:00Z` the day before)."""
    local = (now or datetime.now(UTC)).astimezone(WIB)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


class AllowanceUnavailable(Exception):  # noqa: N818 - named for what the caller must do, not an "Error"
    """The counter could not be read or written. The caller closes the anonymous path."""


class SetStore(Protocol):
    """The four Redis set commands the count uses (`redis.asyncio.Redis` has them)."""

    async def sadd(self, name: str, *values: str) -> int: ...
    async def scard(self, name: str) -> int: ...
    async def srem(self, name: str, *values: str) -> int: ...
    async def expire(self, name: str, time: int) -> bool: ...


@dataclass(frozen=True)
class Allowance:
    allowed: bool
    used: int
    limit: int
    resets_at: datetime

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def _key(prefix: str, subject: str, now: datetime) -> str:
    return f"{prefix}:{subject}:{now.astimezone(WIB):%Y%m%d}"


def _ttl(now: datetime) -> int:
    return int((next_reset(now) - now).total_seconds()) + _TTL_GRACE_SECONDS


async def consume(
    store: SetStore,
    *,
    subject: str,
    use_id: str,
    limit: int,
    prefix: str = "allowance",
    now: datetime | None = None,
) -> Allowance:
    """Take one use for `subject` today, identified by `use_id`.

    A `use_id` already counted today is allowed and costs nothing again: pass a
    message id to count every answer, or a document version or a query to count each
    only once. `limit` comes from the caller's configuration.
    """
    now = now or datetime.now(UTC)
    key = _key(prefix, subject, now)
    try:
        added = await store.sadd(key, use_id)
        used = await store.scard(key)
        if not added:
            return Allowance(True, used, limit, next_reset(now))
        await store.expire(key, _ttl(now))
        if used > limit:
            await store.srem(key, use_id)
            return Allowance(False, used - 1, limit, next_reset(now))
    except Exception as exc:
        raise AllowanceUnavailable(str(exc)) from exc
    return Allowance(True, used, limit, next_reset(now))


async def release(
    store: SetStore,
    *,
    subject: str,
    use_id: str,
    prefix: str = "allowance",
    now: datetime | None = None,
) -> None:
    """Give a use back (a failed or unfinished answer, FR-RS-12)."""
    now = now or datetime.now(UTC)
    try:
        await store.srem(_key(prefix, subject, now), use_id)
    except Exception as exc:
        raise AllowanceUnavailable(str(exc)) from exc


async def status(
    store: SetStore,
    *,
    subject: str,
    limit: int,
    prefix: str = "allowance",
    now: datetime | None = None,
) -> Allowance:
    """What is left today, without taking a use."""
    now = now or datetime.now(UTC)
    try:
        used = await store.scard(_key(prefix, subject, now))
    except Exception as exc:
        raise AllowanceUnavailable(str(exc)) from exc
    return Allowance(used < limit, used, limit, next_reset(now))


__all__ = [
    "GUEST_COOKIE",
    "GUEST_TOKEN_HEADER",
    "WIB",
    "Allowance",
    "AllowanceUnavailable",
    "SetStore",
    "consume",
    "guest_id",
    "mint_guest_token",
    "next_reset",
    "release",
    "status",
    "verify_guest_token",
]
