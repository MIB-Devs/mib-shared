import pytest


@pytest.fixture
def anyio_backend() -> str:
    """asyncio only: the loop the services run under."""
    return "asyncio"
