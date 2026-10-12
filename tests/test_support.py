from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from support import FixtureConnection

    from sg_mcp_server._providers import SGValue


class TestFixtureConnection:
    def test_delete_forwards_to_fixture_site(self, fixture_site: FixtureConnection) -> None:
        project = fixture_site.create('Project', {'name': 'temporary'})
        entity_id = project['id']
        assert isinstance(entity_id, int)

        assert fixture_site.delete('Project', entity_id) is True
        assert fixture_site.find_one('Project', [['id', 'is', entity_id]]) is None

    def test_find_honors_limit_and_page(self, fixture_site: FixtureConnection) -> None:
        for index in range(3):
            fixture_site.create('Project', {'name': f'P{index}'})
        assert len(fixture_site.find('Project', [], ['name'], limit=2)) == 2
        second_page = fixture_site.find('Project', [], ['name'], limit=2, page=2)
        assert len(second_page) == 1

    def test_find_preserves_nested_sg_values(self, fixture_site: FixtureConnection) -> None:
        nested: dict[str, SGValue] = {
            'date': datetime.date(2026, 8, 25),
            'datetime': datetime.datetime(2026, 8, 25, 10, 30, tzinfo=datetime.UTC),
            'items': [None, True, 7, 1.5, 'value', {'nested': 'yes'}],
        }
        created = fixture_site.create('EventLogEntry', {'event_type': 'Test', 'meta': nested})

        row = fixture_site.find_one(
            'EventLogEntry',
            [['id', 'is', created['id']]],
            ['meta'],
        )

        assert row is not None
        assert row['meta'] == nested

    def test_find_preserves_order_clause_precedence(self, fixture_site: FixtureConnection) -> None:
        first = fixture_site.create('Project', {'name': 'B'})
        second = fixture_site.create('Project', {'name': 'A'})
        third = fixture_site.create('Project', {'name': 'B'})

        rows = fixture_site.find(
            'Project',
            [],
            ['name'],
            order=[
                {'field_name': 'name', 'direction': 'asc'},
                {'field_name': 'id', 'direction': 'desc'},
            ],
        )

        assert [(row['name'], row['id']) for row in rows] == [
            ('A', second['id']),
            ('B', third['id']),
            ('B', first['id']),
        ]

    def test_fixture_connection_matches_targeted_schema_shape(
        self,
        fixture_site: FixtureConnection,
    ) -> None:
        expected = fixture_site.schema_read()['Shot']
        assert isinstance(expected, dict)

        all_fields = fixture_site.schema_field_read('Shot')
        exact = fixture_site.schema_field_read(
            'Shot',
            'code',
            {'type': 'Project', 'id': 1},
        )

        assert all_fields == expected
        assert exact == {'code': expected['code']}
