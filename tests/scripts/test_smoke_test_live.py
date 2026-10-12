from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING, Any, Never, cast

import pytest
from mcp import ClientSession, MCPError, types

from scripts import smoke_test_live

if TYPE_CHECKING:
    from scripts.smoke_test_live import _SmokeSummary

_EXPECTED_GET_ARGUMENTS = {
    'entity_type': 'Project',
    'entity_id': 41,
    'fields': ['name'],
}
_EXPECTED_QUERY_ARGUMENTS = {
    'entity_type': 'Project',
    'filters': [],
    'fields': ['name'],
    'order': [{'field_name': 'id', 'direction': 'asc'}],
    'limit': 1,
}


class _ProbeSession:
    def __init__(self, *, include_project: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._include_project = include_project

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        assert arguments is not None
        self.calls.append((name, arguments))
        if len(self.calls) == 1:
            assert (name, arguments) == ('sg_schema', {'op': 'entities'})
            return types.CallToolResult(
                content=[],
                structured_content={'entity_types': [f'Entity{index}' for index in range(92)]},
            )
        if len(self.calls) == 2:
            assert (name, arguments) == ('sg_query', _EXPECTED_QUERY_ARGUMENTS)
            records = [{'type': 'Project', 'id': 41, 'name': 'private'}]
            return types.CallToolResult(
                content=[],
                structured_content={'records': records if self._include_project else []},
            )
        assert self._include_project
        assert len(self.calls) == 3
        assert (name, arguments) == ('sg_get', _EXPECTED_GET_ARGUMENTS)
        return types.CallToolResult(
            content=[],
            structured_content={'records': [{'type': 'Project', 'id': 41, 'name': 'private'}]},
        )

    async def initialize(self) -> types.InitializeResult:
        return types.InitializeResult(
            protocol_version='2025-11-25',
            capabilities=types.ServerCapabilities(),
            server_info=types.Implementation(name='sg-mcp-server', version='0.0.1'),
        )

    async def list_tools(self) -> types.ListToolsResult:
        tools = [
            types.Tool(name=name, input_schema={'type': 'object'})
            for name in ('sg_schema', 'sg_get', 'sg_query')
        ]
        return types.ListToolsResult(tools=tools)


class _WrongToolSession(_ProbeSession):
    def __init__(self, tool_names: tuple[str, ...]) -> None:
        super().__init__()
        self._tool_names = tool_names

    async def list_tools(self) -> types.ListToolsResult:
        tools = [
            types.Tool(name=name, input_schema={'type': 'object'}) for name in self._tool_names
        ]
        return types.ListToolsResult(tools=tools)


def _probe(session: _ProbeSession) -> _SmokeSummary:
    return asyncio.run(smoke_test_live._probe_session(cast('ClientSession', session)))


def test_main_handles_grouped_mcp_failure_without_private_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail() -> Never:
        raise ExceptionGroup(
            'stdio transport failed',
            [MCPError(code=-32603, message='private server details')],
        )

    monkeypatch.setattr(smoke_test_live, '_run_live', fail)

    assert smoke_test_live._main() == 1
    assert capsys.readouterr().err == 'live MCP smoke failed: MCP request failed\n'


def test_probe_session_calls_all_available_read_tools_and_returns_safe_summary() -> None:
    session = _ProbeSession()

    summary = _probe(session)

    assert tuple(field.name for field in dataclasses.fields(summary)) == (
        '_server_name',
        '_tool_names',
        '_entity_type_count',
        '_project_row_count',
        '_query_error',
        '_get_error',
    )
    assert summary._server_name == 'sg-mcp-server'
    assert summary._tool_names == ('sg_get', 'sg_query', 'sg_schema')
    assert summary._entity_type_count == 92
    assert summary._project_row_count == 1
    assert summary._query_error is False
    assert summary._get_error is False
    assert session.calls == [
        ('sg_schema', {'op': 'entities'}),
        ('sg_query', _EXPECTED_QUERY_ARGUMENTS),
        ('sg_get', _EXPECTED_GET_ARGUMENTS),
    ]


@pytest.mark.parametrize(
    'tool_names',
    [
        ('sg_get', 'sg_query'),
        ('sg_get', 'sg_query', 'sg_schema', 'sg_write'),
    ],
)
def test_probe_session_requires_exact_read_tool_set(tool_names: tuple[str, ...]) -> None:
    session = _WrongToolSession(tool_names)

    with pytest.raises(RuntimeError, match='unexpected MCP tool set'):
        _probe(session)

    assert session.calls == []


def test_probe_session_skips_get_when_no_project_exists() -> None:
    session = _ProbeSession(include_project=False)

    summary = _probe(session)

    assert summary._project_row_count == 0
    assert summary._get_error is None
    assert session.calls == [
        ('sg_schema', {'op': 'entities'}),
        ('sg_query', _EXPECTED_QUERY_ARGUMENTS),
    ]


def test_required_environment_reports_all_missing_names_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ('SG_MCP_API_KEY', 'SG_MCP_SCRIPT_NAME', 'SG_MCP_SITE_URL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('UNRELATED_SECRET', 'do-not-reveal')

    with pytest.raises(RuntimeError) as caught:
        smoke_test_live._required_environment()

    assert str(caught.value) == (
        'missing required environment variables: '
        'SG_MCP_API_KEY, SG_MCP_SCRIPT_NAME, SG_MCP_SITE_URL'
    )
    assert 'do-not-reveal' not in str(caught.value)
