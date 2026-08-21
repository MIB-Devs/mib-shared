import json

import structlog

from mib_shared import configure_logging, get_logger


def _last_line(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _fresh_logger(name: str):
    # configure_logging caches bound loggers, so a test that reconfigures has to
    # ask for a logger afterwards rather than reusing one from another test.
    structlog.reset_defaults()
    configure_logging("INFO")
    return get_logger(name)


def test_log_line_is_json_with_level_and_timestamp(capsys):
    log = _fresh_logger("t")
    log.info("did_a_thing", widget="x")
    line = _last_line(capsys)

    assert line["event"] == "did_a_thing"
    assert line["level"] == "info"
    assert line["widget"] == "x"
    assert line["timestamp"].endswith("Z")


def test_exception_logging_keeps_the_traceback(capsys):
    """Regression: without format_exc_info this rendered as "exc_info": true.

    The unhandled-exception handler logs the cause so a generic 500 can be tied
    to a real stack by the trace_id the user quotes (FR-BE-12). A boolean where
    the traceback should be makes that impossible, and does it silently.
    """
    log = _fresh_logger("t")
    try:
        raise RuntimeError("the database ate it")
    except RuntimeError:
        log.exception("unhandled_exception", path="/api/v1/auth")

    line = _last_line(capsys)
    assert line["exception"].startswith("Traceback (most recent call last)")
    assert "RuntimeError: the database ate it" in line["exception"]
    assert "test_exception_logging_keeps_the_traceback" in line["exception"]
    # The raw flag must not survive into the rendered line.
    assert "exc_info" not in line
    # Structured fields still travel alongside the traceback.
    assert line["path"] == "/api/v1/auth"


def test_a_plain_error_carries_no_exception_field(capsys):
    log = _fresh_logger("t")
    log.error("quota_exceeded", account="a-1")
    line = _last_line(capsys)

    assert line["level"] == "error"
    assert "exception" not in line
