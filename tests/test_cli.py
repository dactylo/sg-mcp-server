import argparse
import asyncio
import contextlib
import errno
import importlib.metadata
import io
import json
import os
import signal
import sys
import textwrap
import traceback
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Never

import pytest
from mcp.server import Server

import sg_mcp_server
from sg_mcp_server import _cli, _downstream
from sg_mcp_server._cli import _build_parser, main
from sg_mcp_server._config import Config
from tests.cli_helpers import (
    invoke_cli,
    process_transcript,
    run_cli,
    run_setup,
    run_with_closed_output,
    start_session,
    wait_with_input_open,
)

if TYPE_CHECKING:
    import anyio

_INITIALIZE = {
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'initialize',
    'params': {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'sg-mcp-server-tests', 'version': '0'},
    },
}
_INITIALIZED = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
_LIST_TOOLS = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}
_SG_CLIENT_MISSING = (
    'error: the SG client is not installed; reinstall sg-mcp-server with the official extra\n'
)
_SIGINT_UNAVAILABLE = pytest.mark.skipif(
    sys.platform == 'win32' or signal.getsignal(signal.SIGINT) is signal.SIG_IGN,
    reason='Windows delivers Ctrl-C only through a console, and an ignored SIGINT is inherited',
)
_SITE_SETTINGS = {
    'SG_MCP_SITE_URL': 'https://fixture.shotgunstudio.com',
    'SG_MCP_SCRIPT_NAME': 'sg-mcp-server-tests',
    'SG_MCP_API_KEY': 'not-a-real-key',
}
_SITE_URL_ERROR = (
    'error: the SG_MCP_SITE_URL setting must be an HTTPS origin with a valid host and port, '
    'without userinfo, a non-root path, a query, or a fragment\n'
)

# A stop takes well under a second; this bounds the wait when a regression keeps serving.
_STOP_TIMEOUT_SECONDS = 30


def _fixed_version(_name: str) -> str:
    return '9.8.7'


def _help_entries(
    parser: argparse.ArgumentParser, prefix: str
) -> Iterator[tuple[str, str | None]]:
    for action in parser._actions:
        if action.help is not argparse.SUPPRESS:
            label = '/'.join(action.option_strings) or action.dest
            yield f'{prefix} {label}', action.help


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each test sets the server settings it needs; none come from the developer.
    for name in list(os.environ):
        if name.startswith('SG_MCP_'):
            monkeypatch.delenv(name)


def _message(payload: Mapping[str, object]) -> str:
    return json.dumps(payload) + '\n'


def _metadata_missing(name: str) -> str:
    raise importlib.metadata.PackageNotFoundError(name)


def _process_environment(tmp_path: Path) -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith('SG_MCP_')
    }
    return environment | _SITE_SETTINGS | {'SG_MCP_AUDIT_PATH': str(tmp_path / 'audit.jsonl')}


def _starts_in_lowercase_style(text: str) -> bool:
    first = text.split(maxsplit=1)[0]
    is_acronym = first.upper() == first and any(character.isalpha() for character in first)
    return text[0].islower() or is_acronym


@pytest.fixture
def site_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in _SITE_SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('SG_MCP_AUDIT_PATH', str(tmp_path / 'audit.jsonl'))


@pytest.mark.parametrize('argv', [['serve'], ['--vers'], ['--site-url', 'https://x.test']])
def test_arguments_other_than_help_and_version_are_usage_errors(argv: list[str]) -> None:
    status, stdout, stderr = invoke_cli(argv)

    assert (status, stdout) == (2, '')
    assert stderr == (
        'usage: sg-mcp-server [-h] [--version]\n'
        f'sg-mcp-server: error: unrecognized arguments: {" ".join(argv)}\n'
    )


@pytest.mark.usefixtures('site_settings')
@pytest.mark.parametrize(
    'failure',
    [
        pytest.param(ExceptionGroup('serve', [BrokenPipeError(errno.EPIPE, 'pipe')]), id='flat'),
        pytest.param(
            ExceptionGroup(
                'serve', [ExceptionGroup('stdio', [BrokenPipeError(errno.EPIPE, 'p')])]
            ),
            id='nested',
        ),
    ],
)
def test_client_disconnect_exits_quietly(
    monkeypatch: pytest.MonkeyPatch, failure: ExceptionGroup[Exception]
) -> None:
    async def disconnected(_server: Server) -> None:
        raise failure

    monkeypatch.setattr(_cli, '_serve_stdio', disconnected)

    assert invoke_cli([]) == (0, '', '')


def test_client_disconnect_stops_with_input_open(tmp_path: Path) -> None:
    process, response = start_session(
        _message(_INITIALIZE).encode(), env=_process_environment(tmp_path)
    )
    stdin = process.stdin
    stdout = process.stdout
    assert stdin is not None
    assert stdout is not None
    stdout.close()
    process.stdout = None  # communicate() must not read the closed pipe on Windows
    with contextlib.suppress(BrokenPipeError):  # the transcript reports an early exit
        stdin.write((_message(_INITIALIZED) + _message(_LIST_TOOLS)).encode())
        stdin.flush()

    result = wait_with_input_open(process, timeout=_STOP_TIMEOUT_SECONDS)

    assert (result.returncode, result.stderr) == (0, b''), process_transcript(result)
    server_info = json.loads(response)['result']['serverInfo']
    assert server_info == {
        'name': 'sg-mcp-server',
        'version': importlib.metadata.version('sg-mcp-server'),
    }


def test_closed_stdout_before_help_is_quiet() -> None:
    result = run_with_closed_output(['--help'])
    assert (result.returncode, result.stderr) == (0, b''), process_transcript(result)


def test_closed_stdout_before_version_is_quiet() -> None:
    result = run_with_closed_output(['--version'])
    assert (result.returncode, result.stderr) == (0, b''), process_transcript(result)


def test_error_lines_escape_control_characters(capsys: pytest.CaptureFixture[str]) -> None:
    _cli._report_error('bad\x1b[31m value\n')

    assert capsys.readouterr().err == 'error: bad\\x1b[31m value\\x0a\n'


def test_every_visible_option_and_command_has_lowercase_help() -> None:
    problems = [
        f'{name}: {text!r}'
        for name, text in _help_entries(_build_parser(), 'sg-mcp-server')
        if not text or not _starts_in_lowercase_style(text) or text.endswith('.')
    ]
    assert problems == []


def test_help_goes_to_stdout() -> None:
    status, stdout, stderr = invoke_cli(['--help'])
    assert (status, stderr) == (0, '')
    assert ' '.join(stdout.split('\n\n', 1)[0].split()) == (
        'usage: sg-mcp-server [-h] [--version]'
    )
    settings = [
        line.split()[0] for line in stdout.splitlines() if line.lstrip().startswith('SG_MCP_')
    ]
    assert len(settings) == 10
    assert set(settings) == {
        'SG_MCP_SITE_URL',
        'SG_MCP_SCRIPT_NAME',
        'SG_MCP_API_KEY',
        'SG_MCP_TIMEZONE',
        'SG_MCP_AUDIT_PATH',
        'SG_MCP_DIAGNOSTICS_PATH',
        'SG_MCP_DEFAULT_LIMIT',
        'SG_MCP_HARD_LIMIT',
        'SG_MCP_SCHEMA_TTL_SECONDS',
        'SG_MCP_TIMEOUT_SECONDS',
    }


@pytest.mark.parametrize(
    ('settings', 'message'),
    [
        pytest.param(
            {'SG_MCP_SITE_URL': 'http://example.shotgunstudio.com'}, _SITE_URL_ERROR, id='http'
        ),
        pytest.param(
            {'SG_MCP_SITE_URL': 'https://url-user:url-password@example.shotgunstudio.com'},
            _SITE_URL_ERROR,
            id='userinfo',
        ),
        pytest.param(
            {'SG_MCP_TIMEZONE': 'Not/A_Zone'},
            'error: the SG_MCP_TIMEZONE setting must name an available IANA timezone\n',
            id='timezone',
        ),
        pytest.param(
            {'SG_MCP_DEFAULT_LIMIT': '1\x1b[31m'},
            "error: the SG_MCP_DEFAULT_LIMIT setting must be an integer, got '1\\x1b[31m'\n",
            id='integer',
        ),
        pytest.param(
            {'SG_MCP_AUDIT_PATH': '   '},
            'error: the SG_MCP_AUDIT_PATH setting must not be blank; '
            'unset it to use the default\n',
            id='blank-audit-path',
        ),
        pytest.param(
            {'SG_MCP_TIMEOUT_SECONDS': '0'},
            'error: the SG_MCP_TIMEOUT_SECONDS setting must be a positive integer, got 0\n',
            id='timeout',
        ),
        pytest.param(
            {'SG_MCP_DEFAULT_LIMIT': '300'},
            'error: limits must satisfy 1 <= SG_MCP_DEFAULT_LIMIT <= SG_MCP_HARD_LIMIT '
            '(SG treats limit 0 as unlimited); got 300 and 200\n',
            id='limits',
        ),
    ],
)
@pytest.mark.usefixtures('site_settings')
def test_invalid_settings_are_errors(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], message: str
) -> None:
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    assert invoke_cli([]) == (2, '', message)


@pytest.mark.usefixtures('site_settings')
@pytest.mark.parametrize(
    ('failure', 'windows'),
    [
        pytest.param(BrokenPipeError(errno.EPIPE, 'input read failed'), False, id='broken-pipe'),
        pytest.param(OSError(errno.EINVAL, 'input read failed'), True, id='windows-einval'),
    ],
)
def test_input_read_failure_propagates_through_session(
    monkeypatch: pytest.MonkeyPatch, failure: OSError, windows: bool
) -> None:
    def failed_read() -> str:
        raise failure

    stdin = io.StringIO()
    monkeypatch.setattr(stdin, 'readline', failed_read)
    monkeypatch.setattr(sys, 'stdin', stdin)
    monkeypatch.setattr(_downstream, '_WINDOWS', windows)
    with (
        io.TextIOWrapper(io.BytesIO(), encoding='utf-8') as stdout,
        contextlib.redirect_stdout(stdout),
        pytest.raises(ExceptionGroup) as caught,
    ):
        main([])

    rendered = ''.join(traceback.format_exception(caught.value))
    assert f'{type(failure).__name__}: {failure}' in rendered


@pytest.mark.usefixtures('site_settings')
def test_interrupt_before_serving_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(_config: Config) -> Never:
        raise KeyboardInterrupt

    monkeypatch.setattr(_cli, 'connect', interrupted)

    assert invoke_cli([]) == (130, '', '')


@pytest.mark.usefixtures('site_settings')
def test_interrupt_while_serving_exits_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(_server: Server) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_cli, '_serve_stdio', interrupted)

    assert invoke_cli([]) == (0, '', '')


@_SIGINT_UNAVAILABLE
def test_interrupt_while_serving_stops_with_input_open(tmp_path: Path) -> None:
    process, response = start_session(
        _message(_INITIALIZE).encode(), env=_process_environment(tmp_path)
    )
    assert 'serverInfo' in json.loads(response)['result']

    os.kill(process.pid, signal.SIGINT)
    result = wait_with_input_open(process, timeout=_STOP_TIMEOUT_SECONDS)

    assert (result.returncode, result.stderr) == (0, b''), process_transcript(result)


def test_missing_metadata_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, 'version', _metadata_missing)
    status, stdout, stderr = invoke_cli(['--version'])
    assert (status, stdout) == (2, '')
    assert stderr == 'error: installed distribution metadata for sg-mcp-server is unavailable\n'


@pytest.mark.usefixtures('site_settings')
def test_missing_metadata_stops_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, 'version', _metadata_missing)

    assert invoke_cli([]) == (
        2,
        '',
        'error: installed distribution metadata for sg-mcp-server is unavailable\n',
    )


@pytest.mark.parametrize('name', sorted(_SITE_SETTINGS))
@pytest.mark.usefixtures('site_settings')
def test_missing_required_setting_is_an_error(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delenv(name)

    assert invoke_cli([]) == (2, '', f'error: the {name} setting is required\n')


@pytest.mark.usefixtures('site_settings')
def test_missing_sg_client_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_client(_config: Config) -> Never:
        raise ModuleNotFoundError("No module named 'shotgun_api3'", name='shotgun_api3')

    monkeypatch.setattr(_cli, 'connect', missing_client)

    assert invoke_cli([]) == (2, '', _SG_CLIENT_MISSING)


def test_module_entry_point_runs_main() -> None:
    result = run_cli(['--help'], expect=0)
    assert result.stdout.startswith('usage: sg-mcp-server '), process_transcript(result)


@pytest.mark.usefixtures('site_settings')
def test_no_arguments_serves_until_input_ends() -> None:
    status, stdout, stderr = invoke_cli([], stdin=_message(_INITIALIZE))

    assert (status, stderr) == (0, '')
    [response] = [json.loads(line) for line in stdout.splitlines()]
    assert response['id'] == 1
    assert response['result']['serverInfo']['name'] == 'sg-mcp-server'


def test_normal_runs_do_not_read_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(name: str) -> str:
        raise AssertionError(name)

    monkeypatch.setattr(importlib.metadata, 'version', forbidden)
    assert invoke_cli(['--help'])[0] == 0


@pytest.mark.usefixtures('site_settings')
def test_other_missing_modules_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_dependency(_config: Config) -> Never:
        raise ModuleNotFoundError("No module named 'certifi'", name='certifi')

    monkeypatch.setattr(_cli, 'connect', missing_dependency)

    with pytest.raises(ModuleNotFoundError, match='certifi'):
        invoke_cli([])


@pytest.mark.usefixtures('site_settings')
@pytest.mark.parametrize(
    'failure',
    [
        pytest.param(
            ExceptionGroup(
                'serve', [BrokenPipeError(errno.EPIPE, 'pipe'), RuntimeError('handler failed')]
            ),
            id='mixed',
        ),
        pytest.param(ExceptionGroup('serve', [OSError(errno.EIO, 'I/O error')]), id='io-error'),
    ],
)
def test_other_serving_failures_propagate(
    monkeypatch: pytest.MonkeyPatch, failure: ExceptionGroup[Exception]
) -> None:
    async def failed(_server: Server) -> None:
        raise failure

    monkeypatch.setattr(_cli, '_serve_stdio', failed)

    with pytest.raises(ExceptionGroup) as caught:
        invoke_cli([])
    assert caught.value is failure


def test_package_root_exports_the_cli_entry_point() -> None:
    assert sg_mcp_server.__all__ == ['main']
    assert sg_mcp_server.main is main


def test_protocol_output_cleanup_preserves_other_serving_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = os.pipe()
    os.close(reader)
    failure = RuntimeError('handler failed')
    retained: list[anyio.AsyncFile[str]] = []

    def fail_with_pending_output() -> None:
        with _cli._protocol_output() as output:
            retained.append(output)
            asyncio.run(output.write('pending response\n'))
            raise failure

    with os.fdopen(writer, 'w', encoding='utf-8') as caller:
        monkeypatch.setattr(sys, 'stdout', caller)
        with pytest.raises(RuntimeError) as caught:
            fail_with_pending_output()

        assert caught.value is failure
        assert retained[0].wrapped.closed
        assert not caller.closed


def test_protocol_output_closes_its_writer_but_keeps_the_caller_stream_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / 'protocol'
    with path.open('w', encoding='latin-1') as caller:
        monkeypatch.setattr(sys, 'stdout', caller)
        with _cli._protocol_output() as output:
            asyncio.run(output.write('snowman: \u2603\n'))
        assert output.wrapped.closed
        assert not caller.closed
        caller.write('after serving\n')

    assert path.read_bytes().splitlines() == ['snowman: \u2603'.encode(), b'after serving']


def test_serving_isolates_ordinary_stdout_and_restores_it(tmp_path: Path) -> None:
    script = textwrap.dedent("""\
        import os
        import subprocess
        import sys
        from mcp.server import Server
        from sg_mcp_server._cli import _serve

        server = Server('server-\\u2603')
        initialize = server.create_initialization_options

        def noisy_initialize():
            print('python output', flush=True)
            os.write(1, b'descriptor output\\n')
            subprocess.run([sys.executable, '-c', "print('child output')"], check=True)
            return initialize()

        server.create_initialization_options = noisy_initialize
        status = _serve(server)
        print('restored output', flush=True)
        raise SystemExit(status)
        """)
    result = run_setup(
        [sys.executable, '-c', script],
        stdin=_message(_INITIALIZE).encode(),
        env=_process_environment(tmp_path) | {'PYTHONIOENCODING': 'latin-1'},
    )

    response, restored = result.stdout.splitlines()
    assert json.loads(response)['result']['serverInfo']['name'] == 'server-\u2603'
    assert restored == b'restored output', process_transcript(result)
    assert result.stderr.splitlines() == [
        b'python output',
        b'descriptor output',
        b'child output',
    ], process_transcript(result)


def test_settings_are_checked_before_the_sg_client_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_connect(_config: Config) -> Never:
        message = 'the SG client must be built only after the settings are valid'
        raise AssertionError(message)

    monkeypatch.setattr(_cli, 'connect', forbidden_connect)
    monkeypatch.setenv('SG_MCP_SITE_URL', 'http://example.shotgrid.autodesk.com')
    monkeypatch.setenv('SG_MCP_SCRIPT_NAME', 'sg-mcp-server')
    monkeypatch.setenv('SG_MCP_API_KEY', 'api-secret')

    assert invoke_cli([]) == (2, '', _SITE_URL_ERROR)


def test_unavailable_stdout_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    # Restore stdout before capsys finalizes, so its capture stream is not reinstated.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, 'stdout', None)
        status = main(['--help'])

    assert (status, capsys.readouterr().err) == (2, 'error: standard output is unavailable\n')


@pytest.mark.usefixtures('site_settings')
def test_version_does_not_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(_server: Server) -> None:
        message = '--version must not serve'
        raise AssertionError(message)

    monkeypatch.setattr(_cli, '_serve_stdio', forbidden)
    monkeypatch.setattr(importlib.metadata, 'version', _fixed_version)

    assert invoke_cli(['--version']) == (0, 'sg-mcp-server 9.8.7\n', '')


def test_version_prints_the_installed_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, 'version', _fixed_version)
    assert invoke_cli(['--version']) == (0, 'sg-mcp-server 9.8.7\n', '')
