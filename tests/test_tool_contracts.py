import dataclasses
import re
from typing import cast

from sg_mcp_server import _schema_adapter as schema_adapter
from sg_mcp_server import _tools as tools
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


def _operator_enums(value: object) -> list[list[object]]:
    if isinstance(value, dict):
        mapping = cast('dict[str, object]', value)
        own: list[list[object]] = []
        condition = mapping.get('prefixItems')
        if isinstance(condition, list):
            operator = cast('list[dict[str, object]]', condition)[1]
            own.append(cast('list[object]', operator['enum']))
        properties = mapping.get('properties')
        if isinstance(properties, dict) and 'op' in properties:
            operator = cast('dict[str, dict[str, object]]', properties)['op']
            own.append(cast('list[object]', operator['enum']))
        return own + [enum for item in mapping.values() for enum in _operator_enums(item)]
    if isinstance(value, list):
        return [enum for item in cast('list[object]', value) for enum in _operator_enums(item)]
    return []


def test_advertised_tool_defaults_match_the_defaults_the_server_applies() -> None:
    argument_types = {
        'sg_get': tools._GetArguments,
        'sg_query': tools._QueryArguments,
        'sg_schema': schema_adapter.SchemaArguments,
    }
    assert {tool.name for tool in TOOLS} == set(argument_types)
    for tool in TOOLS:
        properties = cast('dict[str, dict[str, object]]', tool.input_schema['properties'])
        fields = {field.name: field for field in dataclasses.fields(argument_types[tool.name])}
        assert set(properties) == set(fields), tool.name
        for name, schema in properties.items():
            if 'default' not in schema:
                continue
            field = fields[name]
            applied = (
                field.default_factory()
                if field.default_factory is not dataclasses.MISSING
                else field.default
            )
            assert applied == schema['default'], f'{tool.name}.{name}'


def test_condition_shapes_hint_names_every_served_filter_enum() -> None:
    [query] = (tool for tool in TOOLS if tool.name == 'sg_query')
    filters = cast('dict[str, object]', query.input_schema['properties'])['filters']

    for enum in _enums(filters):
        for value in enum:
            assert isinstance(value, str)
            assert re.search(rf'\b{re.escape(value)}\b', tools._CONDITION_SHAPES_HINT), value


def test_schema_tool_describes_exact_bounded_search() -> None:
    [schema] = (tool for tool in TOOLS if tool.name == 'sg_schema')

    assert schema.description is not None
    assert 'case-insensitive substring' in schema.description
    assert 'field names or labels' in schema.description
    assert 'search by 40' in schema.description
    assert 'fuzzy' not in schema.description
    assert 'enumerate the rest' not in schema.description


def test_schema_tool_limits_match_the_engine() -> None:
    [schema] = (tool for tool in TOOLS if tool.name == 'sg_schema')

    assert schema.description is not None
    assert (
        f'describe by {SCHEMA_PAGE_SIZE} and search by {engine_schema.SEARCH_PAGE_SIZE}'
        in schema.description
    )


def test_served_query_vocabulary_matches_the_engine() -> None:
    [query] = (tool for tool in TOOLS if tool.name == 'sg_query')
    enums = _enums(query.input_schema)
    served_operators = {
        operator for enum in _operator_enums(query.input_schema) for operator in enum
    }

    assert served_operators == set().union(*validation._OPERATORS_BY_TYPE.values())
    for vocabulary in (
        relative_dates.SPAN_OPERATORS,
        relative_dates.CALENDAR_OPERATORS,
        relative_dates.RELATIVE_UNITS,
    ):
        occurrences = [enum for enum in enums if set(enum) & set(vocabulary)]
        assert occurrences
        assert all(set(enum) == set(vocabulary) for enum in occurrences)
    assert any(set(enum) == validation._SUMMARY_TYPES for enum in enums)
