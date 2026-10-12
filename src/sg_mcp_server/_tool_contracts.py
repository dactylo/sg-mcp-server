"""Define the exact contract of the served tools."""

from typing import Any, Final

from mcp import types

_FILTER_SCHEMA_BASE_CONDITIONS: Final[tuple[dict[str, Any], ...]] = (
    {
        'type': 'array',
        'prefixItems': [
            {'type': 'string'},
            {
                'type': 'string',
                'enum': ['in_last', 'in_next', 'not_in_last', 'not_in_next'],
            },
            {'type': 'integer', 'minimum': 1},
            {
                'type': 'string',
                'enum': ['HOUR', 'DAY', 'WEEK', 'MONTH', 'YEAR'],
            },
        ],
        'minItems': 4,
        'maxItems': 4,
    },
    {
        'type': 'array',
        'prefixItems': [
            {'type': 'string'},
            {
                'type': 'string',
                'enum': [
                    'in_calendar_day',
                    'in_calendar_month',
                    'in_calendar_week',
                    'in_calendar_year',
                ],
            },
            {'type': 'integer'},
        ],
        'minItems': 3,
        'maxItems': 3,
    },
    {
        'type': 'array',
        'prefixItems': [
            {'type': 'string'},
            {'type': 'string', 'enum': ['between', 'not_between']},
            {},
            {},
        ],
        'minItems': 4,
        'maxItems': 4,
    },
    {
        'type': 'array',
        'prefixItems': [
            {'type': 'string'},
            {'type': 'string', 'enum': ['in', 'not_in']},
            {'type': 'array', 'items': {}},
        ],
        'minItems': 3,
        'maxItems': 3,
    },
    {
        'type': 'array',
        'prefixItems': [
            {'type': 'string'},
            {
                'type': 'string',
                'enum': [
                    'contains',
                    'ends_with',
                    'greater_than',
                    'is',
                    'is_not',
                    'less_than',
                    'name_contains',
                    'name_ends_with',
                    'name_not_contains',
                    'name_starts_with',
                    'not_contains',
                    'starts_with',
                    'type_is',
                    'type_is_not',
                ],
            },
            {},
        ],
        'minItems': 3,
        'maxItems': 3,
    },
    {
        'type': 'object',
        'properties': {
            'field': {'type': 'string'},
            'op': {
                'type': 'string',
                'enum': ['in_last', 'in_next', 'not_in_last', 'not_in_next'],
            },
            'count': {'type': 'integer', 'minimum': 1},
            'unit': {
                'type': 'string',
                'enum': ['HOUR', 'DAY', 'WEEK', 'MONTH', 'YEAR'],
            },
        },
        'required': ['field', 'op', 'count', 'unit'],
        'additionalProperties': False,
    },
)
_FILTER_SCHEMA_INNER_GROUP: Final[dict[str, Any]] = {
    'type': 'object',
    'properties': {
        'filter_operator': {'type': 'string', 'enum': ['all', 'any']},
        'filters': {
            'type': 'array',
            'items': {'oneOf': [*_FILTER_SCHEMA_BASE_CONDITIONS]},
        },
    },
    'required': ['filter_operator', 'filters'],
    'additionalProperties': False,
}
_FILTER_SCHEMA_MAX_GROUP_DEPTH: Final = 2
_FILTER_SCHEMA_OUTER_GROUP: Final[dict[str, Any]] = {
    'type': 'object',
    'properties': {
        'filter_operator': {'type': 'string', 'enum': ['all', 'any']},
        'filters': {
            'type': 'array',
            'items': {
                'oneOf': [
                    *_FILTER_SCHEMA_BASE_CONDITIONS,
                    _FILTER_SCHEMA_INNER_GROUP,
                ]
            },
        },
    },
    'required': ['filter_operator', 'filters'],
    'additionalProperties': False,
}
_FILTER_SCHEMA_ROOT_ITEMS: Final[dict[str, Any]] = {
    'oneOf': [
        *_FILTER_SCHEMA_BASE_CONDITIONS,
        _FILTER_SCHEMA_OUTER_GROUP,
    ]
}
FILTERS_SCHEMA: Final[dict[str, Any]] = {
    'type': 'array',
    'items': _FILTER_SCHEMA_ROOT_ITEMS,
}

GROUPING_SCHEMA: Final[dict[str, Any]] = {
    'type': 'array',
    'description': 'Group records by field; type defaults to "exact" when omitted.',
    'items': {
        'type': 'object',
        'properties': {
            'field': {'type': 'string'},
            'type': {'type': 'string', 'enum': ['exact']},
            'direction': {'type': 'string', 'enum': ['asc', 'desc']},
        },
        'required': ['field'],
        'additionalProperties': False,
    },
}
ORDER_SCHEMA: Final[dict[str, Any]] = {
    'type': 'array',
    'items': {
        'type': 'object',
        'properties': {
            'field_name': {'type': 'string'},
            'direction': {'type': 'string', 'enum': ['asc', 'desc']},
        },
        'required': ['field_name', 'direction'],
        'additionalProperties': False,
    },
}
SUMMARY_FIELDS_SCHEMA: Final[dict[str, Any]] = {
    'type': 'array',
    'minItems': 1,
    'maxItems': 1,
    'items': {
        'type': 'object',
        'properties': {
            'field': {'type': 'string'},
            'type': {
                'type': 'string',
                'enum': [
                    'average',
                    'count',
                    'maximum',
                    'minimum',
                    'record_count',
                    'sum',
                ],
            },
        },
        'required': ['field', 'type'],
        'additionalProperties': False,
    },
}

TOOL_ANNOTATIONS = types.ToolAnnotations(read_only_hint=True, destructive_hint=False)
TOOLS: Final[tuple[types.Tool, ...]] = (
    types.Tool(
        name='sg_schema',
        description=(
            'Explore the live Autodesk Flow Production Tracking (FPT/SG) site schema '
            '(site-level, not per-project). '
            "op: 'entities' lists types with display names; 'describe' lists "
            "one entity's fields compactly; 'search' matches field names or labels by "
            'case-insensitive substring (top 40). Pages of 100 via offset; '
            'refresh=true re-reflects. Selection value and entity-type lists may be capped; '
            'a <name>_omitted count reports how many entries are not shown.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                'op': {'type': 'string', 'enum': ['describe', 'entities', 'search']},
                'entity_type': {'type': 'string', 'default': ''},
                'query': {'type': 'string', 'default': ''},
                'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
                'refresh': {'type': 'boolean', 'default': False},
            },
            'required': ['op'],
            'additionalProperties': False,
        },
        annotations=TOOL_ANNOTATIONS,
    ),
    types.Tool(
        name='sg_query',
        description=(
            'Query Autodesk Flow Production Tracking (FPT/SG) entities with native SG '
            'filter grammar. filters is a list of exact condition shapes: ordinary '
            '[field, operator, value]; between/not_between '
            '[field, operator, low, high]; in/not_in with an array value; relative spans '
            '[field, operator, positive_count, unit]; calendar relations '
            '[field, operator, integer_offset]. Relative units are HOUR, DAY, WEEK, MONTH, '
            'and YEAR. Groups use {"filter_operator": "all"|"any", "filters": [...]} '
            f'with at most {_FILTER_SCHEMA_MAX_GROUP_DEPTH} group objects on a path. Every '
            'query validates against the live schema first; errors enumerate legal '
            'alternatives and never substitute. Results are capped with narrow-or-aggregate '
            'guidance. summarize takes exactly one native summary field per call and cannot '
            'combine with nonempty fields/order, positive limit, or retired_only=true. Query '
            'example: {"entity_type": "Shot", "filters": '
            '[["sg_status_list", "is", "ip"], {"filter_operator": "any", "filters": '
            '[["updated_at", "in_last", 7, "DAY"], ["code", "starts_with", "NF"]]}], '
            '"fields": ["code"]} Summary example: {"summary_fields": [{"field": '
            '"id", "type": "count"}], "grouping": [{"field": "sg_status_list", "type": '
            '"exact", "direction": "asc"}]}'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                'entity_type': {'type': 'string'},
                'filters': {**FILTERS_SCHEMA, 'default': []},
                'fields': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
                'order': {**ORDER_SCHEMA, 'default': []},
                'summarize': {
                    'type': 'object',
                    'properties': {
                        'summary_fields': SUMMARY_FIELDS_SCHEMA,
                        'grouping': GROUPING_SCHEMA,
                    },
                    'required': ['summary_fields'],
                    'additionalProperties': False,
                },
                'filter_operator': {
                    'type': 'string',
                    'enum': ['', 'all', 'any'],
                    'default': '',
                },
                'limit': {'type': 'integer', 'minimum': 0, 'default': 0},
                'retired_only': {'type': 'boolean', 'default': False},
            },
            'required': ['entity_type'],
            'allOf': [
                {
                    'not': {
                        'properties': {'fields': {'minItems': 1}},
                        'required': ['fields', 'summarize'],
                    }
                },
                {
                    'not': {
                        'properties': {'order': {'minItems': 1}},
                        'required': ['order', 'summarize'],
                    }
                },
                {
                    'not': {
                        'properties': {'limit': {'minimum': 1}},
                        'required': ['limit', 'summarize'],
                    }
                },
                {
                    'not': {
                        'properties': {'retired_only': {'const': True}},
                        'required': ['retired_only', 'summarize'],
                    }
                },
            ],
            'additionalProperties': False,
        },
        annotations=TOOL_ANNOTATIONS,
    ),
    types.Tool(
        name='sg_get',
        description=(
            'Fetch one Autodesk Flow Production Tracking (FPT/SG) entity by type and id, '
            'with its web URL.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                'entity_type': {'type': 'string'},
                'entity_id': {'type': 'integer', 'minimum': 1},
                'fields': {'type': 'array', 'items': {'type': 'string'}},
            },
            'required': ['entity_type', 'entity_id', 'fields'],
            'additionalProperties': False,
        },
        annotations=TOOL_ANNOTATIONS,
    ),
)
