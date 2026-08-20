from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anyio
from anyio import to_thread
from fastapi import APIRouter, Response, status

# A dependency probe gets a short leash of its own, and the whole /ready handler
# gets a shorter one than any sensible orchestrator timeout — an unreachable
# database must present as a clean 503, never as a wedged container (OPS-17,
# NFR-44). Both are overridable; neither defaults to "however long the driver
# feels like taking".
DEFAULT_PROBE_TIMEOUT = 2.0
DEFAULT_OVERALL_TIMEOUT = 4.0
DEFAULT_CONNECT_TIMEOUT = 2

# Probe verdicts. ``None`` means "this service has no such dependency", which is
# not a failure — a service with no database is ready without one.
Probe = Callable[[], bool | None] | Callable[[], Awaitable[bool | None]]

OK = "ok"
UNAVAILABLE = "unavailable"
TIMEOUT = "timeout"
SKIPPED = "skipped"


@dataclass(frozen=True)
class ReadinessCheck:
    """One dependency probe.

    ``probe`` is either a plain callable or a coroutine function. A **sync**
    probe is run in a worker thread:
    database and cache drivers block, and blocking inside an async handler stalls
    the event loop, which takes ``/health`` down with the dependency it was
    supposed to be reporting on (FR-BE-18).
    """

    name: str
    probe: Probe
    timeout: float = DEFAULT_PROBE_TIMEOUT


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    checks: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"status": "ready" if self.ready else "not_ready", "checks": self.checks}


def with_connect_timeout(url: str, seconds: int = DEFAULT_CONNECT_TIMEOUT) -> str:
    """Add ``connect_timeout`` to a PostgreSQL URL unless it already carries one.

    The bounded probe below stops ``/ready`` from hanging, but a driver left on
    its default connect timeout still parks a worker thread for minutes after
    the response has gone out. Bounding the connect is what keeps that from
    accumulating one leaked thread per probe.
    """
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "connect_timeout" for key, _ in query):
        return url
    query.append(("connect_timeout", str(seconds)))
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _run_probe(check: ReadinessCheck) -> str:
    try:
        with anyio.fail_after(check.timeout):
            verdict = await check.probe()  # type: ignore[misc]
    except TimeoutError:
        return TIMEOUT
    except Exception:
        # A probe that raises is a dependency that is not answering. Readiness
        # reports that as unavailable rather than as a 500 from the ops endpoint.
        return UNAVAILABLE
    if verdict is None:
        return SKIPPED
    return OK if verdict else UNAVAILABLE


async def _run_check(check: ReadinessCheck) -> tuple[str, str]:
    if inspect.iscoroutinefunction(check.probe):
        return check.name, await _run_probe(check)

    async def _threaded() -> bool | None:
        # abandon_on_cancel: on timeout we stop waiting for the thread rather
        # than waiting out the driver, which is the whole point of the bound.
        return await to_thread.run_sync(check.probe, abandon_on_cancel=True)

    threaded = ReadinessCheck(name=check.name, probe=_threaded, timeout=check.timeout)
    return check.name, await _run_probe(threaded)


async def evaluate_readiness(
    checks: Sequence[ReadinessCheck],
    *,
    overall_timeout: float = DEFAULT_OVERALL_TIMEOUT,
) -> ReadinessReport:
    """Run every probe concurrently under one overall deadline.

    Probes that have not answered when the deadline passes are reported as
    ``timeout`` and the service is not ready — a partial answer now beats a
    complete answer after the orchestrator has given up.
    """
    results: dict[str, str] = {c.name: TIMEOUT for c in checks}
    try:
        with anyio.fail_after(overall_timeout):
            async with anyio.create_task_group() as tg:

                async def _collect(check: ReadinessCheck) -> None:
                    name, verdict = await _run_check(check)
                    results[name] = verdict

                for check in checks:
                    tg.start_soon(_collect, check)
    except TimeoutError:
        pass

    ready = all(verdict in (OK, SKIPPED) for verdict in results.values())
    reported = {name: verdict for name, verdict in results.items() if verdict != SKIPPED}
    return ReadinessReport(ready=ready, checks=reported)


def build_ops_router(
    *,
    service_name: str,
    version: str,
    checks: Sequence[ReadinessCheck] = (),
    overall_timeout: float = DEFAULT_OVERALL_TIMEOUT,
) -> APIRouter:
    """The ``/health`` and ``/ready`` pair, identical in every service (FR-BE-18).

    ``/health`` is liveness and touches nothing, so it keeps answering while a
    dependency is down. ``/ready`` is dependency health, bounded.
    """
    router = APIRouter(tags=["ops"])

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": service_name, "version": version}

    @router.get("/ready")
    async def ready(response: Response) -> dict:
        report = await evaluate_readiness(checks, overall_timeout=overall_timeout)
        if not report.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return report.as_dict()

    return router
