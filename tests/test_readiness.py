import threading
import time

import anyio
import httpx
import pytest
from fastapi import FastAPI

from mib_shared.readiness import (
    ReadinessCheck,
    build_ops_router,
    evaluate_readiness,
    with_connect_timeout,
)


def _app(*checks: ReadinessCheck, overall_timeout: float = 1.0) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_ops_router(
            service_name="mib-test",
            version="0.1.0",
            checks=checks,
            overall_timeout=overall_timeout,
        )
    )
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.anyio
async def test_ready_ok_when_probe_passes():
    resp = await _get(_app(ReadinessCheck("database", lambda: True)), "/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {"database": "ok"}}


@pytest.mark.anyio
async def test_absent_dependency_is_not_a_failure():
    # A probe returning None means "this service has no such dependency".
    resp = await _get(_app(ReadinessCheck("redis", lambda: None)), "/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {}}


@pytest.mark.anyio
async def test_failing_probe_gives_503():
    resp = await _get(_app(ReadinessCheck("database", lambda: False)), "/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "unavailable"


@pytest.mark.anyio
async def test_raising_probe_is_unavailable_not_500():
    def boom() -> bool:
        raise ConnectionRefusedError("no route to host")

    resp = await _get(_app(ReadinessCheck("database", boom)), "/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "unavailable"


@pytest.fixture
def wedged_probe():
    """A probe that blocks the way an unreachable database driver blocks.

    It waits on an event instead of sleeping so the test can release the worker
    thread on the way out — an abandoned thread still sleeping out a 130-second
    driver timeout would hold the whole test session open behind it.
    """
    release = threading.Event()

    def probe() -> bool:
        release.wait(130)
        return True

    try:
        yield probe
    finally:
        release.set()


@pytest.mark.anyio
async def test_unreachable_dependency_fails_fast_under_five_seconds(wedged_probe):
    app = _app(ReadinessCheck("database", wedged_probe, timeout=0.3), overall_timeout=1.0)
    started = time.monotonic()
    resp = await _get(app, "/ready")
    elapsed = time.monotonic() - started

    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "timeout"
    assert elapsed < 5.0


@pytest.mark.anyio
async def test_health_keeps_answering_while_ready_is_blocked(wedged_probe):
    app = _app(ReadinessCheck("database", wedged_probe, timeout=2.0), overall_timeout=3.0)
    health: dict = {}

    async def _hit_ready() -> None:
        await _get(app, "/ready")

    async with anyio.create_task_group() as tg:
        tg.start_soon(_hit_ready)
        await anyio.sleep(0.2)  # let /ready get as far as the blocking probe
        started = time.monotonic()
        resp = await _get(app, "/health")
        health["elapsed"] = time.monotonic() - started
        health["status"] = resp.status_code

    # The blocking probe is on a worker thread, so liveness is not stalled by it.
    assert health["status"] == 200
    assert health["elapsed"] < 1.0


@pytest.mark.anyio
async def test_probes_run_concurrently_not_in_sequence():
    def slow() -> bool:
        time.sleep(0.4)
        return True

    checks = [ReadinessCheck(f"dep{i}", slow, timeout=2.0) for i in range(4)]
    started = time.monotonic()
    report = await evaluate_readiness(checks, overall_timeout=3.0)
    elapsed = time.monotonic() - started

    assert report.ready
    assert elapsed < 1.2  # sequential would be ~1.6s


@pytest.mark.anyio
async def test_async_probe_is_supported():
    async def probe() -> bool:
        await anyio.sleep(0)
        return True

    report = await evaluate_readiness([ReadinessCheck("cache", probe)])
    assert report.ready


def test_connect_timeout_is_added_once():
    url = "postgresql+psycopg://svc:pw@db:5432/mib"
    stamped = with_connect_timeout(url, 2)
    assert "connect_timeout=2" in stamped
    # Never overrides an explicit setting already in the URL.
    assert with_connect_timeout(stamped, 9) == stamped
