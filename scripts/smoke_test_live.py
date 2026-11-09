"""Run a safe, read-only smoke test against the live MCP server.

Run `python scripts/smoke_test_live.py` from the repository root with
`SG_MCP_SITE_URL`, `SG_MCP_SCRIPT_NAME`, and `SG_MCP_API_KEY` set.

The script manages the current checkout's `.venv/bin/sg-mcp-server` subprocess
over stdio. It checks the tool catalog, schema enumeration, a bounded Project
query, and a get operation when a Project is available.

Standard output contains only the approved non-sensitive summary. The command
exits with zero on success or one for a handled configuration, process,
protocol, or tool failure.
"""

import asyncio
import dataclasses
import os
import sys
from pathlib import Path
from typing import cast

from mcp import ClientSession, MCPError, StdioServerParameters, types
from mcp.client.stdio import stdio_client

_EXPECTED_TOOLS = frozenset({'sg_get', 'sg_query', 'sg_schema'})
_REQUIRED_ENVIRONMENT = (
    'SG_MCP_API_KEY',
    'SG_MCP_SCRIPT_NAME',
    'SG_MCP_SITE_URL',
)


class _MalformedResponseError(RuntimeError):
    """Report an MCP response that cannot support the safe summary."""


class _SmokeConfigurationError(RuntimeError):
    """Report missing configuration without exposing environment values."""


@dataclasses.dataclass(frozen=True)
class _SmokeSummary:
    """Contain only non-sensitive live smoke measurements."""

    server_name: str
    tool_names: tuple[str, ...]
    entity_type_count: int
    project_row_count: int
    query_error: bool
    get_error: bool | None


def _failure_message(error: BaseException) -> str | None:
    """Return a safe message for expected direct or grouped failures."""
    if isinstance(error, BaseExceptionGroup):
        group = cast('BaseExceptionGroup[BaseException]', error)
        messages = [_failure_message(nested) for nested in group.exceptions]
        if any(message is None for message in messages):
            message = None
        elif 'live MCP smoke failed: MCP request failed' in messages:
            message = 'live MCP smoke failed: MCP request failed'
        else:
            message = next(message for message in messages if message is not None)
    elif isinstance(error, (_MalformedResponseError, _SmokeConfigurationError)):
        message = f'live MCP smoke failed: {error}'
    elif isinstance(error, MCPError):
        message = 'live MCP smoke failed: MCP request failed'
    elif isinstance(error, OSError):
        message = 'live MCP smoke failed: server process could not start'
    else:
        message = None
    return message


def _print_summary(summary: _SmokeSummary) -> None:
    """Print only the approved non-sensitive summary fields."""
    print(f'server_name: {summary.server_name}')
    print(f'tool_names: {", ".join(summary.tool_names)}')
    print(f'entity_type_count: {summary.entity_type_count}')
    print(f'project_row_count: {summary.project_row_count}')
    print(f'query_error: {summary.query_error}')
    print(f'get_error: {summary.get_error}')


def _required_environment() -> dict[str, str]:
    """Return a copied environment or report every missing variable name."""
    missing = sorted(name for name in _REQUIRED_ENVIRONMENT if not os.environ.get(name))
    if missing:
        names = ', '.join(missing)
        raise _SmokeConfigurationError(f'missing required environment variables: {names}')
    return dict(os.environ)


def _server_parameters() -> StdioServerParameters:
    """Build stdio parameters from the script's repository location."""
    repository_root = Path(__file__).resolve().parents[1]
    return StdioServerParameters(
        command=str(repository_root / '.venv' / 'bin' / 'sg-mcp-server'),
        env=_required_environment(),
        cwd=repository_root,
    )


def _structured_mapping(result: types.CallToolResult, tool_name: str) -> dict[str, object]:
    """Return one tool's structured mapping or reject its response shape."""
    structured = result.structured_content
    if not isinstance(structured, dict):
        raise _MalformedResponseError(f'{tool_name} returned malformed structured content')
    return cast('dict[str, object]', structured)


def _entity_type_count(result: object) -> int:
    """Return the site's total entity-type count from a successful schema probe."""
    if not isinstance(result, types.CallToolResult) or result.is_error:
        raise _MalformedResponseError('sg_schema did not return a successful tool result')
    structured = _structured_mapping(result, 'sg_schema')
    candidate_entity_types = structured.get('entity_types')
    if not isinstance(candidate_entity_types, list):
        raise _MalformedResponseError('sg_schema returned malformed entity types')
    entity_types = cast('list[object]', candidate_entity_types)
    if not all(isinstance(entity_type, str) for entity_type in entity_types):
        raise _MalformedResponseError('sg_schema returned malformed entity types')
    total = structured.get('total')
    if type(total) is not int or total < len(entity_types):
        raise _MalformedResponseError('sg_schema returned malformed entity types')
    return total


async def _get_error(session: ClientSession, records: list[dict[str, object]]) -> bool | None:
    """Probe `sg_get` for the first Project, when one exists."""
    if not records:
        return None
    project_id = records[0].get('id')
    if type(project_id) is not int:
        raise _MalformedResponseError('sg_query returned a Project without an integer id')
    result = await session.call_tool(
        'sg_get',
        {'entity_type': 'Project', 'entity_id': project_id, 'fields': ['name']},
    )
    if not result.is_error:
        _structured_mapping(result, 'sg_get')
    return result.is_error


def _project_records(result: object) -> tuple[list[dict[str, object]], bool]:
    """Return Project records and the query error flag."""
    if not isinstance(result, types.CallToolResult):
        raise _MalformedResponseError('sg_query did not return a tool result')
    if result.is_error:
        return [], True
    candidate_records = _structured_mapping(result, 'sg_query').get('records')
    if not isinstance(candidate_records, list):
        raise _MalformedResponseError('sg_query returned malformed records')
    records = cast('list[object]', candidate_records)
    if not all(isinstance(record, dict) for record in records):
        raise _MalformedResponseError('sg_query returned malformed records')
    return cast('list[dict[str, object]]', records), False


async def _probe_session(session: ClientSession) -> _SmokeSummary:
    """Initialize one session and probe all available read tools."""
    initialized = await session.initialize()
    listed = await session.list_tools()
    tool_names = tuple(sorted(tool.name for tool in listed.tools))
    if tool_names != tuple(sorted(_EXPECTED_TOOLS)):
        raise _MalformedResponseError('unexpected MCP tool set')

    schema_result = await session.call_tool('sg_schema', {'op': 'entities'})
    entity_type_count = _entity_type_count(schema_result)

    query_result = await session.call_tool(
        'sg_query',
        {
            'entity_type': 'Project',
            'filters': [],
            'fields': ['name'],
            'order': [{'field_name': 'id', 'direction': 'asc'}],
            'limit': 1,
        },
    )
    records, query_error = _project_records(query_result)
    get_error = await _get_error(session, records)

    return _SmokeSummary(
        server_name=initialized.server_info.name,
        tool_names=tool_names,
        entity_type_count=entity_type_count,
        project_row_count=len(records),
        query_error=query_error,
        get_error=get_error,
    )


async def _run_live() -> _SmokeSummary:
    """Launch the repository entry point and probe its stdio MCP session."""
    with await asyncio.to_thread(Path(os.devnull).open, 'w', encoding='utf-8') as error_log:
        async with (
            stdio_client(_server_parameters(), errlog=error_log) as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            return await _probe_session(session)


def _main() -> int:
    """Run the live smoke test and return its process exit status."""
    try:
        summary = asyncio.run(_run_live())
    except (
        BaseExceptionGroup,
        MCPError,
        OSError,
        _MalformedResponseError,
        _SmokeConfigurationError,
    ) as error:
        message = _failure_message(error)
        if message is None:
            raise
        print(message, file=sys.stderr)
        return 1

    _print_summary(summary)
    if summary.query_error or summary.get_error is True:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
