from pathlib import Path

import pytest
from mcp.server import Server
from support import FixtureConnection

from tests.server._helpers import build_test_server


@pytest.fixture
def server(fixture_site: FixtureConnection, tmp_path: Path) -> Server:
    return build_test_server(fixture_site, tmp_path)
