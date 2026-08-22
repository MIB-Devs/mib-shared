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

This repo is **public**, so a service installs it with no credential at all.
Services pin a **release tarball** (§8.3 — every service depends on a pinned
version, never `main`):

```toml
dependencies = [
    "mib-shared @ https://github.com/MIB-Devs/mib-shared/archive/refs/tags/v0.2.0.tar.gz",
]
```

A tarball rather than `git+https` deliberately: pip fetches it over plain HTTPS,
so no consumer needs a token in CI and no service image needs a `git` binary
installed just to resolve one dependency.

Public is what makes that true. `internal` visibility would not: an internal repo
still needs authentication to clone, and Actions' `GITHUB_TOKEN` is scoped to its
own repository, so a consumer could not read this one with it. (The repo setting
reading "accessible from repositories in the organization" governs actions and
reusable workflows, not git content — easy to misread as solving it.) This repo
can be public because it is mechanics only: no domain logic, no schema, no
business rules, nothing that is not already inferable from a FastAPI service's
shape.

To cut a release: bump `version` in `pyproject.toml` and `__version__` in
`src/mib_shared/__init__.py` together, merge, then tag the merge commit:

```bash
git tag -a v0.2.0 -m "mib-shared 0.2.0" && git push origin v0.2.0
```

Tags are immutable once a service pins them. Re-pointing a tag would change what
a service installs without changing its pin, so a correction is a new version,
never a moved tag.

| Version | Contents |
|---|---|
| `0.1.0` | Scaffold: auth verification, error envelope, telemetry, HTTP client |
| `0.2.0` | Bounded readiness probes (#3); W3C trace propagation, the error envelope with `trace_id`, the traced client with timeout/retry/fallback, and `mib-check-timeouts` (#2) |
| `0.2.1` | `configure_logging` renders tracebacks — `log.exception` was emitting `"exc_info": true` and discarding the stack |
| `0.2.2` | The 500 handler passes `trace_id` and `request_id` explicitly, so the log line for an unhandled exception carries them |
| `0.3.0` | Local token verification, capability checks, service credentials, and the JWKS cache (#1) |
| `0.4.0` | `mib_shared.migrations`: shared Alembic naming convention, options, and the `own_tables_only` autogenerate filter (#12 pending) |
| `0.5.0` | `optional_principal` for endpoints serving visitors and members alike, plus `JWKSCache.has_keys` and `.warm()` so a service can report readiness on whether it can verify anything (#14) |

### Migrating to a published wheel later

The tarball pin is not a dead end. What makes a later move cheap is that the
**version** is the interface, not the transport:

1. Add a release workflow here that triggers on a tag push and publishes to PyPI.
   Consumers are untouched until they choose to move.
2. Each service then switches one line, whenever it suits — the wheel carries the
   same version the tag did, so there is nothing to renumber and no cutover:

   ```toml
   "mib-shared @ https://github.com/.../v0.2.0.tar.gz",  # before
   "mib-shared==0.2.0",                                  # after
   ```

Note that **GitHub Packages has no Python registry** — it hosts npm, Maven,
NuGet, RubyGems and containers — so the realistic targets are PyPI (public, no
credential for consumers, publish token needed here) or a private index such as
CodeArtifact or Artifactory (a credential for every consumer, which is the thing
going public just removed). PyPI would also mean claiming the `mib-shared` name,
which is not reserved today.

Pinning commit SHAs or vendoring the source would both break the version-as-
interface property, which is why neither is used: a SHA has no version identity
to migrate, and a vendored copy has none at all.

## Authentication

Every service verifies tokens **locally** against mib-identity's published key —
no network hop per request (`FR-SSO-02`):

```python
from typing import Annotated
from fastapi import Depends
from mib_shared import JWKSCache, Principal, require_capability

keys = JWKSCache(jwks_url=settings.identity_jwks_url)

Searcher = Annotated[
    Principal,
    Depends(require_capability("regulations.search", keys=keys,
                               audience=settings.service_name,
                               issuer=settings.jwt_issuer)),
]

@router.get("/search")
async def search(principal: Searcher) -> dict:
    ...
```

Three rules the library enforces rather than trusting callers to remember:

- **Only asymmetric algorithms.** `HS256` with a public key is the classic
  confusion attack — the verifier treats the *published* key as a shared secret,
  so anyone who can read it can mint tokens. `none` likewise. The header's `alg`
  is checked against an allowlist, not obeyed.
- **`audience` is required, not optional.** A separate administrator audience is
  what makes an admin token refused by public endpoints and vice versa
  (`FR-SSO-11`), and that only works if every call states which audience it is.
- **Entitlement is per capability, never a plan name** (`FR-SSO-03`). A token
  claiming `plan: "enterprise"` grants nothing. Plans are versioned rows whose
  capability set changes without a release; an endpoint checking a plan name
  silently makes that untrue.

Missing token → **401** with `WWW-Authenticate`. Authenticated but lacking the
capability → **403**, because re-authenticating would not help and sending the
client to log in again is a loop that cannot succeed.

### Key caching

`JWKSCache` turns key management into a caching problem with three traps, each
handled deliberately:

| Trap | Handling |
|---|---|
| **Rotation** | An unknown `kid` triggers a refresh even when the cache is fresh — otherwise a rotation is invisible for a whole TTL |
| **Stampede** | Fetch attempts are rate limited, so random `kid`s cannot be used to amplify one request into an outbound call |
| **Identity down** | A failed refresh **fails static**: keep verifying with cached keys. Only an empty cache rejects. An identity outage must not take every service's authentication with it — that is the point of verifying locally |

One operational rule follows from the cooldown: **publish a key, then start
signing with it.** Identity should add a key to the JWKS and wait longer than
`min_refresh_interval` before issuing tokens against it. Then no cooldown length
can cause a spurious rejection.

### Endpoints that serve visitors and members alike

`bearer_principal` refuses a caller with no token. Some endpoints must not — a
regulation page shows a preview to anyone and the full text to a subscriber
(`FR-REG-05`, `FR-REG-20`). That is `optional_principal`:

```python
maybe_caller = optional_principal(keys=keys, audience=AUDIENCE, issuer=ISSUER)

@router.get("/regulations/{id}")
async def detail(who: Annotated[Principal | None, Depends(maybe_caller)]):
    return full_text(id) if who and who.has("regulations.read") else preview(id)
```

**No credential is anonymous; a credential that does not verify is still a 401.**
The asymmetry is the point:

- No `Authorization` header means a visitor. Nothing was claimed, nothing failed.
- A caller who presents a token is *asserting an identity*. If that assertion is
  false — expired, wrong audience, bad signature — serving the visitor view hides
  the reason. The user gets a page that has forgotten them with no explanation,
  the front end never learns to refresh, and someone probing with junk tokens
  gets a uniformly friendly response.

An expired token on a public page therefore returns 401 rather than quietly
rendering the preview. That is the right way round: a client that handles 401 by
refreshing and retrying recovers; a silent downgrade never does.

### Readiness, when identity is unreachable

Two different states, and only one of them is a readiness failure:

| | Can it verify? | `/ready` |
|---|---|---|
| Never fetched the keys | no | **not ready** |
| Fetched, identity now down | yes, from cache | ready |

`JWKSCache.has_keys` is that distinction, so a readiness probe is one line:

```python
ReadinessCheck("identity_keys", lambda: keys.has_keys)
```

Call `await keys.warm()` in the app's lifespan. It is best-effort and never
raises: a service that cannot reach identity while starting should come up and
report itself unready, so an operator reads `/ready` rather than watching a
container restart every few seconds. It also removes the first-request fetch cost
after a deploy.

### Service-to-service (`FR-BE-20`)

Being on the internal network is not authentication — a compromised container is
on it too:

```python
from mib_shared import ServiceCaller, require_service, service_call_headers

# In mib-retrieval: callable by mib-ai and nothing else (FR-BE-22).
Caller = Annotated[ServiceCaller, Depends(require_service({"mib-ai"}))]

@router.post("/internal/v1/retrieve")
async def retrieve(caller: Caller) -> dict:
    ...

# In mib-ai, calling it:
await client.post("/internal/v1/retrieve", json=payload,
                  headers=service_call_headers("mib-ai"))
```

Credentials come from `MIB_SERVICE_TOKEN_<CALLER>` environment variables and are
compared in constant time. Every failure — unknown caller, missing credential,
wrong token, or an authentic service that is not on this endpoint's allowlist —
returns the **same** 403, so a caller cannot enumerate which services exist or
which are configured.

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

## Migrations

For the four services that own a schema — `mib-identity`, `mib-ai`,
`mib-ingestion`, `mib-rag`. Their `migrations/env.py` files were byte-identical
except for which schema and version table they named; this is that, once.

```python
# migrations/env.py
from mib_shared.migrations import run_migrations
from app.tables import metadata

run_migrations(metadata=metadata)
```

```python
# app/tables.py
from mib_shared.migrations import schema_metadata

metadata = schema_metadata("identity")   # naming convention comes with it
users = Table("users", metadata, ...)
```

`run_migrations` reads `DATABASE_URL`, takes the schema from `metadata.schema`,
and configures autogenerate. Pass `version_table=` only where two services share
a schema: mib-ai keeps `alembic_version_ai` in `identity` so its history does not
collide with mib-identity's.

**Import it explicitly.** `mib_shared/__init__.py` does not, because this module
needs SQLAlchemy and Alembic and the three services without migrations should not
inherit them. Declare `mib-shared[migrations]` where you use it.

### The one thing not to get wrong

`include_schemas=True` makes Alembic reflect the whole database, and **anything
it reflects but cannot find in `target_metadata` it proposes to DROP.** One schema
here has two owners: `ai_summary_events` and `ai_summaries` belong to mib-ai but
live in `identity` (`FR-BE-15`). So without a filter, autogenerate in
mib-identity emits `drop_table` for mib-ai's tables, and autogenerate in mib-ai
emits `drop_table` for `users`. Migrations run automatically on deploy, so that
is data loss on the next release, not something review catches.

`migration_options` installs `own_tables_only(metadata)` for exactly this reason.
Every adopting service should keep two tests: one asserting the filter suppresses
the drops, and one asserting that **removing** it brings them back. A safety test
that passes vacuously is worse than none, because it reports a control that is
not holding. `mib-identity/tests/test_schema.py` is the reference.

### What the naming convention is for

`NAMING_CONVENTION` reproduces the names PostgreSQL picks itself — `users_pkey`,
`sso_sessions_user_id_fkey`, `plans_code_key`. That is deliberate and load
bearing: it lets a service with hand-written DDL adopt metadata and regenerate
its baseline with **no** change to the schema, which is what makes the
regeneration reviewable. It also means a constraint added later without an
explicit name still gets a name Alembic knows, so a later migration can drop it.

Changing these patterns invalidates every adopting service's
schema-equivalence proof at once. `tests/test_migrations.py` pins them.

### What autogenerate will not do for you

- **A rename reads as a drop plus an add.** The generated migration applies
  cleanly and destroys the column's data. Rewrite it as
  `op.alter_column(..., new_column_name=...)`.
- Extensions (`CREATE EXTENSION vector`), schema creation, triggers, grants and
  data migrations are invisible to it. `run_migrations(create_schema=True)`
  handles the schema for a fresh dev database; the rest is hand-written.

Table definitions stay in the owning service. They are domain, and holding them
here would make every schema change a shared release plus a pin bump in seven
repos (§8.3).

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
