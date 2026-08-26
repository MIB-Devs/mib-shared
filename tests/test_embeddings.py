"""Tests for `EmbeddingCaller` (extracted from `mib-rag`'s and `mib-retrieval`'s
duplicated Qwen-call logic).

Every test drives a fake transport rather than the network, mirroring
`mib-rag/tests/test_embedding.py`'s approach for the batch client built on
top of this.
"""
from __future__ import annotations

import json

import anyio
import httpx
import pytest

from mib_shared import RetryPolicy
from mib_shared.embeddings import (
    MAX_EMBEDDING_BATCH,
    EmbeddingCaller,
    PermanentEmbeddingError,
    TransientEmbeddingError,
)

DIM = 4
BASE = "https://embeddings.example/compatible-mode/v1"


def vector(seed: float) -> list[float]:
    return [seed + i for i in range(DIM)]


def ok_response(request: httpx.Request, *, reverse: bool = False) -> httpx.Response:
    texts = json.loads(request.content)["input"]
    data = [
        {"object": "embedding", "index": i, "embedding": vector(float(i))}
        for i, _ in enumerate(texts)
    ]
    if reverse:
        data.reverse()
    return httpx.Response(
        200,
        json={"object": "list", "data": data, "model": "test-model", "usage": {"prompt_tokens": 5}},
    )


def build(handler, **kwargs) -> EmbeddingCaller:
    kwargs.setdefault("retry", RetryPolicy(attempts=2, backoff_seconds=0.001))
    return EmbeddingCaller(
        base_url=BASE,
        api_key="test-key",
        model="test-model",
        dimension=DIM,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def embed_batch(caller: EmbeddingCaller, texts):
    async def run():
        async with caller:
            return await caller.embed_batch(texts)

    return anyio.run(run)


def test_embeds_texts_in_order():
    result = embed_batch(build(ok_response), ["satu", "dua", "tiga"])
    assert result == [vector(0.0), vector(1.0), vector(2.0)]


def test_a_single_text_batch_works_the_same_way():
    result = embed_batch(build(ok_response), ["satu"])
    assert result == [vector(0.0)]


def test_vectors_are_matched_by_index_not_position():
    def handler(request):
        return ok_response(request, reverse=True)

    result = embed_batch(build(handler), ["satu", "dua"])
    assert result == [vector(0.0), vector(1.0)]


def test_request_carries_the_key_the_model_and_a_plain_list():
    seen = []

    def handler(request):
        seen.append(request)
        return ok_response(request)

    embed_batch(build(handler), ["satu"])

    request = seen[0]
    assert request.url.path.endswith("/embeddings")
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body == {"model": "test-model", "input": ["satu"]}


def test_empty_input_returns_empty_without_a_call():
    seen = []

    def handler(request):
        seen.append(request)
        return ok_response(request)

    assert embed_batch(build(handler), []) == []
    assert seen == []


def test_a_batch_over_the_cap_is_rejected_before_any_call():
    caller = build(lambda request: ok_response(request))
    with pytest.raises(ValueError):
        embed_batch(caller, ["x"] * (MAX_EMBEDDING_BATCH + 1))


def test_an_empty_text_is_rejected_before_any_call():
    caller = build(lambda request: ok_response(request))
    with pytest.raises(ValueError):
        embed_batch(caller, ["  "])


def test_a_dimension_mismatch_is_permanent():
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    with pytest.raises(PermanentEmbeddingError):
        embed_batch(build(handler), ["satu"])


def test_a_short_data_list_is_permanent():
    def handler(request):
        return httpx.Response(200, json={"data": []})

    with pytest.raises(PermanentEmbeddingError):
        embed_batch(build(handler), ["satu", "dua"])


def test_a_malformed_response_is_permanent():
    def handler(request):
        return httpx.Response(200, content=b"not json")

    with pytest.raises(PermanentEmbeddingError):
        embed_batch(build(handler), ["satu"])


def test_a_client_error_is_permanent():
    def handler(request):
        return httpx.Response(400, text="bad request")

    with pytest.raises(PermanentEmbeddingError):
        embed_batch(build(handler), ["satu"])


def test_a_server_error_that_outlasts_retry_is_transient():
    def handler(request):
        return httpx.Response(503, text="unavailable")

    with pytest.raises(TransientEmbeddingError):
        embed_batch(build(handler), ["satu"])


def test_constructor_rejects_missing_configuration():
    with pytest.raises(ValueError):
        EmbeddingCaller(base_url="", api_key="k", model="m", dimension=4)
    with pytest.raises(ValueError):
        EmbeddingCaller(base_url=BASE, api_key="", model="m", dimension=4)
    with pytest.raises(ValueError):
        EmbeddingCaller(base_url=BASE, api_key="k", model="", dimension=4)
    with pytest.raises(ValueError):
        EmbeddingCaller(base_url=BASE, api_key="k", model="m", dimension=0)
