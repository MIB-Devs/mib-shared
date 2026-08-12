from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class TracedClient:
    """Thin wrapper enforcing the inter-service call contract (FR-BE-21, FR-BE-25).

    Every outbound call has a timeout and bounded retries; the ``traceparent``
    header propagates so one user request stays one trace across the hop.
    """

    def __init__(self, base_url: str, *, timeout: httpx.Timeout | None = None,
                 retries: int = 2) -> None:
        transport = httpx.HTTPTransport(retries=retries)
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout or DEFAULT_TIMEOUT,
            transport=transport,
        )

    def request(self, method: str, url: str, *, traceparent: str | None = None,
                **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if traceparent:
            headers["traceparent"] = traceparent
        return self._client.request(method, url, headers=headers, **kwargs)

    def close(self) -> None:
        self._client.close()
