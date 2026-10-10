"""Signed reads and writes against a private Alibaba OSS bucket (FR-REG-17, NFR-11).

Generic mechanics only — a bucket, a key pair, and OSS's classic request
signature. The artifact key *naming convention* (`v1/html/{id}`,
`v1/text/{id}/{content_hash}`, ...) and which regulation a key belongs to are
domain knowledge and stay out of this module, in each consuming service
(§8.3) — same split `mib_shared.embeddings` draws between "the mechanics of
one HTTP call" and "batching, concurrency, orchestration".

`mib-regulations` uses this to read `mib-ingestion`'s content artifacts
(`mib-regulations#22`); `mib-ingestion` uses it to write them
(`mib-ingestion#3`) — one signing implementation rather than two copies
drifting apart.

    GET {endpoint}/{key}
    Date: {RFC 1123 date}
    Authorization: OSS {access_key_id}:{signature}

    PUT {endpoint}/{key}
    Date, Content-Type, Content-MD5 (both signed)
    Authorization: OSS {access_key_id}:{signature}

    DELETE {endpoint}/{key}
    GET {endpoint}/?prefix=...&marker=...&max-keys=...   (ListObjects, one page)

Same conventions as the rest of this package: every call goes through
`TracedAsyncClient` for its timeout, bounded retry and `traceparent`
propagation (FR-BE-21, FR-BE-25), and states a fallback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import anyio
import httpx

from mib_shared.http_client import RetryPolicy, TracedAsyncClient

# A file body is read and sent in pieces of this size, never held whole.
_FILE_CHUNK = 1024 * 1024
# OSS's own ceiling for one ListObjects page.
_MAX_LIST_KEYS = 1000


@dataclass(frozen=True, slots=True)
class ObjectEntry:
    """One object in a listing."""

    key: str
    last_modified: datetime
    size: int


@dataclass(frozen=True, slots=True)
class ObjectListing:
    """One ListObjects page. ``next_marker`` is None once the listing is complete."""

    entries: list[ObjectEntry]
    next_marker: str | None


def _parse_listing(body: bytes) -> ObjectListing:
    # The body comes from our own bucket over TLS; expat's entity-expansion limits
    # (Python 3.12) cover the rest. `{*}` matches with or without a namespace.
    root = ET.fromstring(body)
    entries = [
        ObjectEntry(
            key=c.findtext("{*}Key", ""),
            last_modified=datetime.fromisoformat(c.findtext("{*}LastModified", "")),
            size=int(c.findtext("{*}Size", "0")),
        )
        for c in root.findall("{*}Contents")
    ]
    if root.findtext("{*}IsTruncated", "false").strip().lower() != "true":
        return ObjectListing(entries=entries, next_marker=None)
    # Without a delimiter OSS always sends NextMarker; the last key is the same.
    marker = root.findtext("{*}NextMarker") or (entries[-1].key if entries else None)
    return ObjectListing(entries=entries, next_marker=marker)


class _FileBody:
    """A file sent in chunks. Not a generator, so httpx lets it be iterated again:
    each attempt of a retried PUT opens the file afresh and sends all of it."""

    def __init__(self, path: os.PathLike[str]) -> None:
        self._path = path

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async with await anyio.open_file(self._path, "rb") as f:
            while chunk := await f.read(_FILE_CHUNK):
                yield chunk


def _file_md5_and_size(path: os.PathLike[str]) -> tuple[str, int]:
    digest = hashlib.md5()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(_FILE_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return base64.b64encode(digest.digest()).decode(), size


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

    def _authorization(
        self,
        *,
        verb: str,
        resource: str,
        date: str,
        content_md5: str = "",
        content_type: str = "",
    ) -> str:
        """OSS's classic (v1) signature.

        HMAC-SHA1 over ``VERB\\nContent-MD5\\nContent-Type\\nDate\\nCanonicalizedResource``,
        base64-encoded. A body-less request (a GET) has nothing to put on the
        Content-MD5/Content-Type lines, so they are left empty.
        """
        string_to_sign = f"{verb}\n{content_md5}\n{content_type}\n{date}\n{resource}"
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

    async def put_object(
        self,
        key: str,
        body: bytes | os.PathLike[str],
        *,
        content_type: str,
        fallback: Any = None,
    ) -> Any:
        """A PUT of one object, overwriting any object at `key`.

        `Content-MD5` is sent and signed, so OSS rejects a body corrupted in
        transit rather than storing it. Writing the same bytes to the same key
        twice is harmless, so the PUT is retried like a GET. Returns the
        `httpx.Response` (any status, for the caller to interpret), or
        `fallback`'s result once the retry budget is exhausted (FR-BE-21).

        `body` may be a path to a file instead of bytes: the file is read in 1 MiB
        pieces for its MD5 and again for the upload, with its `Content-Length`,
        so a source of hundreds of MB never sits in memory (`mib-ingestion#75`).
        """
        key = key.lstrip("/")
        resource = f"/{self._bucket}/{key}"
        date = format_datetime(datetime.now(UTC), usegmt=True)
        extra: dict[str, str] = {}
        content: bytes | _FileBody
        if isinstance(body, os.PathLike):
            content_md5, size = await anyio.to_thread.run_sync(_file_md5_and_size, body)
            # Set explicitly, so httpx sends a sized body rather than chunked.
            extra["Content-Length"] = str(size)
            content = _FileBody(body)
        else:
            content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
            content = body
        headers = {
            **extra,
            "Date": date,
            "Content-Type": content_type,
            "Content-MD5": content_md5,
            "Authorization": self._authorization(
                verb="PUT",
                resource=resource,
                date=date,
                content_md5=content_md5,
                content_type=content_type,
            ),
        }
        return await self._http.request(
            "PUT", f"/{key}", headers=headers, content=content, fallback=fallback, idempotent=True
        )

    async def delete_object(self, key: str, *, fallback: Any = None) -> Any:
        """A DELETE of one object.

        OSS answers 204 whether or not the object existed, so a repeat is
        harmless and the DELETE is retried like a GET. Returns the
        `httpx.Response` (any status, for the caller to interpret), or
        `fallback`'s result once the retry budget is exhausted (FR-BE-21).
        """
        key = key.lstrip("/")
        resource = f"/{self._bucket}/{key}"
        date = format_datetime(datetime.now(UTC), usegmt=True)
        headers = {
            "Date": date,
            "Authorization": self._authorization(verb="DELETE", resource=resource, date=date),
        }
        return await self._http.request(
            "DELETE", f"/{key}", headers=headers, fallback=fallback, idempotent=True
        )

    async def list_objects(
        self,
        prefix: str,
        *,
        marker: str = "",
        max_keys: int = _MAX_LIST_KEYS,
        fallback: Any = None,
    ) -> Any:
        """One ListObjects page of the keys under `prefix`, in key order.

        Pass the previous page's ``next_marker`` as `marker` to continue. Returns
        an `ObjectListing`, or `fallback`'s result once the retry budget is
        exhausted (FR-BE-21). A non-2xx answer that is not retried (a 403 for a
        key without list permission) raises `httpx.HTTPStatusError`: unlike a
        GET's 404, it has no meaning a caller could act on.
        """
        if not 1 <= max_keys <= _MAX_LIST_KEYS:
            raise ValueError(f"max_keys must be 1..{_MAX_LIST_KEYS}")
        # Query parameters other than OSS's sub-resources are not signed.
        resource = f"/{self._bucket}/"
        date = format_datetime(datetime.now(UTC), usegmt=True)
        headers = {
            "Date": date,
            "Authorization": self._authorization(verb="GET", resource=resource, date=date),
        }
        params = {"prefix": prefix.lstrip("/"), "max-keys": str(max_keys)}
        if marker:
            params["marker"] = marker
        resp = await self._http.get(
            "/", headers=headers, params=params, fallback=fallback, idempotent=True
        )
        if not isinstance(resp, httpx.Response):
            return resp
        resp.raise_for_status()
        return _parse_listing(resp.content)

    async def iter_objects(self, prefix: str) -> AsyncIterator[ObjectEntry]:
        """Every object under `prefix`, one page in memory at a time.

        No fallback: a page that cannot be read raises, so a caller never takes a
        partial listing for the whole one.
        """
        marker = ""
        while True:
            page = await self.list_objects(prefix, marker=marker)
            for entry in page.entries:
                yield entry
            if page.next_marker is None:
                return
            marker = page.next_marker

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> OSSClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


__all__ = ["ObjectEntry", "ObjectListing", "OSSClient"]
