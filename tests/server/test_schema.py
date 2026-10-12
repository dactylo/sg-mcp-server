import copy
from pathlib import Path
from typing import cast

from mcp.server import Server
from support import FixtureConnection

from sg_mcp_server._providers import (
    RawSchema,
)
from tests.server._helpers import assert_error_is_fenced, build_test_server, call, text_of

FRESH_ENTITY = 'FreshEntity'


class TestSchemaSearch:
    def test_scoped_search_refreshes_once_and_heals_stale_entity(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        connection = _StagedSchemaConnection(fixture_site)
        server = build_test_server(connection, tmp_path)
        call(server, 'sg_schema', {'op': 'entities'})
        assert connection.reflections == 1

        result = call(
            server,
            'sg_schema',
            {'op': 'search', 'entity_type': FRESH_ENTITY, 'query': 'match_'},
        )

        assert result.is_error is False
        assert connection.reflections == 2
        assert text_of(result).startswith('[Schema listing:')
        assert result.structured_content is not None
        assert result.structured_content['contains_untrusted_site_text'] is True
        assert len(result.structured_content['matches']) == 40

    def test_still_unknown_scoped_search_refreshes_once_with_r4_alternatives(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        connection = _StagedSchemaConnection(fixture_site)
        server = build_test_server(connection, tmp_path)
        call(server, 'sg_schema', {'op': 'entities'})
        assert connection.reflections == 1

        result = call(
            server,
            'sg_schema',
            {'op': 'search', 'entity_type': 'Shott', 'query': 'code'},
        )

        body = assert_error_is_fenced(result)
        assert connection.reflections == 2
        assert body['alternatives']['entity_types']
        assert 'Did you mean' not in body['error']
        audit_lines = (tmp_path / 'audit.jsonl').read_text().splitlines()
        assert '"outcome": "validation_error"' in audit_lines[-1]


class _StagedSchemaConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection) -> None:
        self.site = source.site
        self.reflections = 0
        self._stale_entities = copy.deepcopy(dict(source.schema_entity_read()))
        self._stale_fields = copy.deepcopy(dict(source.schema_read()))
        self._fresh_entities = copy.deepcopy(self._stale_entities)
        self._fresh_fields = copy.deepcopy(self._stale_fields)

        raw_stale_entity = self._stale_entities.get('Shot')
        assert isinstance(raw_stale_entity, dict)
        fresh_entity = copy.deepcopy(cast('dict[str, object]', raw_stale_entity))
        raw_entity_name = fresh_entity.get('name')
        assert isinstance(raw_entity_name, dict)
        entity_name = cast('dict[str, object]', raw_entity_name)
        entity_name['value'] = 'Fresh Entity'
        self._fresh_entities[FRESH_ENTITY] = fresh_entity

        raw_stale_fields = self._stale_fields.get('Shot')
        assert isinstance(raw_stale_fields, dict)
        fresh_fields = copy.deepcopy(cast('dict[str, object]', raw_stale_fields))
        raw_template = fresh_fields.get('code')
        assert isinstance(raw_template, dict)
        template = cast('dict[str, object]', raw_template)
        for index in range(45):
            field = copy.deepcopy(template)
            raw_field_name = field.get('name')
            assert isinstance(raw_field_name, dict)
            field_name = cast('dict[str, object]', raw_field_name)
            field_name['value'] = f'Match {index:02d}'
            fresh_fields[f'match_{index:02d}'] = field
        self._fresh_fields[FRESH_ENTITY] = fresh_fields

    def schema_entity_read(self) -> RawSchema:
        snapshot = self._stale_entities if self.reflections == 1 else self._fresh_entities
        return copy.deepcopy(snapshot)

    def schema_read(self) -> RawSchema:
        self.reflections += 1
        snapshot = self._stale_fields if self.reflections == 1 else self._fresh_fields
        return copy.deepcopy(snapshot)


def test_integral_schema_offset_is_normalized(server: Server) -> None:
    integer = call(server, 'sg_schema', {'op': 'entities', 'offset': 1})
    integral = call(server, 'sg_schema', {'op': 'entities', 'offset': 1.0})
    assert integral.is_error is False
    assert integral.structured_content == integer.structured_content
    assert integral.structured_content is not None
    assert type(integral.structured_content['offset']) is int
