"""mib-shared — versioned cross-cutting library (PRD §8.3).

This library carries ONLY cross-cutting mechanics: token verification, the
standard error envelope, telemetry setup, bounded readiness probes, and the
traced HTTP client. It must
never carry domain logic or ORM models — a shared library that knows about
regulations is a distributed monolith wearing four containers.
"""
__version__ = "0.1.0"

from mib_shared.errors import ErrorEnvelope, install_error_handlers
from mib_shared.http_client import TracedClient
from mib_shared.readiness import (
    ReadinessCheck,
    ReadinessReport,
    build_ops_router,
    evaluate_readiness,
    with_connect_timeout,
)
from mib_shared.telemetry import configure_logging, get_logger

__all__ = [
    "ErrorEnvelope",
    "ReadinessCheck",
    "ReadinessReport",
    "TracedClient",
    "build_ops_router",
    "configure_logging",
    "evaluate_readiness",
    "get_logger",
    "install_error_handlers",
    "with_connect_timeout",
]
