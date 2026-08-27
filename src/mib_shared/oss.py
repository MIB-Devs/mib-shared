"""Signed reads from a private Alibaba OSS bucket (FR-REG-17, NFR-11).

Generic mechanics only — a bucket, a key pair, and OSS's classic request
signature. The artifact key *naming convention* (`v1/html/{id}`,
`v1/text/{id}/{content_hash}`, ...) and which regulation a key belongs to are
domain knowledge and stay out of this module, in each consuming service
(§8.3) — same split `mib_shared.embeddings` draws between "the mechanics of
one HTTP call" and "batching, concurrency, orchestration".

`mib-regulations` uses this to read `mib-ingestion`'s content artifacts
(`mib-regulations#22`); `mib-ingestion` will use it to write them once its
adapter/converter is built (`mib-ingestion#3`) — one signing implementation
rather than two copies drifting apart.

    GET {endpoint}/{key}
    Date: {RFC 1123 date}
    Authorization: OSS {access_key_id}:{signature}

Same conventions as the rest of this package: every call goes through
`TracedAsyncClient` for its timeout, bounded retry and `traceparent`
propagation (FR-BE-21, FR-BE-25), and states a fallback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import httpx

from mib_shared.http_client import RetryPolicy, TracedAsyncClient


class OSSClient:
    """One signed-request client for one bucket, built once per process.

    Async because the callers are async, same reasoning `TracedAsyncClient`
    itself documents — nothing here is safe to share across event loops.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        timeout: float = 10.0,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        service: str = "oss",
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is required")
        if not bucket:
            raise ValueError("bucket is required")
        if not access_key_id:
            raise ValueError("access_key_id is required")
        if not access_key_secret:
            raise ValueError("access_key_secret is required")

        self._bucket = bucket
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._http = TracedAsyncClient(
            endpoint.rstrip("/"),
            timeout=timeout,
            retry=retry,
            service=service,
            transport=transport,
        )

    def _authorization(self, *, verb: str, resource: str, date: str) -> str:
        """OSS's classic (v1) signature.

        HMAC-SHA1 over ``VERB\\nContent-MD5\\nContent-Type\\nDate\\nCanonicalizedResource``,
        base64-encoded. A body-less request (a GET) has nothing to put on the
        Content-MD5/Content-Type lines, so they are left empty.
        """
        string_to_sign = f"{verb}\n\n\n{date}\n{resource}"
        digest = hmac.new(
            self._access_key_secret.encode(), string_to_sign.encode(), hashlib.sha1
        ).digest()
        return f"OSS {self._access_key_id}:{base64.b64encode(digest).decode()}"

    async def get_object(self, key: str, *, fallback: Any = None) -> Any:
        """A GET for one object.

        `key` is taken with no leading slash. Returns the `httpx.Response` on
        success (any status, including 404 — not itself a retryable status,
        so it comes back as-is for the caller to interpret), or `fallback`'s
        result once the retry budget for a transport failure or a retryable
        status (503 etc.) is exhausted (FR-BE-21).
        """
        key = key.lstrip("/")
        resource = f"/{self._bucket}/{key}"
        date = format_datetime(datetime.now(UTC), usegmt=True)
        headers = {
            "Date": date,
            "Authorization": self._authorization(verb="GET", resource=resource, date=date),
        }
        return await self._http.get(
            f"/{key}", headers=headers, fallback=fallback, idempotent=True
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> OSSClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


__all__ = ["OSSClient"]
