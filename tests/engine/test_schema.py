import contextlib
from collections.abc import Generator
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from support import FixtureConnection

from sg_mcp_server._engine import _schema as schema_module
from sg_mcp_server._engine._errors import QueryError, SchemaError
from sg_mcp_server._engine._schema import SchemaCache, SiteSchema
from sg_mcp_server._providers import RawSchema, provider_error_type


class TestDescriptions:
    def test_omitted_counts_are_absent_without_truncation(self) -> None:
        described = _build_enumerated_schema(40).describe_field('Shot', 'sg_status_list')

        assert 'valid_values_omitted' not in described
        assert 'display_values_omitted' not in described

    def test_truncation_is_machine_pure(self) -> None:
        described = _build_enumerated_schema(45).describe_field('Shot', 'sg_status_list')
        described_assets = _build_enumerated_schema(45).describe_field('Shot', 'assets')

        valid_values = described.get('valid_values')
        display_values = described.get('display_values')
        valid_types = described_assets.get('valid_types')
        assert isinstance(valid_values, list)
        assert all(isinstance(value, str) for value in valid_values)
        assert isinstance(display_values, dict)
        assert all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in display_values.items()
        )
        assert isinstance(valid_types, list)
        assert all(isinstance(value, str) for value in valid_types)
        assert valid_values == [f'value_{index}' for index in range(40)]
        assert described.get('valid_values_omitted') == 5
        assert display_values == {f'value_{index}': f'Label {index}' for index in range(40)}
        assert described.get('display_values_omitted') == 5
        assert valid_types == [f'value_{index}' for index in range(40)]
        assert described_assets.get('valid_types_omitted') == 5
        valid_values = cast('list[str]', valid_values)
        assert not any('sg_schema search' in value for value in valid_values)
        assert not any('sg_schema search' in key for key in display_values)


class TestReflection:
    def test_malformed_entity_display_name_raises_schema_error(self) -> None:
        schema = SiteSchema(fields={'Shot': {}}, entities={'Shot': {'name': {'value': []}}})

        with pytest.raises(SchemaError, match=r'Shot\.name\.value'):
            schema.entity_display_name('Shot')

    def test_reflects_from_connection(self, fixture_site: FixtureConnection) -> None:
        schema = _build_cache(fixture_site).current()
        assert isinstance(schema, SiteSchema)
        assert schema.has_entity('Shot')
        assert schema.has_field('Shot', 'code')

    def test_resolve_terminal_walks_to_the_owner(self, fixture_site: FixtureConnection) -> None:
        schema = _build_cache(fixture_site).current()
        assert schema.resolve_terminal('Shot', 'code') == ('Shot', 'code')
        assert schema.resolve_terminal('Task', 'entity.Shot.sg_status_list') == (
            'Shot',
            'sg_status_list',
        )

    def test_unknown_entity_raises_schema_error(self, fixture_site: FixtureConnection) -> None:
        schema = _build_cache(fixture_site).current()
        with pytest.raises(SchemaError, match='Shoot'):
            schema._require_entity('Shoot')


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.misses = 0
        self.provider_calls = 0
        self.refreshes = 0

    def cache_hit(self) -> None:
        self.events.append('hit')

    def cache_miss(self) -> None:
        self.events.append('miss')
        self.misses += 1

    def cache_refresh(self) -> None:
        self.events.append('refresh')
        self.refreshes += 1

    def provider_call(self) -> None:
        self.events.append('provider')
        self.provider_calls += 1

    @contextlib.contextmanager
    def schema_reflection(self) -> Generator[None, None, None]:
        self.events.append('reflection')
        yield


class TestCacheLifecycle:
    def test_current_is_cached(self, fixture_site: FixtureConnection) -> None:
        cache = _build_cache(fixture_site)
        assert cache.current() is cache.current()

    def test_expired_ttl_rebuilds(
        self, fixture_site: FixtureConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = 100.0
        monkeypatch.setattr(schema_module, 'time', SimpleNamespace(monotonic=lambda: now))
        initial_fields = fixture_site.schema_read()
        schema_read = Mock(side_effect=[initial_fields, {**initial_fields, 'NewEntity': {}}])
        schema_entity_read = Mock(wraps=fixture_site.schema_entity_read)
        monkeypatch.setattr(fixture_site, 'schema_read', schema_read)
        monkeypatch.setattr(fixture_site, 'schema_entity_read', schema_entity_read)
        cache = _build_cache(fixture_site, ttl=10)

        first = cache.current()
        assert not first.has_entity('NewEntity')

        now = 109.0
        assert cache.current() is first
        assert schema_read.call_count == 1
        assert schema_entity_read.call_count == 1

        now = 110.0
        refreshed = cache.current()
        assert refreshed is not first
        assert refreshed.has_entity('NewEntity')
        assert schema_read.call_count == 2
        assert schema_entity_read.call_count == 2

    def test_observed_current_records_cold_reflection_and_warm_hit(
        self, fixture_site: FixtureConnection
    ) -> None:
        cache = _build_cache(fixture_site)
        observer = _RecordingObserver()

        first = cache.current(observer)

        assert observer.events == ['miss', 'reflection', 'provider', 'provider']
        assert first is cache.current(observer)
        assert observer.events[-1] == 'hit'

    @pytest.mark.parametrize(
        ('failing_method', 'expected_provider_calls'),
        [('schema_read', 1), ('schema_entity_read', 2)],
    )
    def test_observed_reflection_counts_each_provider_attempt_before_failure(
        self,
        failing_method: str,
        expected_provider_calls: int,
        fixture_site: FixtureConnection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observer = _RecordingObserver()

        def fail_reflection() -> None:
            raise RuntimeError('reflection failed')

        monkeypatch.setattr(fixture_site, failing_method, fail_reflection)

        with pytest.raises(RuntimeError, match='reflection failed'):
            _build_cache(fixture_site).current(observer)

        assert observer.provider_calls == expected_provider_calls
        assert observer.events.count('provider') == expected_provider_calls

    def test_observed_refresh_records_refresh_without_another_miss(
        self, fixture_site: FixtureConnection
    ) -> None:
        cache = _build_cache(fixture_site)
        observer = _RecordingObserver()

        cache.current(observer)
        cache.current(observer)
        cache.refresh(observer)

        assert observer.refreshes == 1
        assert observer.misses == 1
        assert observer.provider_calls == 4

    def test_refresh_rebuilds(self, fixture_site: FixtureConnection) -> None:
        cache = _build_cache(fixture_site)
        first = cache.current()
        assert cache.refresh() is not first

    def test_refresh_reflects_before_sampling_timestamp(
        self,
        fixture_site: FixtureConnection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = _build_cache(fixture_site)
        events: list[str] = []
        schema_entity_read = fixture_site.schema_entity_read
        schema_read = fixture_site.schema_read

        def tracked_schema_entity_read() -> RawSchema:
            events.append('schema_entity_read')
            return schema_entity_read()

        def tracked_schema_read() -> RawSchema:
            events.append('schema_read')
            return schema_read()

        def failing_clock() -> float:
            events.append('clock')
            raise RuntimeError('clock failed')

        monkeypatch.setattr(fixture_site, 'schema_entity_read', tracked_schema_entity_read)
        monkeypatch.setattr(fixture_site, 'schema_read', tracked_schema_read)
        monkeypatch.setattr(schema_module.time, 'monotonic', failing_clock)

        with pytest.raises(RuntimeError, match='clock failed'):
            cache.refresh()

        assert events == ['schema_read', 'schema_entity_read', 'clock']


def _build_cache(site: FixtureConnection, ttl: int = 900) -> SchemaCache:
    return SchemaCache(site, ttl_seconds=ttl)


def _build_enumerated_schema(size: int) -> SiteSchema:
    values = [f'value_{index}' for index in range(size)]
    return SiteSchema(
        fields={
            'Shot': {
                'assets': {
                    'data_type': {'value': 'multi_entity'},
                    'description': {'value': ''},
                    'name': {'value': 'Assets'},
                    'properties': {'valid_types': {'value': values}},
                },
                'sg_status_list': {
                    'data_type': {'value': 'status_list'},
                    'description': {'value': ''},
                    'name': {'value': 'Status'},
                    'properties': {
                        'display_values': {
                            'value': {
                                value: f'Label {index}' for index, value in enumerate(values)
                            }
                        },
                        'valid_values': {'value': values},
                    },
                },
            }
        },
        entities={},
    )


@pytest.mark.parametrize('method', ['schema_read', 'schema_entity_read'])
@pytest.mark.parametrize('observed', [False, True])
def test_provider_reflection_failures_are_query_errors(
    fixture_site: FixtureConnection,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    observed: bool,
) -> None:
    failure = provider_error_type()('provider unavailable')

    def fail() -> None:
        raise failure

    monkeypatch.setattr(fixture_site, method, fail)
    cache = _build_cache(fixture_site)
    load = (lambda: cache.current(_RecordingObserver())) if observed else cache.current
    with pytest.raises(QueryError, match='provider unavailable') as caught:
        load()
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize('method', ['schema_read', 'schema_entity_read'])
def test_reflection_transport_failure_becomes_query_error(
    fixture_site: FixtureConnection, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    failure = TimeoutError('timed out')

    def fail() -> None:
        raise failure

    monkeypatch.setattr(fixture_site, method, fail)
    with pytest.raises(QueryError) as caught:
        SchemaCache(fixture_site, ttl_seconds=900).current()
    assert str(caught.value) == 'the SG site request failed (TimeoutError: timed out)'
    assert caught.value.__cause__ is failure
