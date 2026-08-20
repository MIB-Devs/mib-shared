import pytest


@pytest.fixture
def anyio_backend() -> str:
    # asyncio only — this is the loop the services actually run under.
    return "asyncio"
