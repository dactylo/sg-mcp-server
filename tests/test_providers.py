import os
import subprocess
import sys
from pathlib import Path

from shotgun_api3.shotgun import Shotgun, ShotgunError
from support import FixtureConnection

from sg_mcp_server import _providers
from sg_mcp_server._config import Config
from sg_mcp_server._providers import SGReadConnection, connect
from tests.cli_helpers import PROCESS_GROUP_FLAGS, finish_process, process_transcript

_BLOCKED_PROVIDER_SCRIPT = """
import http.client
import importlib.abc
import sys
import xmlrpc.client


class ProviderBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'shotgun_api3' or fullname.startswith('shotgun_api3.'):
            raise ModuleNotFoundError(f'blocked optional provider: {fullname}')
        return None


sys.meta_path.insert(0, ProviderBlocker())
sys.modules.pop('shotgun_api3', None)

import sg_mcp_server
import sg_mcp_server._engine
from sg_mcp_server._providers import provider_transport_error_types

assert provider_transport_error_types() == (
    OSError, http.client.HTTPException, xmlrpc.client.ProtocolError
)

try:
    import shotgun_api3
except ModuleNotFoundError as error:
    assert 'blocked optional provider' in str(error)
else:
    raise AssertionError('provider blocker did not run')
"""
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _PROJECT_ROOT / 'src'
_UNRESPONSIVE_SITE_SCRIPT = """
import json
import socket
import sys
from pathlib import Path

from sg_mcp_server import _tools
from sg_mcp_server._config import Config
from sg_mcp_server._providers import connect
from sg_mcp_server._server import build_server
from tests.server._helpers import assert_error_is_fenced, call

with socket.create_server(('127.0.0.1', 0)) as listener:
    config = Config(
        site_url=f'https://127.0.0.1:{listener.getsockname()[1]}',
        script_name='s',
        api_key='k',
        audit_path=Path(sys.argv[1]),
        timeout_seconds=1,
    )
    connection = connect(config)
    server = build_server(config, connection, _tools.surface())
    for _ in range(2):
        result = call(server, 'sg_schema', {'op': 'entities'})
        body = assert_error_is_fenced(result)
        assert body['error'].startswith('the SG site request failed (TimeoutError: ')
    outcomes = [json.loads(line)['outcome'] for line in config.audit_path.read_text().splitlines()]
    assert outcomes == ['query_error', 'query_error']
"""


class TestConnect:
    def test_builds_live_client_lazily(self) -> None:
        config = Config(
            site_url='https://example.shotgunstudio.com',
            script_name='s',
            api_key='k',
        )
        connection = connect(config)
        assert type(connection) is Shotgun
        assert isinstance(connection, SGReadConnection)
        assert connection.config.timeout_secs == 60

    def test_client_uses_the_configured_timeout(self) -> None:
        config = Config(
            site_url='https://example.shotgunstudio.com',
            script_name='s',
            api_key='k',
            timeout_seconds=7,
        )
        connection = connect(config)
        assert isinstance(connection, Shotgun)
        assert connection.config.timeout_secs == 7

    def test_unresponsive_site_times_out_and_releases_the_connection(self, tmp_path: Path) -> None:
        environment = dict(os.environ)
        environment['PYTHONPATH'] = os.pathsep.join(
            str(path) for path in (_SRC_ROOT, _PROJECT_ROOT / 'tests', _PROJECT_ROOT)
        )
        process = subprocess.Popen(
            [sys.executable, '-c', _UNRESPONSIVE_SITE_SCRIPT, str(tmp_path / 'audit.jsonl')],
            cwd=_PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            creationflags=PROCESS_GROUP_FLAGS,
        )
        result = finish_process(process, timeout=30, cwd=_PROJECT_ROOT)
        assert result.returncode == 0, process_transcript(result, cwd=_PROJECT_ROOT)


class TestOptionalProfile:
    def test_package_and_engine_import_without_provider(self) -> None:
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(_SRC_ROOT)

        completed = subprocess.run(
            [sys.executable, '-c', _BLOCKED_PROVIDER_SCRIPT],
            cwd=_PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr

    def test_provider_error_type_resolves_installed_error_lazily(self) -> None:
        assert _providers.provider_error_type() is ShotgunError


class TestProtocol:
    def test_fixture_connection_satisfies_protocol(self, fixture_site: FixtureConnection) -> None:
        assert isinstance(fixture_site, SGReadConnection)
