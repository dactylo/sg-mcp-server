from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, TypedDict, Unpack, cast

from shotgun_api3.shotgun import Fault

if TYPE_CHECKING:
    from support import FixtureConnection

    from sg_mcp_server._providers import (
        SGRecord,
        SGValue,
        SummaryField,
        SummaryGroup,
        SummaryGrouping,
        SummaryResponse,
        ValidatedFilters,
    )

_AGGREGATE_OPERATIONS: tuple[str, ...] = ('avg', 'count', 'max', 'min', 'sum')
_NULL_GROUP_LABEL = '(none)'
_NUMERIC_OPERATIONS: frozenset[str] = frozenset({'avg', 'sum'})
_SUMMARY_TYPES: dict[str, str] = {
    'average': 'avg',
    'count': 'count',
    'maximum': 'max',
    'minimum': 'min',
    'record_count': 'count',
    'sum': 'sum',
}


class _AggregateError(Exception):
    pass


class _NativeResponseOptions(TypedDict, total=False):
    filter_operator: str | None
    grouping: list[SummaryGrouping] | None


def _aggregable_values(records: list[SGRecord], operation: str, field: str) -> list[SGValue]:
    values: list[SGValue] = []
    for record in records:
        if field not in record:
            raise _AggregateError(
                f'aggregate field {field!r} was not returned by the query; '
                'request it in fields as well'
            )
        value = record[field]
        if value is None:
            continue
        if operation in _NUMERIC_OPERATIONS and not isinstance(value, (int, float)):
            raise _AggregateError(f'{operation} needs numeric values, but {field} holds {value!r}')
        values.append(value)
    return values


def _group_identity(value: SGValue) -> tuple[object, ...]:
    if isinstance(value, dict):
        if 'type' in value and 'id' in value:
            return ('entity', _group_identity(value['type']), _group_identity(value['id']))
        return (
            'dict',
            tuple((key, _group_identity(item)) for key, item in sorted(value.items())),
        )
    if isinstance(value, list):
        return ('list', tuple(_group_identity(item) for item in value))
    return (type(value), value)


def _group_label(value: SGValue) -> str:
    if value is None:
        return _NULL_GROUP_LABEL
    if isinstance(value, dict):
        if 'type' in value and 'id' in value:
            return f'{value["type"]}:{value["id"]}'
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _group_values(record: SGRecord, group_by: list[str]) -> list[tuple[SGValue, ...]]:
    values: list[tuple[SGValue, ...]] = [()]
    for field in group_by:
        if field not in record:
            raise _AggregateError(
                f'group_by field {field!r} was not returned by the query; '
                'request it in fields as well'
            )
        value = record[field]
        field_values = value if isinstance(value, list) else [value]
        if not field_values:
            field_values = [None]
        values = [(*existing, item) for existing in values for item in field_values]
    return values


def _scalar_value(value: SGValue) -> SGValue:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _reduce_records(records: list[SGRecord], operation: str, field: str | None) -> SGValue:
    if operation not in _AGGREGATE_OPERATIONS:
        raise _AggregateError(
            f'{operation!r} is not a supported aggregate operation; '
            f'use one of {", ".join(_AGGREGATE_OPERATIONS)}'
        )
    if operation == 'count':
        return len(records)
    if not field:
        raise _AggregateError(f'{operation} requires a field to aggregate')
    values = _aggregable_values(records, operation, field)
    if not values:
        return None
    numeric_values = cast('list[int | float]', values)
    if operation == 'sum':
        return sum(numeric_values)
    if operation == 'avg':
        return sum(numeric_values) / len(numeric_values)
    ordered_values = cast('list[str]', values)
    if operation == 'min':
        return _scalar_value(min(ordered_values))
    return _scalar_value(max(ordered_values))


def _native_groups(
    contributions: list[tuple[tuple[SGValue, ...], SGRecord]],
    depth: int,
    operation: str,
    field: str | None,
    summary_field: str,
) -> list[SummaryGroup]:
    grouped: dict[
        tuple[object, ...],
        tuple[SGValue, list[tuple[tuple[SGValue, ...], SGRecord]]],
    ] = {}
    for values, row in contributions:
        value = values[depth]
        identity = _group_identity(value)
        if identity not in grouped:
            grouped[identity] = (value, [])
        grouped[identity][1].append((values, row))
    groups: list[SummaryGroup] = []
    for value, members in sorted(grouped.values(), key=lambda item: _group_label(item[0])):
        records = list({id(row): row for _values, row in members}.values())
        group: SummaryGroup = {
            'group_name': _group_label(value),
            'group_value': value,
            'summaries': {summary_field: _reduce_records(records, operation, field)},
        }
        if depth + 1 < len(members[0][0]):
            group['groups'] = _native_groups(
                members,
                depth + 1,
                operation,
                field,
                summary_field,
            )
        groups.append(group)
    return groups


def native_response(
    connection: FixtureConnection,
    entity_type: str,
    filters: ValidatedFilters,
    summary_fields: list[SummaryField],
    **options: Unpack[_NativeResponseOptions],
) -> SummaryResponse:
    filter_operator = options.get('filter_operator')
    grouping = options.get('grouping')
    entry = summary_fields[0]
    operation = _SUMMARY_TYPES[str(entry['type'])]
    summary_field = str(entry['field'])
    group_fields = [str(group['field']) for group in grouping or []]
    fetch = list(group_fields)
    if operation != 'count':
        fetch.append(summary_field)
    rows = connection.find(entity_type, filters, fetch or ['id'], filter_operator=filter_operator)
    try:
        if not group_fields:
            value = _scalar_value(
                _reduce_records(rows, operation, None if operation == 'count' else summary_field)
            )
            return {'summaries': {summary_field: value}, 'groups': []}
        aggregate_field = None if operation == 'count' else summary_field
        contributions = [
            (values, row) for row in rows for values in _group_values(row, group_fields)
        ]
        groups = _native_groups(
            contributions,
            0,
            operation,
            aggregate_field,
            summary_field,
        )
        total = _scalar_value(
            _reduce_records(rows, operation, None if operation == 'count' else summary_field)
        )
    except _AggregateError as error:
        raise Fault(str(error)) from error
    return {'summaries': {summary_field: total}, 'groups': groups}
