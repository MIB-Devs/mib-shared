"""Tests for `GenerationCaller` (extracted from `mib-ai`'s chat-completions client).

Every test drives a fake transport rather than the network: they assert what
leaves the caller, how a response is read and how a failure is classified.
"""
from __future__ import annotations

import json

import anyio
import httpx
import pytest

from mib_shared import RetryPolicy
from mib_shared.generation import (
    Completion,
    GenerationCaller,
    PermanentGenerationError,
    TransientGenerationError,
    Usage,
)

BASE = "https://qwen.example/compatible-mode/v1"


def reply(content: str = "a summary", **extra) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], **extra})


def build(handler, **kwargs) -> GenerationCaller:
    kwargs.setdefault("retry", RetryPolicy(attempts=2, backoff_seconds=0.001))
    return GenerationCaller(
        base_url=BASE,
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def complete(caller: GenerationCaller, **kwargs) -> Completion:
    async def run():
        async with caller:
            return await caller.complete(system="s", user="u", **kwargs)

    return anyio.run(run)


def test_a_call_presents_the_model_both_messages_and_the_key():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return reply()

    result = complete(build(handler))

    assert result.text == "a summary"
    body = json.loads(seen[0].content)
    assert body["model"] == "test-model"
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert seen[0].url.path.endswith("/chat/completions")


def test_the_usage_comes_back_with_the_text():
    result = complete(
        build(lambda r: reply(usage={"prompt_tokens": 120, "completion_tokens": 30}))
    )
    assert result.usage == Usage(input_tokens=120, output_tokens=30)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"usage": None},
        {"usage": {"prompt_tokens": 5}},
        {"usage": {"prompt_tokens": "5", "completion_tokens": 1}},
    ],
)
def test_a_response_without_usable_counts_has_no_usage_rather_than_zero(extra):
    assert complete(build(lambda r: reply(**extra))).usage is None


def test_one_call_can_name_its_own_model_and_ask_for_json():
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return reply("{}")

    caller = build(handler)

    async def run():
        async with caller:
            await caller.complete(system="s", user="u", model="other-model", json_output=True)
            await caller.complete(system="s", user="u")

    anyio.run(run)

    assert seen[0]["model"] == "other-model"
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert seen[1]["model"] == "test-model"
    assert "response_format" not in seen[1]


def test_thinking_is_off_unless_the_caller_or_the_call_turns_it_on():
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return reply("{}")

    plain = build(handler)
    thinking = build(handler, thinking=True)

    async def run():
        async with plain, thinking:
            await plain.complete(system="s", user="u")
            await plain.complete(system="s", user="u", thinking=True)
            await thinking.complete(system="s", user="u")
            await thinking.complete(system="s", user="u", thinking=False)

    anyio.run(run)

    assert seen[0]["enable_thinking"] is False
    assert "enable_thinking" not in seen[1]
    assert "enable_thinking" not in seen[2]
    assert seen[3]["enable_thinking"] is False


def test_a_timeout_can_be_set_for_the_caller_and_overridden_for_one_call():
    seen: list[float | None] = []

    def handler(request):
        seen.append(request.extensions["timeout"]["read"])
        return reply()

    caller = build(handler, timeout=7.0)

    async def run():
        async with caller:
            await caller.complete(system="s", user="u")
            await caller.complete(system="s", user="u", timeout=90.0)

    anyio.run(run)

    assert seen == [7.0, 90.0]


def test_a_5xx_after_the_retry_budget_is_a_transient_error():
    with pytest.raises(TransientGenerationError):
        complete(build(lambda r: httpx.Response(503)))


def test_a_429_after_the_retry_budget_is_a_transient_error():
    with pytest.raises(TransientGenerationError):
        complete(build(lambda r: httpx.Response(429)))


def test_a_400_is_a_permanent_error():
    with pytest.raises(PermanentGenerationError):
        complete(build(lambda r: httpx.Response(400, text="bad request")))


def test_a_transport_failure_is_a_transient_error():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(TransientGenerationError):
        complete(build(handler))


def test_a_retried_call_succeeds_when_the_second_try_does():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else reply()

    assert complete(build(handler)).text == "a summary"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": ["x"]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    ],
)
def test_a_response_that_cannot_be_read_is_a_permanent_error(response):
    with pytest.raises(PermanentGenerationError):
        complete(build(lambda r: response))


def test_construction_requires_base_url_key_and_model():
    with pytest.raises(ValueError):
        GenerationCaller(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError):
        GenerationCaller(base_url="https://x", api_key="", model="m")
    with pytest.raises(ValueError):
        GenerationCaller(base_url="https://x", api_key="k", model="")
