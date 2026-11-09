"""Define the served read-only tool surface and its handlers."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from sg_mcp_server import _engine, _schema_adapter
from sg_mcp_server._diagnostics import DiagnosticContext, ObservedSchemaCache
from sg_mcp_server._errors import bare_condition_hint
from sg_mcp_server._surface import ToolBodyResult, ToolRuntime, ToolSurface, base_argument_error
from sg_mcp_server._tool_contracts import TOOLS

_CONDITION_SHAPES_HINT = (
    '; each filter condition must be [field, operator, value] with operator one of contains, '
    'ends_with, greater_than, is, is_not, less_than, name_contains, name_ends_with, '
    'name_not_contains, name_starts_with, not_contains, starts_with, type_is or type_is_not; '
    '[field, "in" or "not_in", [values]]; [field, "between" or "not_between", low, high]; '
    '[field, relative, count, unit] with relative one of "in_last", "in_next", "not_in_last" '
    'or "not_in_next" and unit one of HOUR, DAY, WEEK, MONTH or YEAR, or the same as '
    '{"field", "op", "count", "unit"}; [field, calendar, offset] with calendar one of '
    '"in_calendar_day", "in_calendar_week", "in_calendar_month" or "in_calendar_year"; '
    'or a group {"filter_operator": "all" or "any", "filters": [...]}'
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import jsonschema
    from mcp import types


@dataclasses.dataclass(frozen=True)
class _GetArguments:
    """Validated arguments for `sg_get`."""

    entity_type: str
    entity_id: int
    fields: list[str]

    def __post_init__(self) -> None:
        """Normalize JSON Schema integer values accepted as integral floats."""
        object.__setattr__(self, 'entity_id', int(self.entity_id))


@dataclasses.dataclass(frozen=True)
class _QueryArguments:
    """Validated arguments for `sg_query`."""

    entity_type: str
    filters: list[object] = dataclasses.field(default_factory=list[object])
    fields: list[str] = dataclasses.field(default_factory=list[str])
    order: list[dict[str, str]] = dataclasses.field(default_factory=list[dict[str, str]])
    summarize: dict[str, object] = dataclasses.field(default_factory=dict[str, object])
    filter_operator: str = ''
    limit: int = 0
    retired_only: bool = False

    def __post_init__(self) -> None:
        """Normalize JSON Schema integer values accepted as integral floats."""
        object.__setattr__(self, 'limit', int(self.limit))


def _condition_shapes_hint(error: jsonschema.ValidationError) -> str | None:
    """Describe accepted filter shapes for a rejected condition or group."""
    path = error.absolute_path
    if len(path) > 1 and path[0] == 'filters':
        return _CONDITION_SHAPES_HINT
    return None


def _handle_schema(
    runtime: ToolRuntime, payload: dict[str, Any], context: DiagnosticContext
) -> types.CallToolResult:
    """Explore the live site schema (client-facing text lives in `TOOLS`)."""
    arguments = _schema_adapter.SchemaArguments(**payload)
    complete_payload = dataclasses.asdict(arguments)
    cache = ObservedSchemaCache(runtime.schema_cache, context)
    return runtime.audited(
        complete_payload,
        context,
        lambda: _schema_adapter.run_schema(cache, arguments, context),
    )


def _run_get(
    runtime: ToolRuntime,
    arguments: _GetArguments,
    context: DiagnosticContext,
) -> ToolBodyResult:
    """Validate, execute, and render an `sg_get` request."""
    request: dict[str, object] = {
        'entity_type': arguments.entity_type,
        'filters': [['id', 'is', arguments.entity_id]],
        'fields': arguments.fields,
    }
    return runtime.run_query_request(
        request, arguments.entity_type, context, require_record_id=arguments.entity_id
    )


def _handle_get(
    runtime: ToolRuntime, payload: dict[str, Any], context: DiagnosticContext
) -> types.CallToolResult:
    """Fetch one entity by type and id, with its web URL."""
    arguments = _GetArguments(**payload)
    complete_payload = dataclasses.asdict(arguments)
    return runtime.audited(
        complete_payload,
        context,
        lambda: _run_get(runtime, arguments, context),
    )


def _run_query(
    runtime: ToolRuntime,
    arguments: _QueryArguments,
    context: DiagnosticContext,
) -> ToolBodyResult:
    """Validate, execute, and render an `sg_query` request."""
    request: dict[str, object] = {
        'entity_type': arguments.entity_type,
        'filters': arguments.filters,
        'fields': arguments.fields,
        'retired_only': arguments.retired_only,
    }
    if arguments.order:
        request['order'] = arguments.order
    if arguments.summarize:
        request['summarize'] = arguments.summarize
    if arguments.filter_operator:
        request['filter_operator'] = arguments.filter_operator
    if arguments.limit:
        request['limit'] = arguments.limit
    return runtime.run_query_request(request, arguments.entity_type, context)


def _handle_query(
    runtime: ToolRuntime, payload: dict[str, Any], context: DiagnosticContext
) -> types.CallToolResult:
    """Query entities with native SG filter grammar."""
    arguments = _QueryArguments(**payload)
    complete_payload = dataclasses.asdict(arguments)
    return runtime.audited(
        complete_payload,
        context,
        lambda: _run_query(runtime, arguments, context),
    )


def _summary_modifier_error(arguments: Mapping[str, object]) -> str | None:
    """Return the stricter `sg_query` summary-modifier guidance, if applicable."""
    shared_error = _engine.summary_modifier_error(arguments)
    if shared_error is not None:
        return shared_error
    fields = arguments.get('fields')
    if arguments.get('summarize') is None or not isinstance(fields, list):
        return None
    if not cast('list[object]', fields):
        return None
    return (
        'summarize cannot combine with nonempty fields; remove fields to summarize, '
        'or remove summarize to query records'
    )


def _argument_error(
    tool: str, arguments: dict[str, Any], error: jsonschema.ValidationError
) -> str:
    """Shape the schema-rejection message for one served tool."""
    message = base_argument_error(error)
    if tool == 'sg_query':
        summary_error = _summary_modifier_error(arguments)
        if summary_error is not None:
            message = f'invalid arguments: {summary_error}'
        hint = bare_condition_hint(arguments)
        if hint is not None:
            message += hint
        elif summary_error is None:
            condition_hint = _condition_shapes_hint(error)
            if condition_hint is not None:
                message += condition_hint
    return message


def surface() -> ToolSurface:
    """Return the served tool surface.

    Returns:
        The complete served tool table, its handlers, and argument-error
        shaping.
    """
    return ToolSurface(
        tools=TOOLS,
        handlers={
            'sg_schema': _handle_schema,
            'sg_query': _handle_query,
            'sg_get': _handle_get,
        },
        argument_error=_argument_error,
    )
