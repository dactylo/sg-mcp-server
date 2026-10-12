import datetime
import http.client
import ssl
import xmlrpc.client
from typing import Unpack, cast

import pytest
from shotgun_api3.lib.httplib2.error import ServerNotFoundError
from shotgun_api3.shotgun import ShotgunError
from support import FixtureConnection

from sg_mcp_server._engine import _execution as execution
from sg_mcp_server._engine._errors import QueryError
from sg_mcp_server._engine._execution import run_query
from sg_mcp_server._engine._schema import SchemaCache, SiteSchema
from sg_mcp_server._engine._validation import ValidatedQuery, validate_query
from sg_mcp_server._providers import FindOptions, SGRecord, ValidatedFilters

_NOW = datetime.datetime.fromisoformat('2026-07-31T12:00:00+00:00')
_SHOT_COUNT = 5

_Loaded = tuple[FixtureConnection, SiteSchema]


class _FindErrorConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection) -> None:
        self.site = source.site

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
        **options: Unpack[FindOptions],
    ) -> list[SGRecord]:
        del entity_type, fields, filters, options
        raise ShotgunError('find backend fault')


class _RecordingFindConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection, rows: list[SGRecord]) -> None:
        self.site = source.site
        self._rows: list[SGRecord] = rows
        self.requested_limits: list[int] = []

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
        **options: Unpack[FindOptions],
    ) -> list[SGRecord]:
        del entity_type, fields, filters
        limit = options.get('limit', 0)
        assert isinstance(limit, int)
        self.requested_limits.append(limit)
        return self._rows[:limit] if limit else list(self._rows)


@pytest.fixture
def loaded(fixture_site: FixtureConnection) -> _Loaded:
    schema = SchemaCache(fixture_site, ttl_seconds=900).current()
    project = fixture_site.create('Project', {'name': 'P1'})
    for index in range(_SHOT_COUNT):
        fixture_site.create('Shot', {'code': f'SH{index:03d}', 'project': project})
    return fixture_site, schema


def _query_for(schema: SiteSchema, extra: dict[str, object], limit: int = 50) -> ValidatedQuery:
    base: dict[str, object] = {'entity_type': 'Shot', 'filters': [], 'fields': ['code']}
    return validate_query(schema, base | extra, default_limit=limit, hard_limit=200, now=_NOW)


class TestRecords:
    def test_any_conjunction_gets_no_keyset_anchor(self, loaded: _Loaded) -> None:
        # Under a top-level 'any', an appended id anchor would OR into the
        # query and widen it instead of continuing it.
        site, schema = loaded
        query = _query_for(
            schema,
            {
                'filters': [
                    ['id', 'greater_than', 0],
                    ['code', 'starts_with', 'SH'],
                ],
                'filter_operator': 'any',
                'limit': 2,
            },
        )
        result = run_query(site, query)
        assert result.kind == 'records'
        assert result.truncated is True
        assert result.last_id is None

    def test_backend_find_fault_becomes_query_error(self, loaded: _Loaded) -> None:
        site, schema = loaded

        with pytest.raises(QueryError, match='find backend fault'):
            run_query(_FindErrorConnection(site), _query_for(schema, {}))

    def test_backend_translation_uses_shared_provider_error_resolver(
        self,
        loaded: _Loaded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        site, schema = loaded

        class ResolvedProviderError(Exception):
            pass

        calls = 0

        def resolve() -> type[Exception]:
            nonlocal calls
            calls += 1
            return ResolvedProviderError

        monkeypatch.setattr(execution, 'provider_error_type', resolve)

        def fail(*_args: object, **_kwargs: object) -> object:
            raise ResolvedProviderError('resolved provider fault')

        monkeypatch.setattr(site, 'find', fail)

        with pytest.raises(QueryError, match='resolved provider fault'):
            run_query(site, _query_for(schema, {}))
        assert calls == 1

    def test_custom_ordering_gets_no_keyset_anchor(self, loaded: _Loaded) -> None:
        site, schema = loaded
        query = _query_for(
            schema,
            {'order': [{'field_name': 'code', 'direction': 'desc'}], 'limit': 2},
        )
        result = run_query(site, query)
        assert result.kind == 'records'
        assert result.truncated is True
        assert result.last_id is None

    def test_descending_id_order_gets_no_keyset_anchor(self, loaded: _Loaded) -> None:
        # greater_than cannot continue a descending walk (it would need
        # less_than); the honest anchor policy is ascending-only.
        site, schema = loaded
        query = _query_for(
            schema,
            {'order': [{'field_name': 'id', 'direction': 'desc'}], 'limit': 2},
        )
        result = run_query(site, query)
        assert result.kind == 'records'
        assert result.truncated is True
        assert result.last_id is None

    def test_keyset_continuation_reaches_all_rows(self, loaded: _Loaded) -> None:
        site, schema = loaded
        seen: list[int] = []
        filters: list[object] = []
        for _ in range(4):
            query = _query_for(schema, {'filters': filters, 'limit': 2})
            result = run_query(site, query)
            assert result.kind == 'records'
            for row in result.records:
                row_id = row['id']
                assert isinstance(row_id, int)
                assert not isinstance(row_id, bool)
                seen.append(row_id)
            if not result.truncated:
                break
            filters = [['id', 'greater_than', result.last_id]]
        assert len(seen) == _SHOT_COUNT
        assert len(set(seen)) == _SHOT_COUNT

    def test_returns_rows_in_id_order(self, loaded: _Loaded) -> None:
        site, schema = loaded
        result = run_query(site, _query_for(schema, {}))
        assert result.kind == 'records'
        ids: list[int] = []
        for row in result.records:
            entity_id = row['id']
            assert isinstance(entity_id, int)
            ids.append(entity_id)
        assert ids == sorted(ids)
        assert result.truncated is False
        assert result.last_id is None

    @pytest.mark.parametrize(
        'failure',
        [
            pytest.param(TimeoutError('timed out'), id='timeout'),
            pytest.param(TimeoutError(), id='no-details'),
            pytest.param(ConnectionResetError(54, 'Connection reset by peer'), id='reset'),
            pytest.param(ssl.SSLError(1, 'certificate verify failed'), id='tls'),
            pytest.param(ServerNotFoundError('Unable to find the server'), id='host-lookup'),
            pytest.param(http.client.RemoteDisconnected('closed'), id='disconnect'),
            pytest.param(
                xmlrpc.client.ProtocolError('example.invalid/api3/json', 503, 'Unavailable', {}),
                id='http-status',
            ),
        ],
    )
    def test_transport_failures_become_query_errors(
        self, loaded: _Loaded, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        site, schema = loaded

        def fail(*_args: object, **_kwargs: object) -> object:
            raise failure

        monkeypatch.setattr(site, 'find', fail)
        with pytest.raises(QueryError) as caught:
            run_query(site, _query_for(schema, {}))
        assert str(caught.value) == (
            f'the SG site request failed ({type(failure).__name__}: '
            f'{str(failure) or "no details"})'
        )
        assert caught.value.__cause__ is failure

    @pytest.mark.parametrize(
        ('row_count', 'expected_truncated'),
        [(1, False), (2, False), (3, True)],
        ids=['below-limit', 'exact-limit', 'over-limit'],
    )
    def test_limit_boundary_uses_one_lookahead_call(
        self,
        loaded: _Loaded,
        row_count: int,
        expected_truncated: bool,
    ) -> None:
        site, schema = loaded
        rows: list[SGRecord] = [
            {'type': 'Shot', 'id': index, 'code': f'SH{index:03d}'}
            for index in range(1, row_count + 1)
        ]
        connection = _RecordingFindConnection(site, rows)

        result = run_query(connection, _query_for(schema, {'limit': 2}))

        assert connection.requested_limits == [3]
        assert result.kind == 'records'
        assert len(result.records) == min(row_count, 2)
        assert result.truncated is expected_truncated
        assert result.last_id == (2 if expected_truncated else None)

    @pytest.mark.parametrize(
        'rows',
        [
            [object()],
            [{1: 'non-string key'}],
            [{'type': 'Shot', 'id': 1, 'value': object()}],
            [{'type': 'Shot', 'id': 1, 'value': {'nested': [object()]}}],
        ],
        ids=['non-mapping-row', 'non-string-key', 'unsupported-value', 'nested-value'],
    )
    def test_malformed_provider_row_becomes_query_error(
        self,
        loaded: _Loaded,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[object],
    ) -> None:
        site, schema = loaded

        def controlled_find(*_args: object, **_options: object) -> object:
            return rows

        monkeypatch.setattr(site, 'find', controlled_find)

        with pytest.raises(QueryError, match='provider row'):
            run_query(site, _query_for(schema, {}))

    @pytest.mark.parametrize('continuation_id', [True, '7'], ids=['bool', 'string'])
    def test_non_integer_id_gets_no_keyset_anchor(
        self,
        loaded: _Loaded,
        monkeypatch: pytest.MonkeyPatch,
        continuation_id: object,
    ) -> None:
        site, schema = loaded
        rows = [
            {'type': 'Shot', 'id': 1, 'code': 'SH001'},
            {'type': 'Shot', 'id': continuation_id, 'code': 'SH002'},
            {'type': 'Shot', 'id': 3, 'code': 'SH003'},
        ]

        def controlled_find(*_args: object, **_options: object) -> object:
            return rows

        monkeypatch.setattr(site, 'find', controlled_find)

        result = run_query(site, _query_for(schema, {'limit': 2}))

        assert result.kind == 'records'
        assert result.truncated is True
        assert result.last_id is None

    def test_truncation_sets_flag_and_keyset_anchor(self, loaded: _Loaded) -> None:
        site, schema = loaded
        result = run_query(site, _query_for(schema, {'limit': 2}))
        assert result.kind == 'records'
        assert len(result.records) == 2
        assert result.truncated is True
        assert result.last_id == result.records[-1]['id']


class TestSummarize:
    def test_backend_summary_fault_becomes_query_error(self, loaded: _Loaded) -> None:
        site, schema = loaded
        query = _query_for(
            schema,
            {
                'fields': [],
                'summarize': {'summary_fields': [{'field': 'code', 'type': 'sum'}]},
            },
        )
        with pytest.raises(QueryError, match='sum'):
            run_query(site, query)

    @pytest.mark.parametrize(
        ('nested', 'groups'),
        [
            pytest.param(
                True,
                [
                    {
                        'group_name': 'A|B',
                        'group_value': 'A|B',
                        'summaries': {'id': 3},
                        'groups': [
                            {'group_name': 'C', 'group_value': 'C', 'summaries': {'id': 3}}
                        ],
                    },
                    {
                        'group_name': 'A',
                        'group_value': 'A',
                        'summaries': {'id': 5},
                        'groups': [
                            {'group_name': 'B|C', 'group_value': 'B|C', 'summaries': {'id': 5}}
                        ],
                    },
                ],
                id='separator-in-label',
            ),
            pytest.param(
                False,
                [
                    {'group_name': 'In Progress', 'group_value': 'ip', 'summaries': {'id': 4}},
                    {'group_name': 'In Progress', 'group_value': 'ipr', 'summaries': {'id': 6}},
                ],
                id='shared-display-name',
            ),
            pytest.param(
                False,
                [
                    {'group_name': None, 'group_value': '(none)', 'summaries': {'id': 2}},
                    {'group_name': None, 'group_value': None, 'summaries': {'id': 7}},
                ],
                id='literal-null-marker',
            ),
        ],
    )
    def test_colliding_group_keys_are_rejected(
        self,
        loaded: _Loaded,
        monkeypatch: pytest.MonkeyPatch,
        nested: bool,
        groups: list[dict[str, object]],
    ) -> None:
        site, schema = loaded

        def controlled_summarize(*_args: object, **_options: object) -> object:
            return {'summaries': {'id': 8}, 'groups': groups}

        monkeypatch.setattr(site, 'summarize', controlled_summarize)
        query = _nested_grouped_query(schema) if nested else _grouped_query(schema)

        with pytest.raises(QueryError, match='ambiguous summary group labels'):
            run_query(site, query)

    def test_group_labels_preserve_null_scalar_and_date_values(
        self,
        loaded: _Loaded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        site, schema = loaded
        response: dict[str, object] = {
            'summaries': {'id': 3},
            'groups': [
                {'group_name': None, 'group_value': None, 'summaries': {'id': 1}},
                {'group_name': None, 'group_value': 'review', 'summaries': {'id': 1}},
                {
                    'group_name': None,
                    'group_value': datetime.date(2026, 8, 7),
                    'summaries': {'id': 1},
                },
            ],
        }

        def controlled_summarize(*_args: object, **_options: object) -> object:
            return response

        monkeypatch.setattr(site, 'summarize', controlled_summarize)

        result = run_query(site, _grouped_query(schema))

        assert result.kind == 'aggregate'
        assert result.aggregate == {
            '(none)': 1,
            'review': 1,
            '2026-08-07': 1,
        }

    def test_grouped_count_uses_canonical_entity_keys(self, loaded: _Loaded) -> None:
        site, schema = loaded
        query = _query_for(
            schema,
            {
                'fields': [],
                'summarize': {
                    'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                    'grouping': [{'field': 'project'}],
                },
            },
        )
        result = run_query(site, query)
        assert result.kind == 'aggregate'
        assert result.aggregate == {'Project:1': _SHOT_COUNT}

    def test_grouped_entity_and_null_values_are_canonicalized_directly(
        self,
        loaded: _Loaded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        site, schema = loaded
        response: dict[str, object] = {
            'summaries': {},
            'groups': [
                {
                    'group_name': 'site-owned entity name',
                    'group_value': {'type': 'Shot', 'id': 7},
                    'summaries': {'id': 3},
                },
                {
                    'group_name': '',
                    'group_value': None,
                    'summaries': {'id': 2},
                },
            ],
        }

        def controlled_summarize(*_args: object, **_options: object) -> object:
            return response

        monkeypatch.setattr(site, 'summarize', controlled_summarize)

        result = run_query(site, _grouped_query(schema))

        assert result.kind == 'aggregate'
        assert result.aggregate == {'Shot:7': 3, '(none)': 2}

    def test_grouped_zero_matches_returns_empty_mapping(self, loaded: _Loaded) -> None:
        site, schema = loaded
        query = _query_for(
            schema,
            {
                'filters': [['code', 'is', 'DOES_NOT_EXIST']],
                'fields': [],
                'summarize': {
                    'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                    'grouping': [{'field': 'project'}],
                },
            },
        )
        result = run_query(site, query)
        assert result.kind == 'aggregate'
        assert result.aggregate == {}

    @pytest.mark.parametrize(
        'response',
        cast(
            'list[object]',
            [
                [],
                {'summaries': [], 'groups': []},
                {'summaries': {}, 'groups': [object()]},
                {
                    'summaries': {},
                    'groups': [
                        {'group_name': 'bad', 'group_value': object(), 'summaries': {'id': 1}}
                    ],
                },
                {
                    'summaries': {},
                    'groups': [{'group_name': 'bad', 'group_value': 'bad', 'summaries': []}],
                },
                {
                    'summaries': {},
                    'groups': [
                        {
                            'group_name': 'bad',
                            'group_value': 'bad',
                            'summaries': {'id': object()},
                        }
                    ],
                },
                {
                    'summaries': {},
                    'groups': [
                        {
                            'group_name': 'outer',
                            'group_value': 'outer',
                            'summaries': {'id': 1},
                            'groups': {},
                        }
                    ],
                },
                {
                    'summaries': {},
                    'groups': [
                        {
                            'group_name': 'outer',
                            'group_value': 'outer',
                            'summaries': {'id': 1},
                        }
                    ],
                },
            ],
        ),
        ids=[
            'non-mapping-response',
            'non-mapping-summaries',
            'non-mapping-group',
            'unsupported-group-value',
            'non-mapping-group-summaries',
            'unsupported-summary-value',
            'malformed-child-groups',
            'missing-required-nesting',
        ],
    )
    def test_malformed_provider_summary_becomes_query_error(
        self,
        loaded: _Loaded,
        monkeypatch: pytest.MonkeyPatch,
        response: object,
    ) -> None:
        site, schema = loaded

        def controlled_summarize(*_args: object, **_options: object) -> object:
            return response

        monkeypatch.setattr(site, 'summarize', controlled_summarize)

        with pytest.raises(QueryError, match='provider summary'):
            run_query(site, _nested_grouped_query(schema))

    def test_nested_groups_emit_leaf_paths_in_provider_order(
        self,
        loaded: _Loaded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        site, schema = loaded
        response: dict[str, object] = {
            'summaries': {'id': 6},
            'groups': [
                {
                    'group_name': 'second shot',
                    'group_value': {'type': 'Shot', 'id': 2},
                    'summaries': {'id': 5},
                    'groups': [
                        {
                            'group_name': 'lighting',
                            'group_value': {'type': 'Step', 'id': 9},
                            'summaries': {'id': 2},
                        },
                        {
                            'group_name': 'compositing',
                            'group_value': {'type': 'Step', 'id': 8},
                            'summaries': {'id': 3},
                        },
                    ],
                },
                {
                    'group_name': 'first shot',
                    'group_value': {'type': 'Shot', 'id': 1},
                    'summaries': {'id': 1},
                    'groups': [
                        {
                            'group_name': 'compositing',
                            'group_value': {'type': 'Step', 'id': 8},
                            'summaries': {'id': 1},
                        }
                    ],
                },
            ],
        }

        def controlled_summarize(*_args: object, **_options: object) -> object:
            return response

        monkeypatch.setattr(site, 'summarize', controlled_summarize)

        result = run_query(site, _nested_grouped_query(schema))

        assert result.kind == 'aggregate'
        assert isinstance(result.aggregate, dict)
        assert result.aggregate == {
            'Shot:2|Step:9': 2,
            'Shot:2|Step:8': 3,
            'Shot:1|Step:8': 1,
        }
        assert list(result.aggregate) == [
            'Shot:2|Step:9',
            'Shot:2|Step:8',
            'Shot:1|Step:8',
        ]

    def test_summary_transport_failure_becomes_query_error(
        self, loaded: _Loaded, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site, schema = loaded
        failure = TimeoutError('timed out')

        def fail(*_args: object, **_kwargs: object) -> object:
            raise failure

        monkeypatch.setattr(site, 'summarize', fail)
        with pytest.raises(QueryError) as caught:
            run_query(site, _grouped_query(schema))
        assert str(caught.value) == 'the SG site request failed (TimeoutError: timed out)'
        assert caught.value.__cause__ is failure

    def test_ungrouped_count_returns_scalar(self, loaded: _Loaded) -> None:
        site, schema = loaded
        query = _query_for(
            schema,
            {
                'fields': [],
                'summarize': {'summary_fields': [{'field': 'id', 'type': 'record_count'}]},
            },
        )
        result = run_query(site, query)
        assert result.kind == 'aggregate'
        assert result.aggregate == _SHOT_COUNT


def _grouped_query(schema: SiteSchema) -> ValidatedQuery:
    return _query_for(
        schema,
        {
            'fields': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': [{'field': 'project'}],
            },
        },
    )


def _nested_grouped_query(schema: SiteSchema) -> ValidatedQuery:
    return _query_for(
        schema,
        {
            'fields': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'record_count'}],
                'grouping': [{'field': 'project'}, {'field': 'sg_status_list'}],
            },
        },
    )
