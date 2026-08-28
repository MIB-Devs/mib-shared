"""The scaffold for calling a sibling service (FR-BE-20 to FR-BE-22, FR-BE-25).

Extracted from three near-identical copies (`mib-ai`'s retrieval and
regulations clients, `mib-regulations`'s retrieval client) plus the lower half
of a fourth (`mib-rag`'s regulations client, which keeps its own typed
permanent/transient error classification and response parsing on top of
this — that part is `mib-rag`'s own reading of `mib-regulations`'s contract,
not mechanics, so it stays there).

Three things every such call needs, all attached here rather than left to each
copy to get right on its own:

- a **timeout** and **bounded retry**, via `TracedAsyncClient` — never `httpx`
  directly
- the caller's **service credential**, from `service_call_headers`
- **`traceparent` propagation**, so one request stays one trace across the hop

`get`/`post` also require an explicit `fallback` per call (FR-BE-21) — the
difference between "the sibling is slow, so this degrades" and "the sibling is
slow, so the whole request fails". A caller that wants the raw `httpx.Response`
instead — to do its own status/404 handling and error classification, the way
`mib-rag`'s regulations client does — uses `request` directly.
"""
from __future__ import annotations

from typing import Any

from mib_shared.auth import service_call_headers
from mib_shared.http_client import TracedAsyncClient


class SiblingUnavailable(RuntimeError):
    """The sibling's base URL isn't configured. Distinct from "the call failed".

    Each caller module should define its own subclass per target it calls, so
    `except my_target.TargetUnavailable` catches only that target's
    misconfiguration rather than every sibling's.
    """


class SiblingClient:
    """One sibling service, reached through one process-wide `TracedAsyncClient`.

    The underlying `TracedAsyncClient` is built lazily, on first actual call —
    never at construction — so a module can build one of these at import time
    without failing just because the target isn't configured in this
    environment (ops endpoints, and tests that never call it).
    """

    def __init__(
        self,
        *,
        caller: str,
        target: str,
        base_url: str | None,
        unavailable: type[SiblingUnavailable] = SiblingUnavailable,
        **client_kwargs: Any,
    ) -> None:
        self._caller = caller
        self._target = target
        self._base_url = base_url
        self._unavailable = unavailable
        self._client_kwargs = client_kwargs
        self._client: TracedAsyncClient | None = None

    def _resolve(self) -> TracedAsyncClient:
        if self._client is None:
            if not self._base_url:
                raise self._unavailable(f"base URL for {self._target} is not configured")
            self._client = TracedAsyncClient(
                self._base_url, service=self._target, **self._client_kwargs
            )
        return self._client

    def headers(self) -> dict[str, str]:
        """The caller's identity and token, for every call to this sibling.

        The token comes from `MIB_SERVICE_TOKEN` in the environment, via
        `service_call_headers` — never from a config object that might get
        logged (NFR-12).
        """
        return service_call_headers(self._caller)

    async def get(self, path: str, *, fallback: Any = None, **kwargs: Any) -> Any:
        """A GET to the sibling, carrying the credential and the trace."""
        headers = {**self.headers(), **(kwargs.pop("headers", None) or {})}
        return await self._resolve().get(path, headers=headers, fallback=fallback, **kwargs)

    async def post(self, path: str, *, fallback: Any = None, **kwargs: Any) -> Any:
        """A POST to the sibling, carrying the credential and the trace.

        Not retried by default: `TracedAsyncClient` retries idempotent methods
        only, and a POST safe to repeat has to say so with `idempotent=True`.
        """
        headers = {**self.headers(), **(kwargs.pop("headers", None) or {})}
        return await self._resolve().post(path, headers=headers, fallback=fallback, **kwargs)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """The raw call, credential and trace attached, no `fallback` opinion.

        For a caller building its own response handling on top — status-code
        classification, a 404-is-valid path, typed parsing — rather than
        `get`/`post`'s "return the body, or the fallback" contract.
        """
        headers = {**self.headers(), **(kwargs.pop("headers", None) or {})}
        return await self._resolve().request(method, path, headers=headers, **kwargs)

    async def aclose(self) -> None:
        """Close the pool on shutdown, on the loop that opened it. A no-op if
        never used, since there is then nothing to close."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["SiblingClient", "SiblingUnavailable"]
