from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from mcp import types

from sg_mcp_server._surface import ToolSurface, base_argument_error


def _handler(runtime: object, payload: dict[str, Any], context: object) -> types.CallToolResult:
    del runtime, payload, context
    return types.CallToolResult(content=[types.TextContent(type='text', text='ok')])


def _tool(name: str) -> types.Tool:
    return types.Tool(name=name, description=name, input_schema={'type': 'object'})


def test_base_argument_error_prefixes_the_schema_message() -> None:
    error = jsonschema.ValidationError('is not of type integer')

    assert base_argument_error(error) == 'invalid arguments: is not of type integer'


def test_surface_requires_handlers_for_exactly_its_tools() -> None:
    surface = ToolSurface(
        tools=(_tool('one'),),
        handlers={'one': _handler},
        argument_error=lambda _tool, _arguments, error: base_argument_error(error),
    )

    assert tuple(surface.handlers) == ('one',)
    with pytest.raises(ValueError, match='handlers'):
        ToolSurface(tools=(_tool('one'),), handlers={}, argument_error=surface.argument_error)
    with pytest.raises(ValueError, match='handlers'):
        ToolSurface(
            tools=(_tool('one'),),
            handlers={'one': _handler, 'two': _handler},
            argument_error=surface.argument_error,
        )
