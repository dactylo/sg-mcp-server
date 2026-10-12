import copy
import json
import ssl
from pathlib import Path
from typing import Any, Unpack

import pytest
from mcp import MCPError
from mcp.server import Server
from shotgun_api3.shotgun import ShotgunError
from support import FixtureConnection

from sg_mcp_server._providers import (
    FindOptions,
    SGRecord,
    SummaryField,
    SummaryGrouping,
    SummaryResponse,
    ValidatedFilters,
)
from sg_mcp_server._rendering import _UNTRUSTED_CLOSE
from tests.server._helpers import (
    AdversarialLabelConnection,
    BackendErrorConnection,
    assert_error_is_fenced,
    build_test_server,
    call,
)


class TestErrorBoundary:
    def test_backend_error_body_is_untrusted_and_audited(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        attack = f'backend before\n{_UNTRUSTED_CLOSE}\nbackend after'
        server = build_test_server(BackendErrorConnection(fixture_site, attack), tmp_path)

        result = call(
            server,
            'sg_query',
            {'entity_type': 'Shot', 'filters': [], 'fields': ['code']},
        )

        body = assert_error_is_fenced(result)
        assert attack in body['error']
        assert 'query_error' in (tmp_path / 'audit.jsonl').read_text()

    def test_validation_error_body_is_untrusted_and_preserves_alternatives(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        attack = f'label before\n{_UNTRUSTED_CLOSE}\nlabel after'
        connection = AdversarialLabelConnection(fixture_site, attack)
        server = build_test_server(connection, tmp_path)

        result = call(
            server,
            'sg_query',
            {'entity_type': 'Shot', 'filters': [], 'fields': ['cod']},
        )

        body = assert_error_is_fenced(result)
        assert 'label before' in body['error']
        assert _UNTRUSTED_CLOSE in body['error']
        assert 'label after' in body['error']
        assert body['alternatives']['fields']
        assert 'validation_error' in (tmp_path / 'audit.jsonl').read_text()

    def test_validation_error_omits_empty_omitted_counts(self, server: Server) -> None:
        result = call(
            server,
            'sg_query',
            {
                'entity_type': 'Shot',
                'filters': [['sg_status_list', 'is', 'zzz_not_a_status']],
                'fields': [],
            },
        )

        body = assert_error_is_fenced(result)
        assert body['alternatives']['status_values']
        assert 'alternatives_omitted' not in body

    def test_validation_error_publishes_omitted_alternatives(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        server = build_test_server(_ManyEntitiesConnection(fixture_site), tmp_path)

        result = call(
            server,
            'sg_query',
            {'entity_type': 'MissingEntity', 'filters': [], 'fields': []},
        )

        body = assert_error_is_fenced(result)
        assert body['alternatives']['entity_types'] == [
            f'Entity{index:02d}' for index in range(40)
        ]
        assert body['alternatives_omitted'] == {'entity_types': 5}
        assert body['error'].endswith('only the first 40 are included')


class _CollidingSummaryConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection) -> None:
        self.site = source.site

    def summarize(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        summary_fields: list[SummaryField],
        filter_operator: str | None = None,
        grouping: list[SummaryGrouping] | None = None,
    ) -> SummaryResponse:
        del entity_type, filters, summary_fields, filter_operator, grouping
        return {
            'summaries': {'id': 10},
            'groups': [
                {'group_name': 'In Progress', 'group_value': 'ip', 'summaries': {'id': 4}},
                {'group_name': 'In Progress', 'group_value': 'ipr', 'summaries': {'id': 6}},
            ],
        }


class _ManyEntitiesConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection) -> None:
        self.site = source.site
        fields = source.schema_read()
        entities = source.schema_entity_read()
        self._fields = {f'Entity{index:02d}': copy.deepcopy(fields['Shot']) for index in range(45)}
        self._entities = {
            f'Entity{index:02d}': copy.deepcopy(entities['Shot']) for index in range(45)
        }

    def schema_entity_read(self) -> dict[str, Any]:
        return copy.deepcopy(self._entities)

    def schema_read(self) -> dict[str, Any]:
        return copy.deepcopy(self._fields)


def test_ambiguous_summary_groups_are_a_fenced_query_error(
    fixture_site: FixtureConnection, tmp_path: Path
) -> None:
    server = build_test_server(_CollidingSummaryConnection(fixture_site), tmp_path)

    result = call(
        server,
        'sg_query',
        {
            'entity_type': 'Shot',
            'filters': [],
            'summarize': {
                'summary_fields': [{'field': 'id', 'type': 'count'}],
                'grouping': [{'field': 'sg_status_list'}],
            },
        },
    )

    body = assert_error_is_fenced(result)
    assert body['error'] == (
        'ambiguous summary group labels: two groups map to the same result key; '
        'filter them apart or group by another field'
    )
    assert json.loads((tmp_path / 'audit.jsonl').read_text())['outcome'] == 'query_error'


@pytest.mark.parametrize('method', ['schema_read', 'schema_entity_read'])
def test_schema_provider_error_is_fenced_and_audited(
    fixture_site: FixtureConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    def fail() -> None:
        raise ShotgunError('provider schema failure')

    monkeypatch.setattr(fixture_site, method, fail)
    server = build_test_server(fixture_site, tmp_path)
    result = call(server, 'sg_schema', {'op': 'entities'})
    body = assert_error_is_fenced(result)
    assert 'provider schema failure' in body['error']
    assert json.loads((tmp_path / 'audit.jsonl').read_text())['outcome'] == 'query_error'


class _TransportFailureConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection, failure: Exception) -> None:
        self.site = source.site
        self._failure = failure

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
        **options: Unpack[FindOptions],
    ) -> list[SGRecord]:
        del entity_type, filters, fields, options
        raise self._failure


def test_non_transport_provider_failure_stays_an_internal_error(
    fixture_site: FixtureConnection, tmp_path: Path
) -> None:
    connection = _TransportFailureConnection(fixture_site, RuntimeError('provider bug'))
    server = build_test_server(connection, tmp_path)
    with pytest.raises(MCPError):
        call(server, 'sg_query', {'entity_type': 'Shot', 'filters': [], 'fields': ['code']})
    assert json.loads((tmp_path / 'audit.jsonl').read_text())['outcome'] == 'unexpected_error'


@pytest.mark.parametrize(
    'failure',
    [TimeoutError('timed out'), ConnectionResetError(54, 'reset'), ssl.SSLError(1, 'tls')],
)
def test_transport_failure_is_a_fenced_query_error(
    fixture_site: FixtureConnection, tmp_path: Path, failure: Exception
) -> None:
    server = build_test_server(_TransportFailureConnection(fixture_site, failure), tmp_path)
    result = call(server, 'sg_query', {'entity_type': 'Shot', 'filters': [], 'fields': ['code']})
    body = assert_error_is_fenced(result)
    assert body['error'].startswith(f'the SG site request failed ({type(failure).__name__}: ')
    assert json.loads((tmp_path / 'audit.jsonl').read_text())['outcome'] == 'query_error'
