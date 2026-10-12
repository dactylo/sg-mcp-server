import copy
import datetime
from collections.abc import Mapping
from types import MappingProxyType
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from support import FixtureConnection

from sg_mcp_server._engine._errors import ValidationError
from sg_mcp_server._engine._schema import SchemaCache, SiteSchema
from sg_mcp_server._engine._validation import (
    _OPERATORS_BY_TYPE,
    ValidatedQuery,
    _resolve_field,
    validate_query,
    validate_with_refresh,
    validate_with_schema,
)

_EMPTY_FILTERS: list[object] = []
_ENTITY_NAME_OPERATORS = (
    'name_contains',
    'name_ends_with',
    'name_not_contains',
    'name_starts_with',
)
_ENTITY_TYPE_OPERATORS = ('type_is', 'type_is_not')
_NOW = datetime.datetime.fromisoformat('2026-07-31T12:00:00+00:00')
_UNHASHABLE_ARRAY: object = []
_UNHASHABLE_OBJECT: object = {}
_NON_FILTERABLE_TYPES: frozenset[str] = frozenset(
    {
        'calculated',
        'color',
        'duration',
        'image',
        'jsonb',
        'password',
        'percent',
        'pivot_column',
        'serializable',
        'summary',
        'url',
        'uuid',
    }
)


class TestBareConditionDiagnostic:
    def test_bare_condition_error_says_how_to_wrap_it(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': ['code', 'is', 'NF_0101'],
        }

        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert 'wrap it in an outer list' in info.value.message
        assert '["code", "is", "NF_0101"]' in info.value.message

    def test_bare_date_condition_is_a_validation_error(self, schema: SiteSchema) -> None:
        payload = {
            'entity_type': 'Shot',
            'filters': ['updated_at', 'is', datetime.date(2026, 7, 31)],
        }

        with pytest.raises(ValidationError, match='wrap it in an outer list') as caught:
            _run(schema, payload)

        assert '"2026-07-31"' in caught.value.message

    def test_short_malformed_filters_keep_the_condition_form_error(
        self,
        schema: SiteSchema,
    ) -> None:
        payload: dict[str, object] = {'entity_type': 'Shot', 'filters': ['code']}

        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert 'Filters must be in the form' in info.value.message


class TestEntityOperators:
    @pytest.mark.parametrize('operator', _ENTITY_NAME_OPERATORS)
    def test_bad_name_value_shape_is_rejected(
        self,
        schema: SiteSchema,
        operator: str,
    ) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [['project', operator, {'type': 'Project', 'id': 2}]],
        }

        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.unknown_name is False

    @pytest.mark.parametrize('operator', _ENTITY_NAME_OPERATORS)
    def test_name_value_is_preserved(
        self,
        schema: SiteSchema,
        operator: str,
    ) -> None:
        condition = ['project', operator, 'Example project']
        payload: dict[str, object] = {'entity_type': 'Shot', 'filters': [condition]}

        assert _run(schema, payload).filters == [condition]

    def test_native_reference_is_preserved(self, schema: SiteSchema) -> None:
        condition = ['project', 'is', {'type': 'Project', 'id': 2}]
        payload: dict[str, object] = {'entity_type': 'Shot', 'filters': [condition]}

        assert _run(schema, payload).filters == [condition]

    @pytest.mark.parametrize('operator', _ENTITY_TYPE_OPERATORS)
    def test_nonexistent_linked_type_is_rejected_with_legal_alternatives(
        self,
        schema: SiteSchema,
        operator: str,
    ) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Task',
            'filters': [['entity.Shot.project', operator, 'Asset']],
        }

        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.alternatives == {'entity_types': ['Project']}
        assert info.value.unknown_name is False

    def test_type_is_accepts_reflected_types_when_unconstrained(
        self,
        schema: SiteSchema,
    ) -> None:
        entity_type, field = _unconstrained_entity_field(schema)
        condition = [field, 'type_is', 'Shot']

        assert _run(schema, {'entity_type': entity_type, 'filters': [condition]}).filters == [
            condition
        ]

        with pytest.raises(ValidationError) as info:
            _run(
                schema,
                {
                    'entity_type': entity_type,
                    'filters': [[field, 'type_is', 'NoSuchType']],
                },
            )
        assert info.value.alternatives['entity_types']

    @pytest.mark.parametrize('operator', _ENTITY_TYPE_OPERATORS)
    def test_valid_linked_type_is_preserved(
        self,
        schema: SiteSchema,
        operator: str,
    ) -> None:
        condition = ['entity.Shot.project', operator, 'Project']
        payload: dict[str, object] = {'entity_type': 'Task', 'filters': [condition]}

        assert _run(schema, payload).filters == [condition]


class TestFilterGrounding:
    def test_bad_filter_shape_is_not_unknown_name(self, schema: SiteSchema) -> None:
        with pytest.raises(ValidationError) as info:
            _run(schema, {'entity_type': 'Shot', 'filters': [['code']]})
        assert info.value.unknown_name is False

    def test_bare_entity_id_is_rejected_not_coerced(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [['project', 'is', 2]],
        }
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert 'native form' in info.value.message
        assert 'entity_types' in info.value.alternatives

    def test_direct_misspelling_diagnostics_are_unchanged(
        self,
        schema: SiteSchema,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            _resolve_field(schema, 'Shot', 'cod')

        assert "'cod' does not exist on Shot; a different field 'code'" in caught.value.message
        assert 'different terminal field' not in caught.value.message
        assert caught.value.alternatives['fields'] == schema.field_names('Shot')[:40]
        assert caught.value.alternatives_omitted == {'fields': 41}
        assert caught.value.unknown_name is True

    def test_display_label_is_rejected_not_translated(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [['sg_status_list', 'is', 'In Progress']],
        }
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert 'status_values' in info.value.alternatives

    def test_dotted_terminal_suggestions_use_the_linked_entity(
        self,
        schema: SiteSchema,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            _resolve_field(schema, 'Task', 'entity.Shot.cod')

        assert 'entity.Shot.cod' in caught.value.message
        assert 'on Task' in caught.value.message
        assert "'code'" in caught.value.message
        assert caught.value.alternatives['fields'] == schema.field_names('Shot')[:40]
        assert caught.value.alternatives_omitted == {'fields': 41}
        assert caught.value.unknown_name is True

    def test_dotted_value_filter_validates_against_terminal_owner(
        self,
        schema: SiteSchema,
    ) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Task',
            'filters': [['entity.Shot.sg_status_list', 'is', 'zzz_not_a_status']],
        }
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert 'status_values' in info.value.alternatives

    def test_explicit_relative_date_requires_all_keys(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [{'field': 'updated_at', 'op': 'in_last', 'count': 7}],
        }
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        for key in ('field', 'op', 'count', 'unit'):
            assert key in info.value.message

    def test_illegal_operator_for_type_enumerates(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [['code', 'in_last', 7, 'DAY']],
        }
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert 'operators' in info.value.alternatives

    @pytest.mark.parametrize(
        'field',
        [
            pytest.param('missing.Shot.code', id='link-field'),
            pytest.param('content.Shot.code', id='link-field-type'),
            pytest.param('entity.NoSuchType.code', id='linked-type'),
            pytest.param('entity.Project.code', id='linked-type-restriction'),
        ],
    )
    def test_invalid_link_segment_keeps_root_fallback(
        self,
        schema: SiteSchema,
        field: str,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            _resolve_field(schema, 'Task', field)

        assert field in caught.value.message
        assert 'on Task' in caught.value.message
        assert caught.value.alternatives['fields'] == schema.field_names('Task')[:40]
        assert caught.value.alternatives_omitted == {'fields': 19}
        assert caught.value.unknown_name is True

    def test_near_miss_is_a_different_question(self, schema: SiteSchema) -> None:
        with pytest.raises(ValidationError) as info:
            _run(schema, {'entity_type': 'Shot', 'filters': [['cod', 'is', 'x']]})
        assert 'different' in info.value.message.lower()

    @pytest.mark.parametrize(
        'condition',
        [
            ['id', 'between', 1, None],
            ['sg_cut_in', 'greater_than', None],
            ['code', 'in', [None]],
        ],
    )
    def test_null_operand_rejected_for_non_null_operators(
        self,
        schema: SiteSchema,
        condition: list[object],
    ) -> None:
        with pytest.raises(ValidationError, match='use "is"/"is_not"'):
            _run(schema, {'entity_type': 'Shot', 'filters': [condition]})

    @pytest.mark.parametrize('operator', ['is', 'is_not'])
    def test_null_still_legal_for_is_and_is_not(
        self,
        schema: SiteSchema,
        operator: str,
    ) -> None:
        _run(
            schema,
            {'entity_type': 'Shot', 'filters': [['description', operator, None]]},
        )

    def test_two_hop_suggestions_keep_the_terminal_entity(self, schema: SiteSchema) -> None:
        with pytest.raises(ValidationError) as caught:
            _resolve_field(schema, 'Task', 'entity.Shot.project.Project.nam')

        assert 'entity.Shot.project.Project.nam' in caught.value.message
        assert 'on Task' in caught.value.message
        assert "'name'" in caught.value.message
        assert caught.value.alternatives['fields'] == schema.field_names('Project')
        assert caught.value.alternatives_omitted == {}
        assert caught.value.unknown_name is True

    def test_unknown_entity_enumerates_and_flags(self, schema: SiteSchema) -> None:
        with pytest.raises(ValidationError) as info:
            _run(schema, {'entity_type': 'Shoot', 'filters': []})
        assert 'Shoot' in info.value.message
        assert 'entity_types' in info.value.alternatives
        assert info.value.unknown_name is True

    def test_unknown_filter_field_in_nested_group_flags(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [
                {
                    'filter_operator': 'any',
                    'filters': [['nope_field', 'is', 'x']],
                }
            ],
        }
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.unknown_name is True

    def test_unknown_grouping_field_flags(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': [{'field': 'nope'}],
            },
        }
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.unknown_name is True

    def test_unknown_order_field_flags(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'order': [{'field_name': 'nope', 'direction': 'asc'}],
        }
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.unknown_name is True

    def test_unknown_requested_field_flags(self, schema: SiteSchema) -> None:
        with pytest.raises(ValidationError) as info:
            _run(schema, {'entity_type': 'Shot', 'filters': [], 'fields': ['cod']})
        assert info.value.unknown_name is True
        assert 'did you mean' not in info.value.message.lower()


class TestHappyPath:
    def test_ambiguous_datetime_string_uses_first_occurrence(
        self,
        schema: SiteSchema,
    ) -> None:
        zone = ZoneInfo('America/Los_Angeles')
        now = datetime.datetime(2026, 11, 2, 10, 0, tzinfo=zone)

        query = validate_query(
            schema,
            {
                'entity_type': 'Shot',
                'filters': [['updated_at', 'is', '2026-11-01T01:30:00']],
            },
            default_limit=50,
            hard_limit=200,
            now=now,
        )

        condition = query.filters[0]
        assert isinstance(condition, list)
        bound = condition[2]
        assert isinstance(bound, datetime.datetime)
        assert bound == datetime.datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        assert bound.tzinfo is zone
        assert bound.fold == 0
        assert bound.astimezone(datetime.UTC) == datetime.datetime(
            2026,
            11,
            1,
            8,
            30,
            tzinfo=datetime.UTC,
        )

    def test_dictionary_payload_remains_supported(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {'entity_type': 'Shot', 'filters': []}

        assert _run(schema, payload).entity_type == 'Shot'

    def test_dst_gap_wall_time_is_rejected(self, schema: SiteSchema) -> None:
        zone = ZoneInfo('America/Los_Angeles')
        now = datetime.datetime(2026, 3, 9, 10, 0, tzinfo=zone)

        with pytest.raises(ValidationError, match='explicit UTC offset'):
            validate_query(
                schema,
                {
                    'entity_type': 'Shot',
                    'filters': [['updated_at', 'is', '2026-03-08T02:30:00']],
                },
                default_limit=50,
                hard_limit=200,
                now=now,
            )

    def test_explicit_grouping_type_preserves_native_key_order(
        self,
        schema: SiteSchema,
    ) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': [{'type': 'exact', 'field': 'sg_status_list', 'direction': 'desc'}],
            },
        }

        query = _run(schema, payload)

        assert query.summarize is not None
        grouping = query.summarize.get('grouping')
        assert grouping is not None
        group = grouping[0]
        summarize = payload['summarize']
        assert isinstance(summarize, dict)
        assert grouping == summarize['grouping']
        assert list(group) == ['type', 'field', 'direction']

    def test_explicit_relative_date_spelling_matches_native(
        self,
        schema: SiteSchema,
    ) -> None:
        native: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [['updated_at', 'in_last', 7, 'DAY']],
            'fields': ['code'],
        }
        explicit: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [{'field': 'updated_at', 'op': 'in_last', 'count': 7, 'unit': 'DAY'}],
            'fields': ['code'],
        }

        assert _run(schema, explicit).filters == _run(schema, native).filters

    def test_id_anywhere_makes_order_total(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'order': [
                {'field_name': 'id', 'direction': 'desc'},
                {'field_name': 'code', 'direction': 'asc'},
            ],
        }

        assert _run(schema, payload).order == payload['order']

    def test_iso_datetime_string_normalizes_to_aware(self, schema: SiteSchema) -> None:
        zone = ZoneInfo('America/Los_Angeles')
        now = datetime.datetime(2026, 8, 23, 10, 0, tzinfo=zone)

        query = validate_query(
            schema,
            {'entity_type': 'Shot', 'filters': [['updated_at', 'is', '2026-08-05T12:00:00']]},
            default_limit=50,
            hard_limit=200,
            now=now,
        )

        condition = query.filters[0]
        assert isinstance(condition, list)
        bound = condition[2]
        assert isinstance(bound, datetime.datetime)
        assert bound == datetime.datetime(2026, 8, 5, 12, 0, tzinfo=zone)
        assert bound.tzinfo is zone

    def test_limit_clamped_to_hard_cap(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'fields': ['code'],
            'limit': 999,
        }
        assert _run(schema, payload).limit == 200

    def test_minimal_query(self, schema: SiteSchema) -> None:
        query = _run(schema, {'entity_type': 'Shot', 'filters': [], 'fields': ['code']})
        assert query.entity_type == 'Shot'
        assert query.limit == 50
        assert query.order[-1] == {'field_name': 'id', 'direction': 'asc'}

    def test_native_datetime_value_is_normalized(self, schema: SiteSchema) -> None:
        condition = ['updated_at', 'is', '2026-07-30T12:00:00']
        payload: dict[str, object] = {'entity_type': 'Shot', 'filters': [condition]}

        assert _run(schema, payload).filters == [
            ['updated_at', 'is', datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.UTC)]
        ]

    def test_null_internal_grouping_is_omitted(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': None,
            },
        }

        query = _run(schema, payload)

        assert query.summarize == {'summary_fields': [{'field': 'id', 'type': 'record_count'}]}

    def test_offset_datetime_string_converts_to_configured_zone(
        self,
        schema: SiteSchema,
    ) -> None:
        zone = ZoneInfo('America/Los_Angeles')
        now = datetime.datetime(2026, 8, 23, 10, 0, tzinfo=zone)

        query = validate_query(
            schema,
            {
                'entity_type': 'Shot',
                'filters': [['updated_at', 'is', '2026-08-05T12:00:00+02:00']],
            },
            default_limit=50,
            hard_limit=200,
            now=now,
        )

        condition = query.filters[0]
        assert isinstance(condition, list)
        bound = condition[2]
        assert isinstance(bound, datetime.datetime)
        assert bound.tzinfo is zone
        assert bound.astimezone(datetime.UTC) == datetime.datetime(
            2026,
            8,
            5,
            10,
            0,
            tzinfo=datetime.UTC,
        )

    def test_omitted_grouping_type_defaults_to_exact(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': [{'field': 'sg_status_list'}],
            },
        }

        query = _run(schema, payload)

        assert query.summarize is not None
        grouping = query.summarize.get('grouping')
        assert grouping is not None
        assert grouping == [{'field': 'sg_status_list', 'type': 'exact'}]
        assert list(grouping[0]) == ['field', 'type']
        summarize = payload['summarize']
        assert isinstance(summarize, dict)
        assert summarize['grouping'] == [{'field': 'sg_status_list'}]

    def test_read_only_mapping_payload_is_accepted(self, schema: SiteSchema) -> None:
        payload = MappingProxyType({'entity_type': 'Shot', 'filters': []})

        query = validate_query(schema, payload, default_limit=50, hard_limit=200, now=_NOW)

        assert query.entity_type == 'Shot'
        assert query.filters == []

    def test_relative_date_rewritten_against_now(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [['updated_at', 'in_last', 7, 'DAY']],
            'fields': ['code'],
        }
        query = _run(schema, payload)
        condition = query.filters[0]
        assert isinstance(condition, list)
        assert condition[1] == 'between'
        assert condition[2] == _NOW - datetime.timedelta(days=7)

    def test_summarize_block_preserved_natively(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': [{'field': 'sg_status_list'}],
            },
        }
        query = _run(schema, payload)
        assert query.summarize is not None
        assert query.summarize['summary_fields'][0]['type'] == 'record_count'

    def test_summarize_preserves_fields_represented_by_grouping(self, schema: SiteSchema) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'fields': ['sg_status_list'],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': [{'field': 'sg_status_list'}],
            },
        }

        query = _run(schema, payload)

        assert query.fields == ['sg_status_list']
        assert query.summarize is not None
        grouping = query.summarize.get('grouping')
        assert grouping is not None
        assert grouping == [{'field': 'sg_status_list', 'type': 'exact'}]


class TestIntegralOperands:
    @pytest.mark.parametrize(
        ('integral', 'whole'),
        [
            pytest.param(
                ['updated_at', 'in_last', 7.0, 'DAY'],
                ['updated_at', 'in_last', 7, 'DAY'],
                id='span',
            ),
            pytest.param(
                {'field': 'updated_at', 'op': 'in_last', 'count': 7.0, 'unit': 'DAY'},
                ['updated_at', 'in_last', 7, 'DAY'],
                id='explicit-span',
            ),
            pytest.param(
                ['updated_at', 'in_calendar_day', -1.0],
                ['updated_at', 'in_calendar_day', -1],
                id='calendar',
            ),
        ],
    )
    def test_integral_relative_operands_match_whole_numbers(
        self, schema: SiteSchema, integral: object, whole: object
    ) -> None:
        integral_query = _run(schema, {'entity_type': 'Shot', 'filters': [integral]})
        whole_query = _run(schema, {'entity_type': 'Shot', 'filters': [whole]})

        assert integral_query.filters == whole_query.filters

    @pytest.mark.parametrize(
        'condition',
        [
            pytest.param(['updated_at', 'in_last', 7.5, 'DAY'], id='fractional-span'),
            pytest.param(['updated_at', 'in_last', True, 'DAY'], id='boolean-span'),
            pytest.param(['updated_at', 'in_calendar_day', -1.5], id='fractional-calendar'),
            pytest.param(['updated_at', 'in_calendar_day', False], id='boolean-calendar'),
            pytest.param(['id', 'is', 1.0], id='number-field-value'),
        ],
    )
    def test_non_whole_operands_stay_rejected(self, schema: SiteSchema, condition: object) -> None:
        with pytest.raises(ValidationError):
            _run(schema, {'entity_type': 'Shot', 'filters': [condition]})


class TestOperatorTables:
    def test_future_types_get_complete_guidance(self) -> None:
        site_schema = SiteSchema(
            fields={
                'Widget': {
                    'novel': {
                        'data_type': {'value': 'future_type'},
                    }
                }
            },
            entities={},
        )

        with pytest.raises(ValidationError, match='fetch it with "fields"'):
            _run(
                site_schema,
                {'entity_type': 'Widget', 'filters': [['novel', 'is', 'value']]},
            )

    def test_malformed_reflected_data_type_is_a_validation_error(self) -> None:
        site_schema = SiteSchema(
            fields={'Widget': {'name': {'data_type': []}}},
            entities={},
        )

        with pytest.raises(ValidationError, match=r'Widget\.name'):
            _run(
                site_schema,
                {'entity_type': 'Widget', 'filters': [['name', 'is', 'value']]},
            )

    def test_reflected_types_partition_across_operator_tables(
        self,
        schema: SiteSchema,
    ) -> None:
        reflected = {
            schema.field_data_type(entity_type, field)
            for entity_type in schema.entity_types()
            for field in schema.field_names(entity_type)
        }

        assert not _OPERATORS_BY_TYPE.keys() & _NON_FILTERABLE_TYPES
        assert reflected <= _OPERATORS_BY_TYPE.keys() | _NON_FILTERABLE_TYPES

    def test_residual_types_get_complete_guidance(self, schema: SiteSchema) -> None:
        with pytest.raises(ValidationError, match='fetch it with "fields"'):
            _run(
                schema,
                {
                    'entity_type': 'Department',
                    'filters': [['color', 'is', 'red']],
                },
            )


class TestStructuralFailures:
    @pytest.mark.parametrize('direction', [_UNHASHABLE_ARRAY, _UNHASHABLE_OBJECT, 'sideways'])
    def test_grouping_direction_must_be_supported_string(
        self,
        schema: SiteSchema,
        direction: object,
    ) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': [{'field': 'code', 'direction': direction}],
            },
        }

        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.unknown_name is False

    @pytest.mark.parametrize(
        'payload',
        [
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [{'field': _UNHASHABLE_ARRAY, 'type': 'record_count'}]
                    },
                },
                id='summary-field',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                        'grouping': [{'field': _UNHASHABLE_ARRAY}],
                    },
                },
                id='grouping-field',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'order': [{'field_name': _UNHASHABLE_ARRAY, 'direction': 'asc'}],
                },
                id='order-field-name',
            ),
        ],
    )
    def test_nested_field_names_must_be_strings(
        self,
        schema: SiteSchema,
        payload: dict[str, object],
    ) -> None:
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.unknown_name is False

    @pytest.mark.parametrize(
        ('modifier', 'name'),
        [
            pytest.param({'fields': ['code']}, 'fields', id='fields'),
            pytest.param(
                {'order': [{'field_name': 'code', 'direction': 'desc'}]},
                'order',
                id='order',
            ),
            pytest.param({'limit': 1}, 'limit', id='limit'),
            pytest.param({'retired_only': True}, 'retired_only', id='retired-only'),
        ],
    )
    def test_summarize_rejects_ignored_record_modifiers(
        self,
        schema: SiteSchema,
        modifier: dict[str, object],
        name: str,
    ) -> None:
        payload: dict[str, object] = {
            'entity_type': 'Shot',
            'filters': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
            },
            **modifier,
        }

        with pytest.raises(ValidationError) as info:
            _run(schema, payload)

        assert 'native summarize cannot honor record-query modifiers' in info.value.message
        assert name in info.value.message

    @pytest.mark.parametrize(
        'payload',
        [
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [{'field': 'id', 'type': _UNHASHABLE_ARRAY}],
                    },
                },
                id='summary-type-list',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [{'field': 'id', 'type': _UNHASHABLE_OBJECT}],
                    },
                },
                id='summary-type-object',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                        'grouping': [{'field': 'sg_status_list', 'type': _UNHASHABLE_ARRAY}],
                    },
                },
                id='grouping-type-list',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                        'grouping': [{'field': 'sg_status_list', 'type': _UNHASHABLE_OBJECT}],
                    },
                },
                id='grouping-type-object',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'order': [{'field_name': 'id', 'direction': _UNHASHABLE_ARRAY}],
                },
                id='order-direction-list',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'order': [{'field_name': 'id', 'direction': _UNHASHABLE_OBJECT}],
                },
                id='order-direction-object',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'filter_operator': _UNHASHABLE_ARRAY,
                },
                id='top-filter-operator-list',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'filter_operator': _UNHASHABLE_OBJECT,
                },
                id='top-filter-operator-object',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [{'filter_operator': _UNHASHABLE_ARRAY, 'filters': _EMPTY_FILTERS}],
                },
                id='nested-filter-operator-list',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [
                        {'filter_operator': _UNHASHABLE_OBJECT, 'filters': _EMPTY_FILTERS}
                    ],
                },
                id='nested-filter-operator-object',
            ),
        ],
    )
    def test_unhashable_values_raise_validation_error(
        self,
        schema: SiteSchema,
        payload: dict[str, object],
    ) -> None:
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.unknown_name is False

    @pytest.mark.parametrize(
        'payload',
        [
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [
                        {
                            'filter_operator': 'all',
                            'filters': _EMPTY_FILTERS,
                            'unexpected': True,
                        }
                    ],
                },
                id='nested-filter-group',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                        'unexpected': True,
                    },
                },
                id='summarize-block',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [
                            {'field': 'id', 'type': 'record_count', 'unexpected': True}
                        ],
                    },
                },
                id='summary-entry',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'summarize': {
                        'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                        'grouping': [{'field': 'code', 'unexpected': True}],
                    },
                },
                id='grouping-entry',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [],
                    'order': [{'field_name': 'id', 'direction': 'asc', 'unexpected': True}],
                },
                id='order-entry',
            ),
            pytest.param(
                {
                    'entity_type': 'Shot',
                    'filters': [
                        {
                            'field': 'updated_at',
                            'op': 'in_last',
                            'count': 7,
                            'unit': 'DAY',
                            'unexpected': True,
                        }
                    ],
                },
                id='explicit-relative-entry',
            ),
        ],
    )
    def test_unknown_nested_keys_are_rejected(
        self,
        schema: SiteSchema,
        payload: dict[str, object],
    ) -> None:
        with pytest.raises(ValidationError) as info:
            _run(schema, payload)
        assert info.value.unknown_name is False


class TestValidateWithRefresh:
    @pytest.mark.parametrize(
        'field',
        [
            pytest.param('entity.Shot.cod', id='one-hop'),
            pytest.param('entity.Shot.project.Project.nam', id='two-hop'),
        ],
    )
    def test_dotted_terminal_miss_retries_once_after_refresh(
        self,
        fixture_site: FixtureConnection,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
    ) -> None:
        cache = SchemaCache(fixture_site, ttl_seconds=900)
        cache.current()
        refresh_calls = 0
        refresh = cache.refresh

        def tracked_refresh() -> SiteSchema:
            nonlocal refresh_calls
            refresh_calls += 1
            return refresh()

        monkeypatch.setattr(cache, 'refresh', tracked_refresh)
        with pytest.raises(ValidationError) as caught:
            validate_with_refresh(
                cache,
                {'entity_type': 'Task', 'filters': [], 'fields': [field]},
                default_limit=50,
                hard_limit=200,
                now=_NOW,
            )

        assert caught.value.unknown_name is True
        assert refresh_calls == 1

    def test_retries_once_after_refresh(
        self,
        fixture_site: FixtureConnection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = SchemaCache(fixture_site, ttl_seconds=900)
        cache.current()
        refresh_calls = 0
        refresh = cache.refresh

        def tracked_refresh() -> SiteSchema:
            nonlocal refresh_calls
            refresh_calls += 1
            return refresh()

        monkeypatch.setattr(cache, 'refresh', tracked_refresh)
        with pytest.raises(ValidationError):
            validate_with_refresh(
                cache,
                {'entity_type': 'Shot', 'filters': [], 'fields': ['still_missing']},
                default_limit=50,
                hard_limit=200,
                now=_NOW,
            )
        assert refresh_calls == 1

    def test_shape_error_does_not_trigger_refresh(
        self,
        fixture_site: FixtureConnection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = SchemaCache(fixture_site, ttl_seconds=900)

        def fail_refresh() -> SiteSchema:
            raise AssertionError('refresh must not be called')

        monkeypatch.setattr(cache, 'refresh', fail_refresh)
        with pytest.raises(ValidationError):
            validate_with_refresh(
                cache,
                {'entity_type': 'Shot', 'filters': [['code']]},
                default_limit=50,
                hard_limit=200,
                now=_NOW,
            )


class _StagedSource:
    def __init__(self, current: SiteSchema, refreshed: SiteSchema) -> None:
        self._current = current
        self._refreshed = refreshed
        self.refreshes = 0

    def current(self) -> SiteSchema:
        return self._current

    def refresh(self) -> SiteSchema:
        self.refreshes += 1
        return self._refreshed


def _stale_and_fresh(fixture_site: FixtureConnection) -> tuple[SiteSchema, SiteSchema]:
    fields = copy.deepcopy(dict(fixture_site.schema_read()))
    stale_fields = copy.deepcopy(fields)
    del cast('dict[str, object]', stale_fields['Shot'])['description']
    entities = fixture_site.schema_entity_read()
    return (
        SiteSchema(fields=stale_fields, entities=entities),
        SiteSchema(fields=fields, entities=entities),
    )


def _unconstrained_entity_field(site_schema: SiteSchema) -> tuple[str, str]:
    for entity_type in site_schema.entity_types():
        for field in site_schema.field_names(entity_type):
            if site_schema.field_data_type(entity_type, field) in {'entity', 'multi_entity'} and (
                not site_schema.valid_types(entity_type, field)
            ):
                return entity_type, field
    raise AssertionError('fixture schema lost its unconstrained entity fields')


@pytest.fixture
def schema(fixture_site: FixtureConnection) -> SiteSchema:
    return SchemaCache(fixture_site, ttl_seconds=900).current()


def _run(schema: SiteSchema, payload: Mapping[str, object]) -> ValidatedQuery:
    return validate_query(schema, payload, default_limit=50, hard_limit=200, now=_NOW)


def test_validate_with_schema_returns_the_refreshed_snapshot_after_a_name_miss(
    fixture_site: FixtureConnection,
) -> None:
    stale, fresh = _stale_and_fresh(fixture_site)
    source = _StagedSource(stale, fresh)
    payload: dict[str, object] = {'entity_type': 'Shot', 'filters': [], 'fields': ['description']}

    query, schema = validate_with_schema(source, payload, 50, 200, _NOW)

    assert query.fields == ['description']
    assert schema is fresh
    assert source.refreshes == 1


def test_validate_with_schema_returns_the_snapshot_that_validated(
    fixture_site: FixtureConnection,
) -> None:
    stale, fresh = _stale_and_fresh(fixture_site)
    source = _StagedSource(stale, fresh)
    payload: dict[str, object] = {'entity_type': 'Shot', 'filters': [], 'fields': ['code']}

    query, schema = validate_with_schema(source, payload, 50, 200, _NOW)

    assert query.fields == ['code']
    assert schema is stale
    assert source.refreshes == 0


def test_validation_rejects_naive_clock(schema: SiteSchema) -> None:
    with pytest.raises(ValidationError, match='timezone-aware'):
        validate_query(
            schema, {'entity_type': 'Shot', 'filters': []}, 50, 200, _NOW.replace(tzinfo=None)
        )
