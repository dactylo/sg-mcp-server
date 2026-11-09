import asyncio
import copy
import dataclasses
import datetime
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, Unpack, cast

from mcp import Client, MCPError, types
from mcp.server import Server
from support import FixtureConnection

from sg_mcp_server import _tools
from sg_mcp_server._config import Config
from sg_mcp_server._providers import RawSchema
from sg_mcp_server._rendering import _UNTRUSTED_CLOSE, _UNTRUSTED_OPEN
from sg_mcp_server._server import build_server
from sg_mcp_server._surface import ToolSurface


class _ConfigOverrides(TypedDict, total=False):
    audit_path: Path
    schema_ttl_seconds: int
    timezone: str


class AdversarialLabelConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection, label: str) -> None:
        self.site = source.site
        self._label = label

    def schema_read(self) -> RawSchema:
        fields = copy.deepcopy(dict(super().schema_read()))
        raw_shot = fields.get('Shot')
        assert isinstance(raw_shot, dict)
        shot = cast('dict[str, object]', raw_shot)
        raw_code = shot.get('code')
        assert isinstance(raw_code, dict)
        code = cast('dict[str, object]', raw_code)
        raw_name = code.get('name')
        assert isinstance(raw_name, dict)
        name = cast('dict[str, object]', raw_name)
        name['value'] = self._label
        return fields


def build_test_server(
    connection: FixtureConnection,
    tmp_path: Path,
    *,
    diagnostics: bool = False,
    surface: ToolSurface | None = None,
    clock: Callable[[], datetime.datetime] | None = None,
    **overrides: Unpack[_ConfigOverrides],
) -> Server:
    config = Config(
        site_url='https://fixture.shotgunstudio.com',
        script_name='s',
        api_key='k',
        audit_path=tmp_path / 'audit.jsonl',
        diagnostics_path=tmp_path / 'diagnostics.jsonl' if diagnostics else None,
    )
    return build_server(
        dataclasses.replace(config, **overrides),
        connection,
        _tools.surface() if surface is None else surface,
        clock=clock,
    )


def call(server: Server, name: str, payload: dict[str, Any]) -> types.CallToolResult:

    async def run() -> types.CallToolResult:
        error: MCPError | None = None
        async with Client(server) as client:
            try:
                return await client.call_tool(name, payload)
            except MCPError as caught:
                error = caught
        assert error is not None
        raise error

    return asyncio.run(run())


def listed_tools(server: Server) -> list[types.Tool]:

    async def run() -> list[types.Tool]:
        async with Client(server) as client:
            return (await client.list_tools()).tools

    return asyncio.run(run())


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def server_info(server: Server) -> types.Implementation | None:

    async def run() -> types.Implementation | None:
        async with Client(server) as client:
            return client.server_info

    return asyncio.run(run())


def server_instructions(server: Server) -> str | None:

    async def run() -> str | None:
        async with Client(server) as client:
            return client.instructions

    return asyncio.run(run())


def text_of(result: types.CallToolResult) -> str:
    return '\n'.join(
        block.text for block in result.content if isinstance(block, types.TextContent)
    )


def assert_error_is_fenced(result: types.CallToolResult) -> dict[str, Any]:
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content['contains_untrusted_site_text'] is True
    lines = text_of(result).splitlines()
    assert lines.count(_UNTRUSTED_OPEN) == 1
    assert lines.count(_UNTRUSTED_CLOSE) == 1
    assert lines[0] == _UNTRUSTED_OPEN
    assert lines[-1] == _UNTRUSTED_CLOSE
    body = json.loads(json.loads(lines[1]))
    assert body == result.structured_content
    return body
