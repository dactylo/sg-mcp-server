from collections.abc import Iterator

import pytest
from support import FixtureConnection, new_fixture_site

from sg_mcp_server import _diagnostics


@pytest.fixture
def fixture_site() -> FixtureConnection:
    return new_fixture_site()


@pytest.fixture(autouse=True)
def clear_warning_registry() -> Iterator[None]:
    _diagnostics._WARNING_CATEGORIES.clear()
    yield
    _diagnostics._WARNING_CATEGORIES.clear()
