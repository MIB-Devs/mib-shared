"""One bounded call against an OpenAI-compatible embeddings endpoint.

Two consumers send the same request and parse the same response — `mib-rag`'s
corpus backfill and `mib-retrieval`'s per-request query — and only what they
do *around* that one HTTP call differs: `mib-rag` splits a corpus into
batches, runs several concurrently, and keeps a circuit breaker across them;
`mib-retrieval` sends one prompt per call. This module is the part that was
duplicated between the two: the request/response contract and its error
classification. Batching, concurrency, and circuit-breaking stay in each
consumer — those are orchestration choices specific to a bulk backfill versus
a single interactive lookup, not mechanics of the call itself.

    POST {base_url}/embeddings
    Authorization: Bearer {api_key}
    {"model": "...", "input": ["...", ...]}
    -> {"data": [{"index": 0, "embedding": [...]}, ...], "model": "...", ...}

Verified against a live, OpenAI-compatible Qwen deployment (`mib-rag`,
24 Aug 2026) — the shape is the OpenAI Embeddings API's own, not specific to
that vendor, which is what makes this mechanics rather than domain logic.
"""
from __future__ import annotations

from collections.abc import Sequence

import httpx

from mib_shared.http_client import RetryBudgetExceeded, RetryPolicy, TracedAsyncClient
from mib_shared.telemetry import get_logger

log = get_logger("mib_shared.embeddings")

# Verified against the live API: a larger request is rejected outright rather
# than split. Every caller must batch beneath this itself.
MAX_EMBEDDING_BATCH = 20


class EmbeddingError(Exception):
    """Base class, so a caller can catch every failure from one call."""


class PermanentEmbeddingError(EmbeddingError):
    """Retrying unchanged will not help: bad config, bad response shape, wrong dimension."""


class TransientEmbeddingError(EmbeddingError):
    """Might succeed later: a timeout, a 5xx, or a rate limit that outlasted the bounded retry."""


class EmbeddingCaller:
    """Wraps one `TracedAsyncClient` bound to an embeddings endpoint.

    Construct one per process (or per test), same as `TracedAsyncClient`
    itself — nothing here is safe to share across event loops. Batch size,
    concurrency, and any circuit breaker are the caller's concern; this class
    only ever makes one HTTP call per `embed_batch` invocation.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        timeout: float = 30.0,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        service: str = "embedding",
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        if dimension < 1:
            raise ValueError("dimension must be positive")

        self._model = model
        self._dimension = dimension
        self._api_key = api_key
        self._client = TracedAsyncClient(
            base_url.rstrip("/"),
            timeout=timeout,
            retry=retry,
            service=service,
            transport=transport,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """One HTTP call for up to `MAX_EMBEDDING_BATCH` texts.

        Vectors come back in the caller's order, matched on the response's
        own ``index`` rather than position — the one thing worse than a
        failed embedding is a successful-looking one attached to the wrong
        input, silent until retrieval quietly returns the wrong result.
        """
        if not texts:
            return []
        if len(texts) > MAX_EMBEDDING_BATCH:
            raise ValueError(
                f"embed_batch takes at most {MAX_EMBEDDING_BATCH} texts per call, got "
                f"{len(texts)}; the caller must split a larger input itself"
            )
        for position, text in enumerate(texts):
            if not text or not text.strip():
                raise ValueError(f"text at index {position} is empty; refusing to embed it")

        payload = {"model": self._model, "input": list(texts)}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.post(
                "embeddings", json=payload, headers=headers, idempotent=True
            )
        except RetryBudgetExceeded as exc:
            raise TransientEmbeddingError(f"embedding call failed: {exc}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientEmbeddingError(f"embedding call failed: {exc!r}") from exc

        if response.status_code != httpx.codes.OK:
            raise _status_error(response)

        return self._vectors_from(response, len(texts))

    def _vectors_from(self, response: httpx.Response, expected: int) -> list[list[float]]:
        try:
            body = response.json()
        except ValueError as exc:
            raise PermanentEmbeddingError(f"embedding response was not JSON: {exc}") from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise PermanentEmbeddingError("embedding response has no 'data' list")
        if len(data) != expected:
            raise PermanentEmbeddingError(f"asked for {expected} embeddings, got {len(data)}")

        returned_model = body.get("model")
        if returned_model and returned_model != self._model:
            log.warning(
                "embedding_model_mismatch", requested=self._model, returned=returned_model
            )

        vectors: list[list[float] | None] = [None] * expected
        for item in data:
            if not isinstance(item, dict):
                raise PermanentEmbeddingError("embedding entry is not an object")
            index = item.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < expected:
                raise PermanentEmbeddingError(
                    f"embedding entry has an out-of-range index: {index!r}"
                )
            if vectors[index] is not None:
                raise PermanentEmbeddingError(f"embedding index {index} appeared twice")
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise PermanentEmbeddingError(f"embedding at index {index} is not a vector")
            if len(vector) != self._dimension:
                raise PermanentEmbeddingError(
                    f"model returned {len(vector)} dimensions, expected {self._dimension}"
                )
            vectors[index] = [float(value) for value in vector]

        missing = [i for i, v in enumerate(vectors) if v is None]
        if missing:
            raise PermanentEmbeddingError(f"no embedding returned for input index {missing[0]}")
        return [v for v in vectors if v is not None]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> EmbeddingCaller:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def _status_error(response: httpx.Response) -> EmbeddingError:
    """Classify a non-200 the bounded retry already gave up on.

    429 and 5xx have been retried by the shared client by the time they
    arrive here, so seeing one means the budget is spent, not that it was
    never tried.
    """
    detail = response.text[:300]
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.status_code >= 500:
        return TransientEmbeddingError(f"HTTP {response.status_code}: {detail}")
    return PermanentEmbeddingError(f"HTTP {response.status_code}: {detail}")


__all__ = [
    "EmbeddingCaller",
    "EmbeddingError",
    "MAX_EMBEDDING_BATCH",
    "PermanentEmbeddingError",
    "TransientEmbeddingError",
]
