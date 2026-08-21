from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    """JSON logs correlated by trace_id (NFR-16, FR-BE-08..13).

    ``StackInfoRenderer`` and ``format_exc_info`` are not optional garnish: without
    them ``log.exception(...)`` renders as ``"exc_info": true`` and the traceback
    is thrown away. That would make the unhandled-exception handler in
    :mod:`mib_shared.errors` useless - it logs the cause precisely so the generic
    500 a user sees can be tied to a real stack by the trace_id they quote
    (FR-BE-12).
    """
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=lvl)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
