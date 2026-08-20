from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx

from mib_shared.telemetry import get_logger
from mib_shared.tracing import TRACEPARENT_HEADER, current_context

logger = get_logger(__name__)

# Every outbound call has a timeout, a bounded retry, and a defined fallback
# (FR-BE-21). None of the three is optional: on one host, a service waiting
# forever on a sibling is how a single slow dependency becomes an outage.
DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=2.0)

# Retried only where a repeat is harmless. A POST that created a subscription is
# not safe to repeat just because the response was lost, so POST and PATCH are
# retried ONLY when the caller declares the operation idempotent (for instance
# because it carries an idempotency key).
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# 4xx other than 429 means the request itself was wrong, so repeating it only
# spends the budget.
RETRY_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries. ``attempts`` counts the first try, so 3 means 2 retries."""

    attempts: int = 3
    backoff_seconds: float = 0.2
    max_backoff_seconds: float = 2.0
    retry_statuses: frozenset[int] = field(default_factory=lambda: RETRY_STATUSES)

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with jitter.

        Without jitter, retries from several services synchronise and arrive as
        one burst on a dependency that is already struggling.
        """
        raw = self.backoff_seconds * (2 ** (attempt - 1))
        return min(raw, self.max_backoff_seconds) * (0.5 + random.random() / 2)


class RetryBudgetExceeded(httpx.HTTPError):
    """Every attempt failed and the caller defined no fallback."""

    def __init__(self, method: str, url: str, attempts: int, last: BaseException | None) -> None:
        super().__init__(f"{method.upper()} {url} failed after {attempts} attempt(s)")
        self.attempts = attempts
        self.last = last


def _is_retryable_method(method: str, idempotent: bool | None) -> bool:
    if idempotent is not None:
        return idempotent
    return method.upper() in IDEMPOTENT_METHODS


def _traced_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Propagate the trace to the next hop (FR-BE-25).

    The outbound call is a child span of the current request, so the receiving
    service continues the same trace instead of starting its own.
    """
    out = dict(headers or {})
    ctx = current_context()
    if ctx is not None and TRACEPARENT_HEADER not in {k.lower() for k in out}:
        out[TRACEPARENT_HEADER] = ctx.child().traceparent()
    return out


class _TracedBase:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: httpx.Timeout | float | None = None,
        retry: RetryPolicy | None = None,
        service: str | None = None,
    ) -> None:
        # The timeout is never left to a library default, which for some
        # transports means no timeout at all.
        self._timeout = httpx.Timeout(timeout) if timeout is not None else DEFAULT_TIMEOUT
        self._retry = retry or RetryPolicy()
        self._service = service or base_url
        self._base_url = base_url

    def _should_retry_response(self, response: httpx.Response) -> bool:
        return response.status_code in self._retry.retry_statuses

    def _attempts_for(self, method: str, idempotent: bool | None) -> int:
        return self._retry.attempts if _is_retryable_method(method, idempotent) else 1

    def _log_retry(self, method: str, url: str, attempt: int, **kw: Any) -> None:
        logger.warning(
            "outbound_call_retry",
            service=self._service,
            method=method.upper(),
            url=url,
            attempt=attempt,
            attempts_allowed=self._retry.attempts,
            **kw,
        )

    def _log_exhausted(
        self, method: str, url: str, attempts: int, last: BaseException | httpx.Response | None
    ) -> None:
        logger.error(
            "outbound_call_failed",
            service=self._service,
            method=method.upper(),
            url=url,
            attempts=attempts,
            error=type(last).__name__ if isinstance(last, BaseException) else None,
            status=last.status_code if isinstance(last, httpx.Response) else None,
        )

    def _give_up(
        self,
        method: str,
        url: str,
        attempts: int,
        last: BaseException | httpx.Response | None,
        fallback: Callable[[BaseException | httpx.Response], Any] | None,
    ) -> Any:
        self._log_exhausted(method, url, attempts, last)
        exhausted = RetryBudgetExceeded(
            method, url, attempts, last if isinstance(last, BaseException) else None
        )
        if fallback is not None:
            # A defined fallback is what turns a dependency failure into degraded
            # service rather than a 500 (FR-BE-21) — lexical search when the
            # embedding service is down, for instance.
            return fallback(last if last is not None else exhausted)
        raise exhausted


class TracedAsyncClient(_TracedBase):
    """The client every service-to-service call goes through (FR-BE-21, FR-BE-25).

    Async because the callers are async: a synchronous HTTP call inside an async
    handler blocks the event loop, which is the defect that made ``/ready`` hang.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: httpx.Timeout | float | None = None,
        retry: RetryPolicy | None = None,
        service: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **client_kwargs: Any,
    ) -> None:
        super().__init__(base_url, timeout=timeout, retry=retry, service=service)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=self._timeout,
            transport=transport,
            **client_kwargs,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        fallback: Callable[[BaseException | httpx.Response], Any] | None = None,
        idempotent: bool | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        attempts = self._attempts_for(method, idempotent)
        last: BaseException | httpx.Response | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method, url, headers=_traced_headers(headers), **kwargs
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt == attempts:
                    break
                self._log_retry(method, url, attempt, error=type(exc).__name__)
                await anyio.sleep(self._retry.delay_for(attempt))
                continue

            if attempt < attempts and self._should_retry_response(response):
                last = response
                self._log_retry(method, url, attempt, status=response.status_code)
                await anyio.sleep(self._retry.delay_for(attempt))
                continue
            return response

        return self._give_up(method, url, attempts, last, fallback)

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TracedAsyncClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


class TracedClient(_TracedBase):
    """Synchronous variant for workers and one-shot jobs (mib-ingestion, mib-rag).

    Never use this from an async handler — reach for ``TracedAsyncClient`` there.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: httpx.Timeout | float | None = None,
        retry: RetryPolicy | None = None,
        service: str | None = None,
        transport: httpx.BaseTransport | None = None,
        **client_kwargs: Any,
    ) -> None:
        super().__init__(base_url, timeout=timeout, retry=retry, service=service)
        self._client = httpx.Client(
            base_url=base_url,
            timeout=self._timeout,
            transport=transport,
            **client_kwargs,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        fallback: Callable[[BaseException | httpx.Response], Any] | None = None,
        idempotent: bool | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        attempts = self._attempts_for(method, idempotent)
        last: BaseException | httpx.Response | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(
                    method, url, headers=_traced_headers(headers), **kwargs
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt == attempts:
                    break
                self._log_retry(method, url, attempt, error=type(exc).__name__)
                time.sleep(self._retry.delay_for(attempt))
                continue

            if attempt < attempts and self._should_retry_response(response):
                last = response
                self._log_retry(method, url, attempt, status=response.status_code)
                time.sleep(self._retry.delay_for(attempt))
                continue
            return response

        return self._give_up(method, url, attempts, last, fallback)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TracedClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
