"""One bounded call against an OpenAI-compatible chat-completions endpoint.

`mib-ai` (summaries, the chat) and `mib-ingestion` (HTML cleanup, text correction)
make the same request and parse the same response; only the timeout, the retry
budget and what is done with the text differ. This module is the part that would
be duplicated: the request, the response contract, the token usage and the
classification of a failure. What to ask, the model name, the key and the retry
policy of a given caller stay with that caller. Nothing here names a model or a
prompt: the repo is public.

    POST {base_url}/chat/completions
    Authorization: Bearer {api_key}
    {"model": "...", "messages": [...], "enable_thinking": false}
    -> {"choices": [{"message": {"content": "..."}}], "usage": {...}}

`base_url` is a base, not an endpoint (`EmbeddingCaller` appends `/embeddings` to
the same value).
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from mib_shared.http_client import RetryBudgetExceeded, RetryPolicy, TracedAsyncClient


class GenerationError(Exception):
    """Base class, so a caller can catch every failure from one call."""


class GenerationUnavailable(GenerationError):
    """Generation is not configured. Distinct from "generation failed"."""


class PermanentGenerationError(GenerationError):
    """Retrying unchanged will not help: bad config or an unparseable response."""


class TransientGenerationError(GenerationError):
    """Might succeed later: a timeout, a 5xx, or a rate limit that outlasted the bounded retry."""


@dataclass(frozen=True)
class Usage:
    """Tokens the call was billed for. Input above 32k tokens is billed at a higher rate."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Completion:
    """The assistant message and what it cost. `usage` is None when the response carried none."""

    text: str
    usage: Usage | None


class GenerationCaller:
    """Wraps one `TracedAsyncClient` bound to a chat-completions endpoint.

    Construct one per process (or per test), same as `TracedAsyncClient` itself:
    nothing here is safe to share across event loops. The default `timeout` and
    `retry` belong to the caller, since a summary wants a short one and a cleanup
    segment a long one, and `complete` can override the timeout for one call.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 15.0,
        thinking: bool = False,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        service: str = "generation",
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        self._model = model
        self._thinking = thinking
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

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        json_output: bool = False,
        thinking: bool | None = None,
        timeout: float | None = None,
    ) -> Completion:
        """One chat-completions call. Returns the assistant message's content and its usage.

        `model` overrides the caller's default for one call; `json_output` asks the model
        for a single JSON object, which the caller still validates. `thinking` overrides the
        caller's for one call: Qwen3 models reason before they answer unless told not to
        (`enable_thinking`), which made a small schema several times slower and a summary
        not finish in 90 s. `timeout` overrides the caller's for one call.

        Idempotent by convention, not by side effect: generation has none, so a retried
        call after a lost response is safe to repeat.
        """
        payload: dict[str, object] = {
            "model": model or self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        if not (self._thinking if thinking is None else thinking):
            payload["enable_thinking"] = False
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        extra = {} if timeout is None else {"timeout": timeout}
        try:
            response = await self._client.post(
                "chat/completions", json=payload, headers=headers, idempotent=True, **extra
            )
        except RetryBudgetExceeded as exc:
            raise TransientGenerationError(f"generation call failed: {exc}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientGenerationError(f"generation call failed: {exc!r}") from exc

        if response.status_code != httpx.codes.OK:
            raise _status_error(response)

        return self._completion_from(response)

    def _completion_from(self, response: httpx.Response) -> Completion:
        try:
            body = response.json()
        except ValueError as exc:
            raise PermanentGenerationError(f"generation response was not JSON: {exc}") from exc

        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise PermanentGenerationError("generation response has no 'choices'")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise PermanentGenerationError("generation response has no message content")
        return Completion(text=content, usage=_usage_from(body))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GenerationCaller:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def _usage_from(body: dict) -> Usage | None:
    """The billed tokens, or None when the response has no usable counts."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    sent = usage.get("prompt_tokens")
    received = usage.get("completion_tokens")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (sent, received)):
        return None
    return Usage(input_tokens=sent, output_tokens=received)


def _status_error(response: httpx.Response) -> GenerationError:
    """Classify a non-200 the bounded retry already gave up on.

    429 and 5xx have been retried by the shared client by the time they arrive
    here, so seeing one means the budget is spent, not that it was never tried.
    """
    detail = response.text[:300]
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.status_code >= 500:
        return TransientGenerationError(f"HTTP {response.status_code}: {detail}")
    return PermanentGenerationError(f"HTTP {response.status_code}: {detail}")


__all__ = [
    "Completion",
    "GenerationCaller",
    "GenerationError",
    "GenerationUnavailable",
    "PermanentGenerationError",
    "TransientGenerationError",
    "Usage",
]
