"""The guest token and the daily allowance (mib-ai#2, mib-regulations#34)."""
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mib_shared.guest import (
    GUEST_TOKEN_HEADER,
    AllowanceUnavailable,
    consume,
    guest_id,
    mint_guest_token,
    next_reset,
    release,
    status,
    verify_guest_token,
)

SECRET = "a-shared-secret"
# 2026-10-17 12:00 UTC is 19:00 WIB the same day.
NOON = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


class FakeStore:
    """The four set commands, in memory."""

    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}
        self.down = False

    def _up(self) -> None:
        if self.down:
            raise ConnectionError("redis is down")

    async def sadd(self, name: str, *values: str) -> int:
        self._up()
        members = self.sets.setdefault(name, set())
        before = len(members)
        members.update(values)
        return len(members) - before

    async def scard(self, name: str) -> int:
        self._up()
        return len(self.sets.get(name, ()))

    async def srem(self, name: str, *values: str) -> int:
        self._up()
        members = self.sets.get(name, set())
        before = len(members)
        members.difference_update(values)
        if not members:
            self.sets.pop(name, None)
        return before - len(members)

    async def expire(self, name: str, time: int) -> bool:
        self._up()
        self.ttls[name] = time
        return name in self.sets


# --- the token ---


def test_a_minted_token_verifies_to_its_id():
    token = mint_guest_token(SECRET)
    found = verify_guest_token(token, SECRET)
    assert found is not None and len(found) == 32
    assert token.startswith(found + ".")


def test_two_minted_tokens_differ():
    assert mint_guest_token(SECRET) != mint_guest_token(SECRET)


@pytest.mark.parametrize(
    "token",
    [None, "", "no-dot", ".", "abc.def", "z" * 32 + ".sig", "0" * 32 + "."],
)
def test_a_malformed_token_is_no_guest(token):
    assert verify_guest_token(token, SECRET) is None


def test_a_token_signed_with_another_secret_is_forged():
    assert verify_guest_token(mint_guest_token("other"), SECRET) is None


def test_a_changed_id_with_the_old_signature_is_forged():
    token = mint_guest_token(SECRET)
    _, _, signature = token.partition(".")
    assert verify_guest_token("1" * 32 + "." + signature, SECRET) is None


def test_no_secret_means_no_guest_path():
    assert verify_guest_token(mint_guest_token(SECRET), None) is None
    assert verify_guest_token(mint_guest_token(SECRET), "") is None


def test_guest_id_reads_the_forwarded_header():
    app = FastAPI()

    @app.get("/who")
    def who(request: Request) -> dict:
        return {"id": guest_id(request, SECRET)}

    client = TestClient(app)
    token = mint_guest_token(SECRET)
    sent = client.get("/who", headers={GUEST_TOKEN_HEADER: token})
    assert sent.json()["id"] == token.split(".")[0]
    assert client.get("/who").json()["id"] is None
    assert client.get("/who", headers={GUEST_TOKEN_HEADER: "junk"}).json()["id"] is None


# --- the day ---


def test_the_day_resets_at_midnight_wib():
    assert next_reset(NOON) == datetime(2026, 10, 17, 17, 0, tzinfo=UTC)
    # 16:59 UTC is 23:59 WIB: a minute left. 17:00 UTC is already the next day.
    at = lambda day, hour, minute: datetime(2026, 10, day, hour, minute, tzinfo=UTC)  # noqa: E731
    assert next_reset(at(17, 16, 59)) == at(17, 17, 0)
    assert next_reset(at(17, 17, 0)) == at(18, 17, 0)


# --- the count ---


async def take(store, use_id, *, subject="g1", limit=2, now=NOON, **extra):
    return await consume(store, subject=subject, use_id=use_id, limit=limit, now=now, **extra)


@pytest.mark.anyio
async def test_the_first_uses_are_allowed_and_the_next_is_not():
    store = FakeStore()
    results = [await take(store, f"m{n}") for n in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert [r.remaining for r in results] == [1, 0, 0]
    assert results[2].used == 2
    assert results[2].resets_at == datetime(2026, 10, 17, 17, 0, tzinfo=UTC)


@pytest.mark.anyio
async def test_a_refused_use_leaves_the_count_at_the_limit():
    store = FakeStore()
    for n in range(5):
        await take(store, f"m{n}")
    assert (await status(store, subject="g1", limit=2, now=NOON)).used == 2


@pytest.mark.anyio
async def test_the_same_use_twice_costs_once():
    store = FakeStore()
    first = await take(store, "query-a")
    again = await take(store, "query-a")
    assert first.used == again.used == 1 and again.allowed


@pytest.mark.anyio
async def test_a_counted_use_is_still_free_after_the_limit_is_reached():
    store = FakeStore()
    await take(store, "a")
    await take(store, "b")
    assert not (await take(store, "c")).allowed
    # Paging query "b" again is not a new search.
    assert (await take(store, "b")).allowed


@pytest.mark.anyio
async def test_a_released_use_is_given_back():
    store = FakeStore()
    await take(store, "a", limit=1)
    await release(store, subject="g1", use_id="a", now=NOON)
    assert (await take(store, "b", limit=1)).allowed


@pytest.mark.anyio
async def test_subjects_and_days_are_counted_apart():
    store = FakeStore()
    await take(store, "a", limit=1)
    assert (await take(store, "a", subject="g2", limit=1)).allowed
    # 00:00 WIB the next day is 17:00 UTC.
    tomorrow = datetime(2026, 10, 17, 17, 0, tzinfo=UTC)
    assert (await take(store, "b", limit=1, now=tomorrow)).allowed


@pytest.mark.anyio
async def test_the_prefix_keeps_features_apart():
    store = FakeStore()
    await take(store, "a", limit=1, prefix="ai")
    assert (await take(store, "a", limit=1, prefix="search")).allowed


@pytest.mark.anyio
async def test_the_key_expires_after_the_day_and_an_hour():
    store = FakeStore()
    await take(store, "a")
    (ttl,) = store.ttls.values()
    assert ttl == 5 * 3600 + 3600  # 12:00 UTC to 17:00 UTC, plus the hour


@pytest.mark.anyio
async def test_a_store_that_is_down_closes_the_path():
    store = FakeStore()
    store.down = True
    with pytest.raises(AllowanceUnavailable):
        await take(store, "a")
    with pytest.raises(AllowanceUnavailable):
        await release(store, subject="g1", use_id="a", now=NOON)
    with pytest.raises(AllowanceUnavailable):
        await status(store, subject="g1", limit=2, now=NOON)
