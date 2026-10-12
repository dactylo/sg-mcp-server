"""Validate native SG read queries against reflected site schema."""

# pyright: reportPrivateUsage=false

import dataclasses
import datetime
from collections.abc import Mapping
from typing import NoReturn, TypedDict, cast

from sg_mcp_server._engine import _errors as errors
from sg_mcp_server._engine import _relative_dates as relative_dates
from sg_mcp_server._engine import _schema as schema
from sg_mcp_server._errors import bare_condition_hint
from sg_mcp_server._providers import SGValue, SummaryRequest, ValidatedFilters

_BETWEEN_OPERATORS: frozenset[str] = frozenset({'between', 'not_between'})
_BETWEEN_VALUE_COUNT = 2
_CALENDAR_CONDITION_LENGTH = 3
_DATE_OPERATORS: frozenset[str] = frozenset(
    {
        'between',
        'greater_than',
        'in',
        'is',
        'is_not',
        'less_than',
        'not_between',
        *relative_dates.CALENDAR_OPERATORS,
        *relative_dates.SPAN_OPERATORS,
    }
)
_ENTITY_NAME_OPERATORS: frozenset[str] = frozenset(
    {'name_contains', 'name_ends_with', 'name_not_contains', 'name_starts_with'}
)
_ENTITY_TYPE_OPERATORS: frozenset[str] = frozenset({'type_is', 'type_is_not'})
_EXPLICIT_RELATIVE_KEYS: tuple[str, ...] = ('field', 'op', 'count', 'unit')
_FILTER_GROUP_KEYS: frozenset[str] = frozenset({'filter_operator', 'filters'})
_GROUPING_DIRECTIONS: frozenset[str] = frozenset({'asc', 'desc'})
_GROUPING_KEYS: frozenset[str] = frozenset({'direction', 'field', 'type'})
_GROUPING_TYPES: frozenset[str] = frozenset({'exact'})
_LIST_OPERATORS: frozenset[str] = frozenset({'in', 'not_in'})
_MIN_CONDITION_LENGTH = 3
_NULLABLE_OPERATORS: frozenset[str] = frozenset({'is', 'is_not'})
_OPERATORS_BY_TYPE: dict[str, frozenset[str]] = {
    'checkbox': frozenset({'is', 'is_not'}),
    'date': _DATE_OPERATORS,
    'date_time': _DATE_OPERATORS,
    'entity': frozenset(
        {
            'in',
            'is',
            'is_not',
            'name_contains',
            'name_ends_with',
            'name_not_contains',
            'name_starts_with',
            'type_is',
            'type_is_not',
        }
    ),
    'entity_type': frozenset({'is'}),
    'float': frozenset(
        {'between', 'greater_than', 'in', 'is', 'is_not', 'less_than', 'not_between'}
    ),
    'list': frozenset({'in', 'is', 'is_not', 'not_in'}),
    'multi_entity': frozenset({'is', 'is_not'}),
    'number': frozenset(
        {'between', 'greater_than', 'in', 'is', 'is_not', 'less_than', 'not_between'}
    ),
    'status_list': frozenset({'in', 'is', 'is_not', 'not_in'}),
    'text': frozenset(
        {
            'contains',
            'ends_with',
            'in',
            'is',
            'is_not',
            'not_contains',
            'not_in',
            'starts_with',
        }
    ),
}
_ORDER_KEYS: frozenset[str] = frozenset({'direction', 'field_name'})
_QUERY_KEYS: frozenset[str] = frozenset(
    {
        'entity_type',
        'fields',
        'filter_operator',
        'filters',
        'limit',
        'order',
        'retired_only',
        'summarize',
    }
)
_SPAN_CONDITION_LENGTH = 4
_SUMMARIZE_KEYS: frozenset[str] = frozenset({'grouping', 'summary_fields'})
_SUMMARY_FIELD_KEYS: frozenset[str] = frozenset({'field', 'type'})
_SUMMARY_TYPES: dict[str, str] = {
    'average': 'avg',
    'count': 'count',
    'maximum': 'max',
    'minimum': 'min',
    'record_count': 'count',
    'sum': 'sum',
}


@dataclasses.dataclass(frozen=True)
class ValidatedQuery:
    """Executable native SG read request after schema validation.

    Attributes are fixed, but nested lists and mappings remain mutable.

    Attributes:
        entity_type: Reflected entity type to query.
        filters: Native SG filters with relative dates rewritten to bounds.
        fields: Requested fields, defaulting to `id` when none were selected.
        order: Ordering clauses with ascending `id` appended when no `id`
            ordering is present.
        filter_operator: Top-level filter conjunction, or the provider default.
        limit: Effective row limit after applying the configured hard cap.
        retired_only: Whether to query retired records.
        summarize: Native summary request, or `None` for a records query.
    """

    entity_type: str
    filters: ValidatedFilters
    fields: list[str]
    order: list[dict[str, str]]
    filter_operator: str | None
    limit: int
    retired_only: bool
    summarize: SummaryRequest | None


class _StructuralQuery(TypedDict):
    """Structurally normalized input for semantic query validation."""

    entity_type: str
    fields: list[str]
    filter_operator: str | None
    filters: list[object]
    limit: int
    order: list[dict[str, str]]
    retired_only: bool
    summarize: SummaryRequest | None


@dataclasses.dataclass(frozen=True)
class _ValidationContext:
    """Carry schema and timezone through one validation walk."""

    _site_schema: schema.SiteSchema
    _zone: datetime.tzinfo


def _as_array(value: object, message: str) -> list[object]:
    """Return a decoded JSON array or raise the supplied validation message."""
    if not isinstance(value, list):
        raise errors.ValidationError(message)
    return cast('list[object]', value)


def _as_mapping(value: object, message: str) -> Mapping[str, object]:
    """Return a read-only query mapping with string keys or reject it."""
    if not isinstance(value, Mapping):
        raise errors.ValidationError(message)
    mapping = cast('Mapping[object, object]', value)
    if any(not isinstance(key, str) for key in mapping):
        raise errors.ValidationError(message)
    return cast('Mapping[str, object]', mapping)


def _as_object(value: object, message: str) -> dict[str, object]:
    """Return a decoded JSON object or raise the supplied validation message."""
    if not isinstance(value, dict):
        raise errors.ValidationError(message)
    return cast('dict[str, object]', _as_mapping(cast('dict[object, object]', value), message))


def _as_strings(value: object, message: str) -> list[str]:
    """Return a decoded JSON array of strings or reject it."""
    items = _as_array(value, message)
    if any(not isinstance(item, str) for item in items):
        raise errors.ValidationError(message)
    return cast('list[str]', items)


def _check_operator(entity_type: str, field: str, data_type: str, operator: str) -> None:
    """Reject an operator the field's data type does not support."""
    allowed = sorted(_OPERATORS_BY_TYPE.get(data_type, frozenset()))
    if not allowed:
        raise errors.ValidationError(
            f'{entity_type}.{field} is a {data_type} field, which this backend cannot '
            'filter on; fetch it with "fields" and filter on another field instead',
            alternatives={'operators': []},
        )
    if operator not in allowed:
        raise errors.ValidationError(
            f'{operator!r} cannot be used on {entity_type}.{field} ({data_type}); '
            f'valid operators are {", ".join(allowed)}',
            alternatives={'operators': allowed},
        )


def _field_error_context(
    site_schema: schema.SiteSchema,
    entity_type: str,
    field: str,
) -> tuple[str, str]:
    """Return the deepest valid owner and unresolved terminal field."""
    parts = field.split('.', 2)
    if len(parts) != schema.LINKED_PATH_SEGMENTS:
        return entity_type, field
    link_field, linked_type, remainder = parts
    if (
        not site_schema.has_field(entity_type, link_field)
        or not site_schema.has_entity(linked_type)
        or site_schema.field_data_type(entity_type, link_field) not in {'entity', 'multi_entity'}
    ):
        return entity_type, field
    valid_types = site_schema.valid_types(entity_type, link_field) or []
    if valid_types and linked_type not in valid_types:
        return entity_type, field
    return _field_error_context(site_schema, linked_type, remainder)


def _fields_represented_by_grouping(
    payload: Mapping[str, object],
    fields: object,
) -> bool:
    """Return whether selected fields are already native grouping declarations."""
    if not isinstance(fields, list) or not fields:
        return False
    raw_summarize = payload.get('summarize')
    if not isinstance(raw_summarize, dict):
        return False
    summarize = cast('dict[str, object]', raw_summarize)
    grouping = summarize.get('grouping')
    if not isinstance(grouping, list):
        return False
    grouping_fields: set[str] = set()
    for raw_entry in cast('list[object]', grouping):
        if not isinstance(raw_entry, dict):
            continue
        entry = cast('dict[str, object]', raw_entry)
        field = entry.get('field')
        if isinstance(field, str):
            grouping_fields.add(field)
    return all(
        isinstance(field, str) and field in grouping_fields
        for field in cast('list[object]', fields)
    )


def _normalized_summarize(block: dict[str, object]) -> SummaryRequest:
    """Copy a validated summarize block, omitting null grouping and filling grouping defaults."""
    normalized = dict(block)
    grouping = block.get('grouping')
    if grouping is None:
        normalized.pop('grouping', None)
    if isinstance(grouping, list):
        normalized['grouping'] = [
            {**entry, 'type': entry.get('type', 'exact')}
            for entry in (cast('dict[str, object]', raw) for raw in cast('list[object]', grouping))
        ]
    return cast('SummaryRequest', normalized)


def _raise_unknown_entity(site_schema: schema.SiteSchema, entity_type: str) -> NoReturn:
    """Raise the unknown-entity error with enumerated alternatives."""
    suggestions = site_schema.suggest_entities(entity_type)
    if suggestions:
        candidate = suggestions[0]
        display_name = site_schema.entity_display_name(candidate)
        message = (
            f'{entity_type!r} does not exist; a different entity type {candidate!r} '
            f'(display name {display_name!r}) exists -- querying it answers a different question'
        )
    else:
        message = f'{entity_type!r} does not exist'
    raise errors.ValidationError(
        message,
        alternatives={'entity_types': site_schema.entity_types()},
        unknown_name=True,
    )


def _reject_disallowed_null(
    operator: str,
    owner_entity: str,
    owner_field: str,
    operand: object,
) -> None:
    """Reject a null operand unless the operator explicitly supports it."""
    if operand is None and operator not in _NULLABLE_OPERATORS:
        raise errors.ValidationError(
            f'{operator} on {owner_entity}.{owner_field} cannot compare against null; '
            'use "is"/"is_not" to test for null'
        )


def _reject_unknown_keys(
    value: dict[str, object],
    allowed: frozenset[str],
    owner: str,
) -> None:
    """Reject keys outside one closed nested-object shape."""
    unknown = sorted((key for key in value if key not in allowed), key=str)
    if unknown:
        names = ', '.join(str(key) for key in unknown)
        raise errors.ValidationError(
            f'unknown {owner} key(s): {names}; accepted keys are {", ".join(sorted(allowed))}'
        )


def _normalize_explicit_relative_filters(filters: object) -> object:
    """Normalize explicit relative-date conditions throughout a native filter tree."""
    if isinstance(filters, dict):
        condition_or_group = _as_object(
            cast('dict[object, object]', filters),
            'each filter object must use string keys',
        )
        if 'field' in condition_or_group:
            _reject_unknown_keys(
                condition_or_group,
                frozenset(_EXPLICIT_RELATIVE_KEYS),
                'explicit relative-date condition',
            )
            if any(key not in condition_or_group for key in _EXPLICIT_RELATIVE_KEYS):
                required = ', '.join(f'"{key}"' for key in _EXPLICIT_RELATIVE_KEYS)
                raise errors.ValidationError(
                    f'an explicit relative-date condition requires keys {required}'
                )
            return [condition_or_group[key] for key in _EXPLICIT_RELATIVE_KEYS]
        group = dict(condition_or_group)
        if 'filters' in group:
            group['filters'] = _normalize_explicit_relative_filters(group['filters'])
        return group
    if not isinstance(filters, list):
        return filters
    normalized: list[object] = []
    for condition in cast('list[object]', filters):
        if isinstance(condition, dict):
            normalized.append(
                _normalize_explicit_relative_filters(cast('dict[object, object]', condition))
            )
        else:
            normalized.append(condition)
    return normalized


def _resolve_field(site_schema: schema.SiteSchema, entity_type: str, field: str) -> str:
    """Resolve a direct or dotted field path to its data type."""
    try:
        return site_schema.resolve_path(entity_type, field)
    except errors.SchemaError as error:
        if site_schema.has_field(entity_type, field):
            raise errors.ValidationError(str(error)) from None
        owner_entity, owner_field = _field_error_context(site_schema, entity_type, field)
        suggestions = site_schema.suggest_fields(owner_entity, owner_field)
        if suggestions:
            candidate = suggestions[0]
            label = site_schema.describe_field(owner_entity, candidate)['label']
            field_kind = 'terminal field' if owner_field != field else 'field'
            message = (
                f'{field!r} does not exist on {entity_type}; a different {field_kind} '
                f'{candidate!r} (label {label!r}) exists -- querying it answers a '
                'different question'
            )
        else:
            message = f'{field!r} does not exist on {entity_type}'
        raise errors.ValidationError(
            message,
            alternatives={'fields': site_schema.field_names(owner_entity)},
            unknown_name=True,
        ) from None


def _validate_condition(condition: object) -> None:
    """Check one filter condition for the client-side structural rules."""
    if isinstance(condition, dict):
        _validate_filters(cast('dict[object, object]', condition))
        return
    if not isinstance(condition, list):
        raise errors.ValidationError(
            f'Filters must be in the form [lval, operator, rval], got {condition!r}'
        )
    items = cast('list[object]', condition)
    if len(items) < _MIN_CONDITION_LENGTH:
        raise errors.ValidationError(
            f'Filters must be in the form [lval, operator, rval], got {condition!r}'
        )
    if not isinstance(items[0], str):
        raise errors.ValidationError(f'filter field name must be a string, got {items[0]!r}')
    if not isinstance(items[1], str):
        raise errors.ValidationError(f'filter relation must be a string, got {items[1]!r}')


def _validate_filters(filters: object) -> None:
    """Check a filter list or nested filter group."""
    if isinstance(filters, dict):
        group = _as_object(
            cast('dict[object, object]', filters),
            'nested filter groups must use string keys',
        )
        _reject_unknown_keys(group, _FILTER_GROUP_KEYS, 'nested filter group')
        if 'filter_operator' not in group or 'filters' not in group:
            found = ', '.join(sorted(group))
            raise errors.ValidationError(
                'Bad filter operator, requires keys "filter_operator" and "filters", '
                f'found {found}'
            )
        filter_operator = group['filter_operator']
        if not isinstance(filter_operator, str) or filter_operator not in {'all', 'any'}:
            raise errors.ValidationError('filter_operator must be "all" or "any"')
        _validate_filters(group['filters'])
        return
    conditions = _as_array(filters, 'filters must be a list of conditions')
    hint = bare_condition_hint({'filters': conditions})
    if hint is not None:
        raise errors.ValidationError(hint.removeprefix('; '))
    for condition in conditions:
        _validate_condition(condition)


def _validate_grouping(grouping: object) -> None:
    """Check the grouping entries of a summarize block."""
    if grouping is None:
        return
    for entry in _as_array(grouping, 'summarize.grouping must be a list'):
        group = _as_object(entry, 'each grouping entry needs a "field" key')
        _reject_unknown_keys(group, _GROUPING_KEYS, 'grouping entry')
        if 'field' not in group:
            raise errors.ValidationError('each grouping entry needs a "field" key')
        if not isinstance(group['field'], str):
            raise errors.ValidationError('grouping field name must be a string')
        grouping_type = group.get('type', 'exact')
        if not isinstance(grouping_type, str) or grouping_type not in _GROUPING_TYPES:
            raise errors.ValidationError(
                f'unsupported grouping type {grouping_type!r}; this server groups on '
                '"exact" values only'
            )
        if 'direction' in group:
            direction = group['direction']
            if not isinstance(direction, str) or direction not in _GROUPING_DIRECTIONS:
                raise errors.ValidationError('grouping direction must be "asc" or "desc"')


def _validate_summarize(summarize: object) -> None:
    """Check the native SG summarize block."""
    block = _as_object(summarize, 'summarize must be an object with summary_fields')
    _reject_unknown_keys(block, _SUMMARIZE_KEYS, 'summarize block')
    summary_fields = _as_array(
        block.get('summary_fields'),
        'summarize.summary_fields must be a non-empty list',
    )
    if not summary_fields:
        raise errors.ValidationError('summarize.summary_fields must be a non-empty list')
    if len(summary_fields) != 1:
        raise errors.ValidationError(
            'this server executes one summary field per call; issue one call per summary field'
        )
    entry = _as_object(
        summary_fields[0],
        'each summary field needs "field" and "type" keys',
    )
    _reject_unknown_keys(entry, _SUMMARY_FIELD_KEYS, 'summary field entry')
    if 'field' not in entry or 'type' not in entry:
        raise errors.ValidationError('each summary field needs "field" and "type" keys')
    if not isinstance(entry['field'], str):
        raise errors.ValidationError('summary field name must be a string')
    summary_type = entry['type']
    if not isinstance(summary_type, str) or summary_type not in _SUMMARY_TYPES:
        raise errors.ValidationError(
            f'unsupported summary type {summary_type!r}; '
            f'use one of {", ".join(sorted(_SUMMARY_TYPES))}'
        )
    _validate_grouping(block.get('grouping'))


def _validated_date(entity_type: str, field: str, value: object) -> str:
    """Check one ISO date operand for a date field."""
    if not isinstance(value, str):
        raise errors.ValidationError(
            f'{entity_type}.{field} is a date field; pass an ISO string such as '
            f'"2026-07-31", got {value!r}'
        )
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        raise errors.ValidationError(
            f'{value!r} is not an ISO date for {entity_type}.{field}; '
            'use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS'
        ) from None
    return value


def _validated_datetime(
    entity_type: str,
    field: str,
    value: object,
    zone: datetime.tzinfo,
) -> datetime.datetime:
    """Normalize one ISO date-time operand to an aware configured-zone value."""
    if not isinstance(value, str):
        raise errors.ValidationError(
            f'{entity_type}.{field} is a date_time field; pass an ISO string such as '
            f'"2026-07-31T12:00:00", got {value!r}'
        )
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise errors.ValidationError(
            f'{value!r} is not an ISO timestamp for {entity_type}.{field}; '
            'use YYYY-MM-DDTHH:MM:SS with an optional UTC offset'
        ) from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(zone)
    localized = parsed.replace(tzinfo=zone)
    round_trip = localized.astimezone(datetime.UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != parsed:
        raise errors.ValidationError(
            f'{value!r} does not exist in the configured timezone (a DST gap); '
            'supply an explicit UTC offset such as "2026-03-08T03:30:00-07:00"'
        )
    return localized


def _validated_order(order: object) -> list[dict[str, str]]:
    """Check the order clauses and fill in their default direction."""
    if order is None:
        return []
    message = 'order must be a list of {field_name, direction} objects'
    validated: list[dict[str, str]] = []
    for item in _as_array(order, message):
        entry = _as_object(item, message)
        _reject_unknown_keys(entry, _ORDER_KEYS, 'order entry')
        if 'field_name' not in entry:
            raise errors.ValidationError('each order entry needs a field_name')
        field_name = entry['field_name']
        if not isinstance(field_name, str):
            raise errors.ValidationError('order field_name must be a string')
        direction = entry.get('direction', 'asc')
        if not isinstance(direction, str) or direction not in {'asc', 'desc'}:
            raise errors.ValidationError('order direction must be "asc" or "desc"')
        validated.append({'field_name': field_name, 'direction': direction})
    return validated


def _validated_selection(
    site_schema: schema.SiteSchema,
    entity_type: str,
    field: str,
    value: object,
) -> str:
    """Check a value against a list or status_list field's native values."""
    valid_values = site_schema.valid_values(entity_type, field) or []
    alternatives = {'status_values': valid_values}
    if not isinstance(value, str):
        raise errors.ValidationError(
            f'{entity_type}.{field} takes a native status code, got {value!r}',
            alternatives=alternatives,
        )
    if value in valid_values:
        return value
    raise errors.ValidationError(
        f'{value!r} is not a native status code for {entity_type}.{field}; '
        'pass one of the enumerated native values',
        alternatives=alternatives,
    )


def _validated_sg_value(value: object) -> SGValue:
    """Return one supported recursive SG value or reject it."""
    if value is None or isinstance(
        value,
        (bool, int, float, str, datetime.date, datetime.datetime),
    ):
        return value
    if isinstance(value, list):
        return [_validated_sg_value(item) for item in cast('list[object]', value)]
    if isinstance(value, dict):
        mapping = _as_object(
            cast('dict[object, object]', value),
            'SG value mappings must use string keys',
        )
        return {key: _validated_sg_value(item) for key, item in mapping.items()}
    raise errors.ValidationError(f'unsupported SG value {value!r}')


def _coerce_entity_value(
    site_schema: schema.SiteSchema,
    entity_type: str,
    field: str,
    value: object,
) -> SGValue:
    """Validate one native entity reference without substituting a bare id."""
    if value is None:
        return None
    valid_types = site_schema.valid_types(entity_type, field) or []
    alternatives = {'entity_types': valid_types}
    if isinstance(value, dict):
        entity_ref = _as_object(
            cast('dict[object, object]', value),
            'native entity references must use string keys',
        )
        reference_type = entity_ref.get('type')
        reference_id = entity_ref.get('id')
        if not isinstance(reference_type, str) or (
            not isinstance(reference_id, int) or isinstance(reference_id, bool)
        ):
            raise errors.ValidationError(
                f'{entity_type}.{field} needs a native entity reference with string "type" '
                f'and whole-number "id", got {value!r}',
                alternatives=alternatives,
            )
        if valid_types and reference_type not in valid_types:
            raise errors.ValidationError(
                f'{entity_type}.{field} cannot point at {reference_type}; pass a native '
                'entity reference whose "type" is enumerated',
                alternatives=alternatives,
            )
        return _validated_sg_value(entity_ref)
    raise errors.ValidationError(
        f'{entity_type}.{field} needs the native form '
        f'{{"type": "...", "id": <whole number>}}, got {value!r}',
        alternatives=alternatives,
    )


def _validated_entity_value(
    site_schema: schema.SiteSchema,
    terminal: tuple[str, str],
    operator: str,
    value: object,
) -> SGValue:
    """Validate an entity condition value according to its operator family."""
    entity_type, field = terminal
    if operator in _ENTITY_NAME_OPERATORS:
        if not isinstance(value, str):
            raise errors.ValidationError(
                f'{operator} on {entity_type}.{field} needs a string, got {value!r}'
            )
        return value
    if operator in _ENTITY_TYPE_OPERATORS:
        valid_types = site_schema.valid_types(entity_type, field) or []
        if not valid_types:
            site_types = site_schema.entity_types()
            if not isinstance(value, str) or value not in site_types:
                raise errors.ValidationError(
                    f'{value!r} is not a reflected entity type, so {operator} on '
                    f'{entity_type}.{field} cannot match it',
                    alternatives={'entity_types': site_types},
                )
            return value
        alternatives = {'entity_types': valid_types}
        if not isinstance(value, str):
            raise errors.ValidationError(
                f'{operator} on {entity_type}.{field} needs an entity type name, got {value!r}',
                alternatives=alternatives,
            )
        if value not in valid_types:
            raise errors.ValidationError(
                f'{value!r} is not a legal linked type for {entity_type}.{field}; '
                'pass one of the enumerated entity types',
                alternatives=alternatives,
            )
        return value
    return _coerce_entity_value(site_schema, entity_type, field, value)


def _validated_simple(
    entity_type: str,
    field: str,
    data_type: str,
    value: object,
) -> SGValue:
    """Check a checkbox, number, float or text value against its data type."""
    if data_type == 'checkbox':
        if not isinstance(value, bool):
            raise errors.ValidationError(
                f'{entity_type}.{field} is a checkbox; pass true or false, got {value!r}'
            )
        return value
    if data_type == 'float':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise errors.ValidationError(
                f'{entity_type}.{field} is a float field; pass a number, got {value!r}'
            )
        return value
    if data_type == 'number':
        if isinstance(value, bool) or not isinstance(value, int):
            raise errors.ValidationError(
                f'{entity_type}.{field} is a number field; pass a whole number, got {value!r}'
            )
        return value
    if data_type in {'entity_type', 'text'}:
        if not isinstance(value, str):
            raise errors.ValidationError(
                f'{entity_type}.{field} is a {data_type} field; pass a string, got {value!r}'
            )
        return value
    raise errors.ValidationError(
        f'{entity_type}.{field} is a {data_type} field, which this backend cannot filter on'
    )


def _coerce_scalar_value(
    context: _ValidationContext,
    entity_type: str,
    field: str,
    data_type: str,
    value: object,
) -> SGValue:
    """Validate one non-entity condition value for its field type."""
    if value is None:
        return None
    if data_type in {'list', 'status_list'}:
        return _validated_selection(context._site_schema, entity_type, field, value)
    if data_type == 'date':
        return _validated_date(entity_type, field, value)
    if data_type == 'date_time':
        return _validated_datetime(entity_type, field, value, context._zone)
    return _validated_simple(entity_type, field, data_type, value)


def _coerce(
    context: _ValidationContext,
    entity_type: str,
    field: str,
    data_type: str,
    value: object,
) -> SGValue:
    """Validate one condition value according to its field's data type."""
    if data_type in {'entity', 'multi_entity'}:
        return _coerce_entity_value(context._site_schema, entity_type, field, value)
    return _coerce_scalar_value(context, entity_type, field, data_type, value)


def _value_condition(
    context: _ValidationContext,
    condition: list[object],
    data_type: str,
    owner_entity: str,
    owner_field: str,
) -> list[SGValue]:
    """Validate one non-relative native condition and preserve its grammar."""
    field, operator = str(condition[0]), str(condition[1])
    if operator in _LIST_OPERATORS:
        if len(condition) != _MIN_CONDITION_LENGTH:
            raise errors.ValidationError(
                f'{operator} on {owner_entity}.{owner_field} needs one list of values'
            )
        items = _as_array(
            condition[2],
            f'{operator} on {owner_entity}.{owner_field} needs a list of values, '
            f'got {condition[2]!r}',
        )
        for item in items:
            _reject_disallowed_null(operator, owner_entity, owner_field, item)
        values = [_coerce(context, owner_entity, owner_field, data_type, item) for item in items]
        return [field, operator, values]
    if operator in _BETWEEN_OPERATORS:
        if len(condition) != _BETWEEN_VALUE_COUNT + 2:
            raise errors.ValidationError(
                f'{operator} on {owner_entity}.{owner_field} needs exactly two values'
            )
        _reject_disallowed_null(operator, owner_entity, owner_field, condition[2])
        _reject_disallowed_null(operator, owner_entity, owner_field, condition[3])
        low = _coerce(context, owner_entity, owner_field, data_type, condition[2])
        high = _coerce(context, owner_entity, owner_field, data_type, condition[3])
        return [field, operator, low, high]
    if len(condition) != _MIN_CONDITION_LENGTH:
        raise errors.ValidationError(
            f'{operator} on {owner_entity}.{owner_field} needs exactly one value'
        )
    value = condition[2]
    _reject_disallowed_null(operator, owner_entity, owner_field, value)
    if data_type in {'entity', 'multi_entity'}:
        return [
            field,
            operator,
            _validated_entity_value(
                context._site_schema,
                (owner_entity, owner_field),
                operator,
                value,
            ),
        ]
    return [
        field,
        operator,
        _coerce(context, owner_entity, owner_field, data_type, value),
    ]


def _whole_number(value: object) -> object:
    """Convert integral floats to integers at integer-only filter positions."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _rewritten_condition(
    context: _ValidationContext,
    condition: list[object],
    data_type: str,
    terminal: tuple[str, str],
    now: datetime.datetime,
) -> list[SGValue]:
    """Validate one native condition and rewrite relative-date operators."""
    owner_entity, owner_field = terminal
    field, operator = str(condition[0]), str(condition[1])
    if operator in relative_dates.SPAN_OPERATORS:
        if len(condition) != _SPAN_CONDITION_LENGTH:
            raise errors.ValidationError(
                f'{operator} takes four elements: [field, "{operator}", count, unit], '
                f'for example ["updated_at", "{operator}", 7, "DAY"]. '
                f'Valid units are {", ".join(relative_dates.RELATIVE_UNITS)}.'
            )
        count, unit = _whole_number(condition[2]), condition[3]
        if not isinstance(unit, str):
            raise errors.ValidationError(f'{operator} needs a string time unit, got {unit!r}')
        bounds = relative_dates.span_range(operator, count, unit, now)
    elif operator in relative_dates.CALENDAR_OPERATORS:
        if len(condition) != _CALENDAR_CONDITION_LENGTH:
            raise errors.ValidationError(
                f'{operator} takes three elements: [field, "{operator}", offset], '
                'where offset is 0 for the current period, -1 for the previous one.'
            )
        offset = _whole_number(condition[2])
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise errors.ValidationError(f'{operator} needs a whole-number offset, got {offset!r}')
        bounds = relative_dates.calendar_range(operator, offset, now)
    else:
        return _value_condition(
            context,
            condition,
            data_type,
            owner_entity,
            owner_field,
        )
    values = relative_dates.field_values(bounds, data_type)
    return [field, relative_dates.rewritten_relation(operator), values[0], values[1]]


def _grounded_filters(
    context: _ValidationContext,
    entity_type: str,
    filters: list[object],
    now: datetime.datetime,
) -> ValidatedFilters:
    """Walk native filters: ground every condition and rewrite relative dates."""
    grounded: ValidatedFilters = []
    for condition in filters:
        if isinstance(condition, dict):
            group = _as_object(
                cast('dict[object, object]', condition),
                'nested filter groups must use string keys',
            )
            nested = _as_array(group['filters'], 'nested filters must be a list')
            grounded.append(
                {
                    'filter_operator': _validated_sg_value(group['filter_operator']),
                    'filters': _grounded_filters(
                        context,
                        entity_type,
                        nested,
                        now,
                    ),
                }
            )
            continue
        items = _as_array(condition, 'each filter condition must be a list')
        field, operator = str(items[0]), str(items[1])
        data_type = _resolve_field(context._site_schema, entity_type, field)
        owner_entity, owner_field = context._site_schema.resolve_terminal(entity_type, field)
        _check_operator(owner_entity, owner_field, data_type, operator)
        grounded.append(
            _rewritten_condition(
                context,
                items,
                data_type,
                (owner_entity, owner_field),
                now,
            )
        )
    return grounded


def summary_modifier_error(payload: Mapping[str, object]) -> str | None:
    """Return the fail-closed native-summary modifier error, if applicable.

    Args:
        payload: Structurally valid native query arguments.

    Returns:
        Actionable guidance when native summarize would ignore record
        modifiers, otherwise `None`.
    """
    if payload.get('summarize') is None:
        return None
    fields = payload.get('fields')
    order = payload.get('order')
    limit = payload.get('limit')
    fields_are_modifier = (
        isinstance(fields, list)
        and bool(cast('list[object]', fields))
        and not _fields_represented_by_grouping(
            payload,
            cast('list[object]', fields),
        )
    )
    modifiers = [
        name
        for name, present in (
            ('fields', fields_are_modifier),
            ('order', isinstance(order, list) and bool(cast('list[object]', order))),
            ('limit', isinstance(limit, int) and not isinstance(limit, bool) and limit > 0),
            ('retired_only', payload.get('retired_only') is True),
        )
        if present
    ]
    if not modifiers:
        return None
    return (
        f'native summarize cannot honor record-query modifiers: {", ".join(modifiers)}; '
        'remove those modifiers to summarize, or remove summarize to query records'
    )


def _structural_validate(query: object) -> _StructuralQuery:
    """Check that a native SG query is structurally well formed."""
    call = _as_mapping(query, 'sg_query input must be an object with string keys')
    unknown = sorted(set(call) - _QUERY_KEYS)
    if unknown:
        raise errors.ValidationError(
            f'unknown sg_query argument(s): {", ".join(unknown)}; '
            f'accepted arguments are {", ".join(sorted(_QUERY_KEYS))}'
        )
    entity_type = call.get('entity_type')
    if not isinstance(entity_type, str) or not entity_type:
        raise errors.ValidationError('sg_query requires a string entity_type')
    if 'filters' not in call:
        raise errors.ValidationError('sg_query requires filters; use [] to match everything')
    filters = _normalize_explicit_relative_filters(call['filters'])
    _validate_filters(filters)
    validated_filters = _as_array(filters, 'filters must be a list of conditions')

    fields: list[str] = []
    if call.get('fields') is not None:
        fields = _as_strings(call['fields'], 'fields must be a list of strings')
    order = _validated_order(call.get('order'))
    filter_operator = call.get('filter_operator')
    if filter_operator is not None and (
        not isinstance(filter_operator, str) or filter_operator not in {'all', 'any'}
    ):
        raise errors.ValidationError('filter_operator must be "all" or "any"')
    limit = call.get('limit', 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise errors.ValidationError('limit must be a non-negative whole number')
    retired_only = call.get('retired_only', False)
    if not isinstance(retired_only, bool):
        raise errors.ValidationError('retired_only must be true or false')
    summarize: SummaryRequest | None = None
    if call.get('summarize') is not None:
        _validate_summarize(call['summarize'])
        summarize = _normalized_summarize(
            _as_object(call['summarize'], 'summarize must be an object with summary_fields')
        )
    modifier_error = summary_modifier_error(call)
    if modifier_error is not None:
        raise errors.ValidationError(modifier_error)

    return {
        'entity_type': entity_type,
        'filters': validated_filters,
        'fields': fields,
        'order': order,
        'filter_operator': filter_operator,
        'limit': limit,
        'retired_only': retired_only,
        'summarize': summarize,
    }


def validate_query(
    site_schema: schema.SiteSchema,
    payload: Mapping[str, object],
    default_limit: int,
    hard_limit: int,
    now: datetime.datetime,
) -> ValidatedQuery:
    """Validate a raw tool payload into the executable IR.

    Args:
        site_schema: Reflected schema to ground against.
        payload: Raw `sg_query` tool input (native SG grammar).
        default_limit: Rows returned when the payload names no limit.
        hard_limit: Absolute row cap; larger requests clamp to it.
        now: Aware configured-zone timestamp anchoring relative dates.

    Returns:
        The executable query in a frozen outer dataclass; nested lists and
        dictionaries remain mutable. The summarize block keeps its native
        `{summary_fields, grouping}` shape.

    Raises:
        ValidationError: On a naive clock or any shape, name, operator, or
            value failure, with enumerated alternatives.
    """
    zone = now.tzinfo
    if zone is None or now.utcoffset() is None:
        raise errors.ValidationError('query clock must be timezone-aware')
    structural = _structural_validate(payload)
    entity_type: str = structural['entity_type']
    if not site_schema.has_entity(entity_type):
        _raise_unknown_entity(site_schema, entity_type)
    for field in structural['fields']:
        _resolve_field(site_schema, entity_type, field)
    for spec in structural['order']:
        _resolve_field(site_schema, entity_type, spec['field_name'])
    summarize = structural['summarize']
    if summarize is not None:
        _resolve_field(site_schema, entity_type, summarize['summary_fields'][0]['field'])
        for group in summarize.get('grouping', []):
            _resolve_field(site_schema, entity_type, group['field'])
    context = _ValidationContext(site_schema, zone)
    filters = _grounded_filters(context, entity_type, structural['filters'], now)
    order = list(structural['order'])
    if not any(spec['field_name'] == 'id' for spec in order):
        order = [*order, {'field_name': 'id', 'direction': 'asc'}]
    return ValidatedQuery(
        entity_type=entity_type,
        filters=filters,
        fields=structural['fields'] or ['id'],
        order=order,
        filter_operator=structural['filter_operator'],
        limit=min(structural['limit'] or default_limit, hard_limit),
        retired_only=structural['retired_only'],
        summarize=summarize,
    )


def validate_with_schema(
    cache: schema.SchemaSource,
    payload: Mapping[str, object],
    default_limit: int,
    hard_limit: int,
    now: datetime.datetime,
) -> tuple[ValidatedQuery, schema.SiteSchema]:
    """Validate and retain the schema snapshot, refreshing once on an unknown name.

    Args:
        cache: The schema cache; `refresh()` is called at most once.
        payload: Raw `sg_query` tool input.
        default_limit: Rows returned when the payload names no limit.
        hard_limit: Absolute row cap.
        now: Aware configured-zone timestamp anchoring relative dates.

    Returns:
        The validated query and the exact schema snapshot it was validated
        against.

    Raises:
        ValidationError: When validation fails against the fresh schema too
            (or immediately, for non-name failures).
        QueryError: When schema reflection fails.
    """
    site_schema = cache.current()
    try:
        query = validate_query(site_schema, payload, default_limit, hard_limit, now)
    except errors.ValidationError as error:
        if not error.unknown_name:
            raise
        site_schema = cache.refresh()
        query = validate_query(site_schema, payload, default_limit, hard_limit, now)
    return query, site_schema


def validate_with_refresh(
    cache: schema.SchemaSource,
    payload: Mapping[str, object],
    default_limit: int,
    hard_limit: int,
    now: datetime.datetime,
) -> ValidatedQuery:
    """Validate, refreshing the schema cache once on an unknown name.

    Args:
        cache: The schema cache; `refresh()` is called at most once.
        payload: Raw `sg_query` tool input.
        default_limit: Rows returned when the payload names no limit.
        hard_limit: Absolute row cap.
        now: Aware configured-zone timestamp anchoring relative dates.

    Returns:
        The validated query.

    Raises:
        ValidationError: When validation fails against the fresh schema too
            (or immediately, for non-name failures).
        QueryError: When schema reflection fails.
    """
    return validate_with_schema(cache, payload, default_limit, hard_limit, now)[0]
