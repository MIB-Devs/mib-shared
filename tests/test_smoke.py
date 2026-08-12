from mib_shared import ErrorEnvelope, configure_logging, get_logger


def test_error_envelope_shape():
    env = ErrorEnvelope(error_code="x", message="y")
    dumped = env.model_dump()
    assert dumped["error_code"] == "x"
    assert "request_id" in dumped and "trace_id" in dumped


def test_logging_configures():
    configure_logging("INFO")
    log = get_logger("test")
    assert log is not None
