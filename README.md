# mib-shared

> Versioned cross-cutting library — NOT a service

Auth middleware, the standard error envelope, telemetry and W3C trace propagation, bounded readiness probes, and a traced HTTP client. It carries only mechanics; **never** domain logic or ORM models (§8.3). Every FastAPI service depends on a pinned version of this package.

Part of the **MIB Platform** (Tax Regulations Portal & LMS). See the canonical
spec in [`MIB-Devs/.github` → `PRD.md`](https://github.com/MIB-Devs/.github/blob/main/PRD.md).

- **Stack:** Python 3.12 library (packaged, versioned)
- **PRD references:** §8.3, FR-BE-07, FR-BE-13, FR-BE-12, FR-BE-18, FR-BE-21, FR-BE-25, FR-SSO-02/03, NFR-16, OPS-17
- **Deployment:** one Docker Compose project on a single Alibaba ECS host (AD-3).
  This service is **stateless** (AS-2) and addressed by DNS name, never `localhost`.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install .[dev]
pytest -q
```

## Releasing, and how services pin it

The library is private, so a service cannot `pip install mib-shared` from an
index. Services pin a **git tag** instead (§8.3 — every service depends on a
pinned version, never `main`):

```toml
dependencies = [
    "mib-shared @ git+https://github.com/MIB-Devs/mib-shared@v0.2.0",
]
```

CI and image builds authenticate with an org-level fine-grained PAT
(`MIB_CI_TOKEN`, read-only, `mib-shared` only) — see a consumer's `ci.yml` for
the three lines involved. If `mib-shared` ever becomes internal or public, the
same pin works with the token removed.

To cut a release: bump `version` in `pyproject.toml` and `__version__` in
`src/mib_shared/__init__.py` together, merge, then tag the merge commit:

```bash
git tag -a v0.2.0 -m "mib-shared 0.2.0" && git push origin v0.2.0
```

Tags are immutable once a service pins them. Re-pointing a tag would change what
a service installs without changing its lockfile or its pin, so a correction is a
new version, never a moved tag.

| Version | Contents |
|---|---|
| `0.1.0` | Scaffold: auth verification, error envelope, telemetry, HTTP client |
| `0.2.0` | Bounded readiness probes (#3); W3C trace propagation, the error envelope with `trace_id`, the traced client with timeout/retry/fallback, and `mib-check-timeouts` (#2) |

### Migrating to a published wheel later

The git-tag pin is the cheapest thing that works while the repo is private; it is
not a dead end. What makes a later move cheap is that the **version** is the
interface, not the transport:

1. Add a release workflow here that triggers on a tag push and publishes the
   wheel (GitHub Packages, or any private index). Consumers are untouched.
2. Each service then switches one line, whenever it suits — the published wheel
   carries the same version the tag did, so there is nothing to renumber and no
   big-bang cutover:

   ```toml
   "mib-shared @ git+https://github.com/MIB-Devs/mib-shared@v0.2.0",  # before
   "mib-shared==0.2.0",                                              # after
   ```

3. Auth keeps its shape: the same read-only token, with `read:packages` instead
   of repo read. The BuildKit secret mount in each service Dockerfile does not
   change — only the `pip` line inside it.

Pinning commit SHAs or vendoring the source would both break that property, which
is why neither is used: a SHA has no version identity to migrate, and a vendored
copy has none at all.

## Tracing and the error envelope

Every service mounts the middleware and the handlers, in this order:

```python
from mib_shared import TracingMiddleware, install_error_handlers

app.add_middleware(TracingMiddleware)   # extracts/starts the trace, echoes the ids
install_error_handlers(app)             # one error shape, carrying trace_id
```

`TracingMiddleware` continues an inbound `traceparent` (Traefik starts it at the
edge) or begins a trace, binds `trace_id`/`span_id`/`request_id` onto every log
line for the request, and echoes them back as `x-trace-id`, `x-request-id` and
`traceparent` — on successes too, since the reference a user quotes to support
has to exist before anything goes wrong (FR-BE-12).

### Outbound calls

`TracedAsyncClient` is the only sanctioned way to call another service. It
carries a timeout, a bounded retry, a defined fallback, and `traceparent`
propagation as one unit (FR-BE-21, FR-BE-25):

```python
from mib_shared import TracedAsyncClient

client = TracedAsyncClient("http://mib-retrieval:8000", service="retrieval")

# Degrades instead of failing: a Qwen outage becomes lexical search, not a 500.
hits = await client.post(
    "/internal/retrieve",
    json={"q": prompt},
    fallback=lambda cause: None,
)
```

Retries apply to idempotent methods only. A `POST` is retried **only** when the
caller declares `idempotent=True` — repeating a POST that already created a
subscription is worse than failing it. Use `TracedClient` (sync) in workers and
one-shot jobs, never inside an async handler.

### Across a queue hop

A background job runs in another process, later, so the trace has to be carried
on the job record (FR-BE-13):

```python
from mib_shared import restored_trace, trace_carrier

job.trace = trace_carrier()             # at enqueue, inside the request

with restored_trace(job.trace):         # in the worker
    do_the_work()                       # same trace_id, its own span
```

### Timeouts are enforced, not requested

The package ships a check that every service's CI runs over its own source:

```bash
mib-check-timeouts src app
```

It fails the build on any `httpx`/`requests` call or client built without a
timeout. Ruff cannot express this, and review misses it exactly once — which is
all it takes. A deliberate exception is marked in the source with
`# mib: timeout-ok`.

## Ops endpoints

Services build `/health` and `/ready` from this library so both behave
identically everywhere:

```python
from mib_shared import ReadinessCheck, build_ops_router, with_connect_timeout

def check_database() -> bool | None:
    if not settings.database_url:
        return None  # no such dependency here — not a failure
    engine = create_engine(with_connect_timeout(settings.database_url))
    ...

app.include_router(
    build_ops_router(
        service_name=settings.service_name,
        version=settings.version,
        checks=[ReadinessCheck("database", check_database)],
    )
)
```

> **Boundary rule:** if a change to this library forces every service to redeploy for a *domain* reason, the change does not belong here.

## Conventions

- Config comes from the environment only — never commit secrets (NFR-12). Copy
  `.env.example` to `.env` for local development.
- Cross-service calls go over HTTP against another service's published API,
  never its database (FR-BE-14). Every outbound call has a timeout, bounded
  retry, and a defined fallback (FR-BE-21) — enforced by `mib-check-timeouts`
  in CI, not by review.
- Cross-cutting mechanics (token verification, error envelope, telemetry, traced
  HTTP client) will be consumed from the `mib-shared` library (§8.3).
- Readiness probes are **bounded and off the event loop**. A sync driver call
  belongs in a `ReadinessCheck`, which runs it in a worker thread under a
  timeout; `/ready` answers 503 rather than hanging, and `/health` keeps
  answering while a dependency is down (FR-BE-18, OPS-17).
