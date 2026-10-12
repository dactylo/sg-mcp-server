"""Parse the sg-mcp-server command line and serve MCP over standard streams."""

import argparse
import asyncio
import importlib.metadata
import io
import os
import re
import sys
import threading
from collections.abc import AsyncIterator, Generator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from typing import cast

import anyio
from mcp import stdio_server
from mcp.os.win32.utilities import rebind_std_handle_to_fd
from mcp.server import Server

from sg_mcp_server import _tools
from sg_mcp_server._config import Config, ConfigError
from sg_mcp_server._downstream import is_downstream_closure, silence_descriptor, write_stdout
from sg_mcp_server._providers import connect
from sg_mcp_server._server import build_server

_CONTROL_CHARACTERS = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_DESCRIPTION = 'Serve read-only SG tools to an MCP client over standard input and output.'
_EPILOG = """\
An MCP client normally starts this command, which serves until the client
disconnects. Set these environment variables in the client's configuration:

  SG_MCP_SITE_URL            HTTPS site address (required)
  SG_MCP_SCRIPT_NAME         script name used for all requests (required)
  SG_MCP_API_KEY             API key for that script (required)
  SG_MCP_TIMEZONE            IANA time zone for dates (default: UTC)
  SG_MCP_AUDIT_PATH          security audit file
                             (default: ~/.sg-mcp-server/audit.jsonl)
  SG_MCP_DIAGNOSTICS_PATH    latency diagnostics file (default: off)
  SG_MCP_DEFAULT_LIMIT       row limit when a query omits one (default: 50)
  SG_MCP_HARD_LIMIT          maximum row limit (default: 200)
  SG_MCP_SCHEMA_TTL_SECONDS  seconds to reuse the cached schema (default: 900)
  SG_MCP_TIMEOUT_SECONDS     seconds to wait on the SG site (default: 60)

See "Settings" in the usage guide, docs/usage.md, for details."""
_METADATA_MISSING = 'installed distribution metadata for sg-mcp-server is unavailable'
_SG_CLIENT_MODULE = 'shotgun_api3'


class _ProtocolInputError(Exception):
    """A protocol input failure that must not be mistaken for output closure."""


class _ProtocolInput(anyio.AsyncFile[str]):
    """Client messages read on daemon threads, so a stop never waits for more input."""

    def __init__(self) -> None:
        """Read standard input through a private UTF-8 reader on its descriptor."""
        try:
            descriptor = sys.stdin.fileno()
        except (OSError, ValueError):
            # A replaced stream without a descriptor, such as an in-memory one, is read as it is.
            stream = sys.stdin
        else:
            # closefd=False leaves sys.stdin open when this reader closes, and without a buffer
            # the read that a stop abandons holds no lock.
            raw = io.FileIO(descriptor, closefd=False)
            stream = io.TextIOWrapper(raw, encoding='utf-8', errors='replace')
        super().__init__(stream)

    async def __aiter__(self) -> AsyncIterator[str]:
        """Yield each line until the client ends its input."""
        while line := await self.readline():
            yield line

    async def readline(self) -> str:
        """Return the next line, or an empty string once the client ends its input."""
        loop = asyncio.get_running_loop()
        line: asyncio.Future[str] = loop.create_future()

        def settle(result: str | OSError | ValueError) -> None:
            if line.done():  # the session stopped while this read was waiting
                return
            if isinstance(result, str):
                line.set_result(result)
            else:
                line.set_exception(result)

        def read() -> None:
            result: str | OSError | ValueError
            try:
                result = self.wrapped.readline()
            except (OSError, ValueError) as error:
                result = error
            with suppress(RuntimeError):  # the event loop closed after the session stopped
                loop.call_soon_threadsafe(settle, result)

        threading.Thread(target=read, name='MCP input', daemon=True).start()
        try:
            return await line
        except OSError as error:
            raise _ProtocolInputError('standard input read failed') from error


def _build_parser() -> argparse.ArgumentParser:
    """Build the parser for the help and version options."""
    parser = argparse.ArgumentParser(
        prog='sg-mcp-server',
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument('--version', action='store_true', help='show the version and exit')
    return parser


def _client_disconnected(error: BaseException) -> bool:
    """Report whether serving stopped only because the client closed standard output."""
    if isinstance(error, BaseExceptionGroup):
        group = cast('BaseExceptionGroup[BaseException]', error)
        return all(_client_disconnected(item) for item in group.exceptions)
    return isinstance(error, OSError) and is_downstream_closure(error)


@contextmanager
def _divert_stdout(protocol_descriptor: int) -> Generator[None, None, None]:
    """Keep ordinary writes and child output off the protocol until serving stops."""
    try:
        diversion = os.dup(2)
    except OSError:
        diversion = os.open(os.devnull, os.O_WRONLY)
    try:
        try:
            os.dup2(diversion, 1)
            with suppress(OSError):
                rebind_std_handle_to_fd(1)
            yield
        finally:
            os.dup2(protocol_descriptor, 1)
            with suppress(OSError):
                rebind_std_handle_to_fd(1)
    finally:
        os.close(diversion)


@contextmanager
def _protocol_output() -> Generator[anyio.AsyncFile[str], None, None]:
    """Own the UTF-8 protocol writer without closing the caller's output stream."""
    try:
        descriptor = sys.stdout.fileno()
    except (OSError, ValueError):
        # Replaced streams retain their buffer and do not need descriptor diversion.
        output = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        try:
            yield anyio.wrap_file(output)
        finally:
            output.detach()
        return

    # A missing standard stream must not become our protocol descriptor or diversion.
    with ExitStack() as reserved:
        while (duplicate := os.dup(descriptor)) in (0, 1, 2):
            reserved.callback(os.close, duplicate)
    with io.FileIO(duplicate, 'w') as raw:
        output = io.TextIOWrapper(io.BufferedWriter(raw), encoding='utf-8')
        try:
            if descriptor == 1:
                with _divert_stdout(duplicate):
                    yield anyio.wrap_file(output)
            else:
                yield anyio.wrap_file(output)
        finally:
            try:
                # Closing the owned buffer also discards bytes from a failed flush. Leaving
                # it for garbage collection retries that flush outside our error handler.
                output.close()
            except OSError as error:
                if not is_downstream_closure(error):
                    raise


def _report_error(message: str) -> None:
    """Print one terminal-safe error line to standard error."""
    visible = _CONTROL_CHARACTERS.sub(lambda match: f'\\x{ord(match.group()):02x}', message)
    print(f'error: {visible}', file=sys.stderr)


def _render_version() -> tuple[int, str]:
    """Render the installed distribution version without other work."""
    try:
        version = importlib.metadata.version('sg-mcp-server')
    except importlib.metadata.PackageNotFoundError:
        _report_error(_METADATA_MISSING)
        return 2, ''
    return 0, f'sg-mcp-server {version}\n'


async def _serve_stdio(server: Server) -> None:
    """Serve one MCP session over the process's standard input and output."""
    with _protocol_output() as stdout:
        async with stdio_server(stdin=_ProtocolInput(), stdout=stdout) as (
            read_stream,
            write_stream,
        ):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def _serve(server: Server) -> int:
    """Serve one client, treating a disconnect or an interrupt as a normal stop."""
    try:
        asyncio.run(_serve_stdio(server))
    except KeyboardInterrupt:
        return 0
    except (OSError, BaseExceptionGroup) as error:
        if not _client_disconnected(error):
            raise
        # The transport restored the closed pipe; keep the final flush off it.
        silence_descriptor(sys.stdout)
    return 0


def _run(arguments: list[str]) -> tuple[int, str]:
    """Parse arguments, then render the version or serve the tools."""
    args = _build_parser().parse_args(arguments)
    if args.version:
        return _render_version()
    try:
        config = Config.from_env(os.environ)
        server = build_server(config, connect(config), _tools.surface())
    except ConfigError as error:
        _report_error(str(error))
        return 2, ''
    except importlib.metadata.PackageNotFoundError:
        # A subclass of ModuleNotFoundError, so it is matched first.
        _report_error(_METADATA_MISSING)
        return 2, ''
    except ModuleNotFoundError as error:
        if error.name != _SG_CLIENT_MODULE:
            raise
        _report_error(
            'the SG client is not installed; reinstall sg-mcp-server with the official extra'
        )
        return 2, ''
    return _serve(server), ''


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sg-mcp-server command-line interface.

    With no arguments, serve the read-only tools to one MCP client over
    standard input and output. `--version` writes the installed version and
    returns 0. `--help` and invalid arguments end in `SystemExit` from argument
    parsing. A client disconnect, the end of input, and an interrupt while
    serving each stop the server with status 0, once calls in progress finish.
    If the reader closes standard output, unwritten output is discarded while
    the status still reports the result.

    Args:
        argv: Arguments to parse, or `None` to use `sys.argv`.

    Returns:
        The process exit status.
    """
    if sys.stdout is None:
        # Started with standard output closed: nothing can be delivered.
        _report_error('standard output is unavailable')
        return 2
    output = ''
    try:
        status, output = _run(sys.argv[1:] if argv is None else list(argv))
    except KeyboardInterrupt:
        status = 130
    finally:
        # Also flushes help or usage text that argparse wrote before raising SystemExit.
        write_stdout(output)
    return status
