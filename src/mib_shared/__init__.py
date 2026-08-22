"""mib-shared — versioned cross-cutting library (PRD §8.3).

This library carries ONLY cross-cutting mechanics: token verification, the
standard error envelope, telemetry and trace propagation, bounded readiness
probes, and the traced HTTP client. It must never carry domain logic or ORM
models — a shared library that knows about regulations is a distributed
monolith wearing four containers.
"""
__version__ = "0.3.0"

from mib_shared.auth import (
    ALLOWED_ALGORITHMS,
    Principal,
    ServiceCaller,
    bearer_principal,
    has_capability,
    require_capability,
    require_service,
    service_call_headers,
    verify_access_token,
)
from mib_shared.errors import ErrorEnvelope, install_error_handlers
from mib_shared.http_client import (
    DEFAULT_TIMEOUT,
    RetryBudgetExceeded,
    RetryPolicy,
    TracedAsyncClient,
    TracedClient,
)
from mib_shared.keys import JWKSCache, KeyUnavailable, StaticKey
from mib_shared.readiness import (
    ReadinessCheck,
    ReadinessReport,
    build_ops_router,
    evaluate_readiness,
    with_connect_timeout,
)
from mib_shared.telemetry import configure_logging, get_logger
from mib_shared.tracing import (
    TraceContext,
    TracingMiddleware,
    context_from_scope,
    current_context,
    new_trace_context,
    parse_traceparent,
    restored_trace,
    trace_carrier,
    trace_context,
)

__all__ = [
    "ALLOWED_ALGORITHMS",
    "DEFAULT_TIMEOUT",
    "ErrorEnvelope",
    "JWKSCache",
    "KeyUnavailable",
    "Principal",
    "ReadinessCheck",
    "ReadinessReport",
    "RetryBudgetExceeded",
    "RetryPolicy",
    "ServiceCaller",
    "StaticKey",
    "TraceContext",
    "TracedAsyncClient",
    "TracedClient",
    "TracingMiddleware",
    "bearer_principal",
    "build_ops_router",
    "configure_logging",
    "context_from_scope",
    "current_context",
    "evaluate_readiness",
    "get_logger",
    "has_capability",
    "install_error_handlers",
    "new_trace_context",
    "parse_traceparent",
    "require_capability",
    "require_service",
    "restored_trace",
    "service_call_headers",
    "trace_carrier",
    "trace_context",
    "verify_access_token",
    "with_connect_timeout",
]
