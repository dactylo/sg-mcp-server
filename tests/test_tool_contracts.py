from typing import cast

from sg_mcp_server._engine import _relative_dates as relative_dates
from sg_mcp_server._engine import _schema as engine_schema
from sg_mcp_server._engine import _validation as validation
from sg_mcp_server._schema_adapter import SCHEMA_PAGE_SIZE
from sg_mcp_server._tool_contracts import TOOLS


def _enums(value: object) -> list[list[object]]:
    if isinstance(value, dict):
        mapping = cast('dict[str, object]', value)
        own = [cast('list[object]', mapping['enum'])] if 'enum' in mapping else []
        return own + [enum for item in mapping.values() for enum in _enums(item)]
    if isinstance(value, list):
        return [enum for item in cast('list[object]', value) for enum in _enums(item)]
    return []


def test_schema_tool_describes_exact_bounded_search() -> None:
    [schema] = (tool for tool in TOOLS if tool.name == 'sg_schema')

    assert schema.description is not None
    assert 'case-insensitive substring' in schema.description
    assert 'field names or labels' in schema.description
    assert 'top 40' in schema.description
    assert 'fuzzy' not in schema.description
    assert 'enumerate the rest' not in schema.description


def test_schema_tool_limits_match_the_engine() -> None:
    [schema] = (tool for tool in TOOLS if tool.name == 'sg_schema')

    assert schema.description is not None
    assert f'top {engine_schema._MAX_SEARCH_RESULTS}' in schema.description
    assert f'Pages of {SCHEMA_PAGE_SIZE}' in schema.description


def test_served_query_vocabulary_matches_the_engine() -> None:
    [query] = (tool for tool in TOOLS if tool.name == 'sg_query')
    enums = _enums(query.input_schema)

    assert list(relative_dates.SPAN_OPERATORS) in enums
    assert list(relative_dates.CALENDAR_OPERATORS) in enums
    assert list(relative_dates.RELATIVE_UNITS) in enums
    assert sorted(validation._SUMMARY_TYPES) in enums
