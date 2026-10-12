"""Query execution: paged find and native summarize translation."""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, TypeAlias, cast

from sg_mcp_server._engine._errors import QueryError, transport_query_error
from sg_mcp_server._providers import provider_error_type, provider_transport_error_types

if TYPE_CHECKING:
    from sg_mcp_server._engine._validation import ValidatedQuery
    from sg_mcp_server._providers import SGReadConnection, SGRecord, SGValue

QueryResult: TypeAlias = '_AggregateQueryResult | _RecordsQueryResult'

_AMBIGUOUS_SUMMARY_GROUPS = (
    'ambiguous summary group labels: two groups map to the same result key; '
    'filter them apart or group by another field'
)
_NULL_GROUP_KEY = '(none)'
_SUMMARY_GROUP_SEPARATOR = '|'


@dataclasses.dataclass(frozen=True)
class _AggregateQueryResult:
    """One aggregate query outcome."""

    kind: Literal['aggregate']
    aggregate: SGValue


@dataclasses.dataclass(frozen=True)
class _RecordsQueryResult:
    """One records query outcome."""

    kind: Literal['records']
    records: list[SGRecord]
    truncated: bool
    last_id: int | None


def _canonical_group_key(value: SGValue, name: SGValue) -> str:
    """Return the stable key for one native summarize group."""
    if value is None:
        return _NULL_GROUP_KEY
    if isinstance(value, dict):
        entity_type = value.get('type')
        entity_id = value.get('id')
        if (
            not isinstance(entity_type, str)
            or not isinstance(entity_id, int)
            or isinstance(entity_id, bool)
        ):
            raise QueryError('provider summary group_value is not a supported entity link')
        return f'{entity_type}:{entity_id}'
    if isinstance(value, list):
        raise QueryError('provider summary group_value must be scalar or an entity link')
    return str(name if name is not None else value)


def _find(
    connection: SGReadConnection,
    query: ValidatedQuery,
    limit: int,
) -> object:
    """One find call with backend faults translated to QueryError."""
    shotgun_error = provider_error_type()
    transport_errors = provider_transport_error_types()
    try:
        return connection.find(
            query.entity_type,
            query.filters,
            query.fields,
            order=query.order,
            filter_operator=query.filter_operator,
            limit=limit,
            retired_only=query.retired_only,
        )
    except shotgun_error as error:
        raise QueryError(str(error)) from error
    except transport_errors as error:
        raise transport_query_error(error) from error


def _provider_mapping(value: object, label: str) -> dict[str, object]:
    """Narrow one provider mapping or raise a bounded query error."""
    if not isinstance(value, Mapping):
        raise QueryError(f'{label} must be a mapping')
    mapping = cast('Mapping[object, object]', value)
    if any(not isinstance(key, str) for key in mapping):
        raise QueryError(f'{label} must use string keys')
    return cast('dict[str, object]', dict(mapping))


def _provider_value(value: object, label: str) -> SGValue:
    """Narrow one recursive provider value or raise a bounded query error."""
    if value is None or isinstance(
        value,
        (bool, int, float, str, datetime.date, datetime.datetime),
    ):
        return value
    if isinstance(value, list):
        return [_provider_value(item, label) for item in cast('list[object]', value)]
    if isinstance(value, Mapping):
        mapping = _provider_mapping(cast('Mapping[object, object]', value), label)
        return {key: _provider_value(item, label) for key, item in mapping.items()}
    raise QueryError(f'{label} contains an unsupported provider value')


def _provider_record(value: object, label: str) -> SGRecord:
    """Narrow one provider record or summary mapping."""
    mapping = _provider_mapping(value, label)
    return {key: _provider_value(item, label) for key, item in mapping.items()}


def _flatten_summary_groups(
    raw_groups: list[object], summary_field: str, grouping_depth: int
) -> dict[str, SGValue]:
    """Flatten provider summary leaves into distinct displayed group keys."""
    mapping: dict[str, SGValue] = {}

    def visit(raw_group: object, depth: int, labels: list[str], index: int) -> None:
        label = f'provider summary group {index}'
        group = _provider_mapping(raw_group, label)
        if 'group_value' not in group:
            raise QueryError(f'{label} is missing group_value')
        value = _provider_value(group['group_value'], f'{label} group_value')
        name = _provider_value(group.get('group_name'), f'{label} group_name')
        group_summaries = _provider_record(group.get('summaries'), f'{label} summaries')
        group_labels = [*labels, _canonical_group_key(value, name)]
        if depth < grouping_depth:
            child_groups = group.get('groups')
            if not isinstance(child_groups, list):
                raise QueryError(f'{label} groups must be a list')
            for child_index, child in enumerate(cast('list[object]', child_groups)):
                visit(child, depth + 1, group_labels, child_index)
            return
        key = _SUMMARY_GROUP_SEPARATOR.join(group_labels)
        if key in mapping:
            raise QueryError(_AMBIGUOUS_SUMMARY_GROUPS)
        mapping[key] = group_summaries.get(summary_field)

    for index, raw_group in enumerate(raw_groups):
        visit(raw_group, 1, [], index)
    return mapping


def _provider_rows(value: object) -> list[SGRecord]:
    """Narrow one provider row list."""
    if not isinstance(value, list):
        raise QueryError('provider rows must be a list')
    return [
        _provider_record(row, f'provider row {index}')
        for index, row in enumerate(cast('list[object]', value))
    ]


def _run_records(connection: SGReadConnection, query: ValidatedQuery) -> QueryResult:
    """Fetch one bounded record page with a lookahead row."""
    fetched = _provider_rows(_find(connection, query, query.limit + 1))
    truncated = len(fetched) > query.limit
    rows = fetched[: query.limit]
    id_led = (
        bool(query.order)
        and query.order[0] == {'field_name': 'id', 'direction': 'asc'}
        and query.filter_operator in (None, 'all')
    )
    candidate_id = rows[-1].get('id') if truncated and id_led and rows else None
    last_id = (
        candidate_id
        if isinstance(candidate_id, int) and not isinstance(candidate_id, bool)
        else None
    )
    return _RecordsQueryResult(
        kind='records',
        records=rows,
        truncated=truncated,
        last_id=last_id,
    )


def _run_summarize(connection: SGReadConnection, query: ValidatedQuery) -> QueryResult:
    """Run a native summary query and translate its provider response."""
    shotgun_error = provider_error_type()
    transport_errors = provider_transport_error_types()
    block = query.summarize or {}
    summary_field = block['summary_fields'][0]['field']
    try:
        response = connection.summarize(
            query.entity_type,
            query.filters,
            block['summary_fields'],
            filter_operator=query.filter_operator,
            grouping=block.get('grouping'),
        )
    except shotgun_error as error:
        raise QueryError(str(error)) from error
    except transport_errors as error:
        raise transport_query_error(error) from error
    response_mapping = _provider_mapping(response, 'provider summary response')
    summaries = _provider_record(
        response_mapping.get('summaries'),
        'provider summary summaries',
    )
    if not block.get('grouping'):
        return _AggregateQueryResult(kind='aggregate', aggregate=summaries.get(summary_field))
    raw_groups = response_mapping.get('groups')
    if not isinstance(raw_groups, list):
        raise QueryError('provider summary groups must be a list')
    mapping = _flatten_summary_groups(
        cast('list[object]', raw_groups), summary_field, len(block.get('grouping') or [])
    )
    return _AggregateQueryResult(kind='aggregate', aggregate=mapping)


def run_query(connection: SGReadConnection, query: ValidatedQuery) -> QueryResult:
    """Execute a validated query.

    Args:
        connection: SG connection (live client or the test double).
        query: The validated IR.

    Returns:
        A records result containing at most `query.limit` rows and a
        `truncated` flag determined by one lookahead row. `last_id` is the
        non-boolean integer ID from the last returned row when the result is
        truncated, ordering begins with ascending `id`, and the filter operator
        is `None` or `all`; otherwise it is `None`.

        A summarized query instead returns the selected aggregate value, or a
        mapping of grouped leaf values. Group keys use `(none)` for nulls,
        `Type:id` for entity links, and the stringified group name for other
        values, falling back to the stringified value when the name is `None`.
        Nested group labels are joined with `|`.

    Raises:
        QueryError: If the backend rejects the query, a supported transport
            failure occurs, the provider response has an unsupported shape or
            value, or distinct summary groups produce the same result key.
    """
    if query.summarize is not None:
        return _run_summarize(connection, query)
    return _run_records(connection, query)
