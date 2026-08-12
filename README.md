# mib-shared

> Versioned cross-cutting library — NOT a service

Auth middleware, the standard error envelope, telemetry setup, and a traced HTTP client. It carries only mechanics; **never** domain logic or ORM models (§8.3). Every FastAPI service depends on a pinned version of this package.

Part of the **MIB Platform** (Tax Regulations Portal & LMS). See the canonical
spec in [`MIB-Devs/.github` → `PRD.md`](https://github.com/MIB-Devs/.github/blob/main/PRD.md).

- **Stack:** Python 3.12 library (packaged, versioned)
- **PRD references:** §8.3, FR-BE-07, FR-BE-12, FR-BE-21, FR-BE-25, FR-SSO-02/03, NFR-16
- **Deployment:** one Docker Compose project on a single Alibaba ECS host (AD-3).
  This service is **stateless** (AS-2) and addressed by DNS name, never `localhost`.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install .[dev]
pytest -q
```

> **Boundary rule:** if a change to this library forces every service to redeploy for a *domain* reason, the change does not belong here.

## Conventions

- Config comes from the environment only — never commit secrets (NFR-12). Copy
  `.env.example` to `.env` for local development.
- Cross-service calls go over HTTP against another service's published API,
  never its database (FR-BE-14). Every outbound call has a timeout, bounded
  retry, and a defined fallback (FR-BE-21).
- Cross-cutting mechanics (token verification, error envelope, telemetry, traced
  HTTP client) will be consumed from the `mib-shared` library (§8.3).
