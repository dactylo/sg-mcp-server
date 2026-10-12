import datetime
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NoReturn

import pytest
from support import Clock, FixtureConnection

from sg_mcp_server._diagnostics import (
    DiagnosticContext,
    DiagnosticSink,
    ObservedConnection,
    ObservedSchemaCache,
)
from sg_mcp_server._engine._errors import ValidationError
from sg_mcp_server._engine._execution import run_query
from sg_mcp_server._engine._schema import SchemaCache
from sg_mcp_server._engine._validation import validate_with_refresh
from sg_mcp_server._providers import SGReadConnection, SGRecord
from sg_mcp_server._tool_contracts import TOOLS

_NOW = datetime.datetime.fromisoformat('2026-07-31T12:00:00+00:00')
_EXPECTED_RECORD_KEYS = {
    'schema_version',
    'ts',
    'request_id',
    'tool',
    'outcome',
    'duration_ms',
    'phases_ms',
    'schema_cache',
    'provider_call_count',
    'unattributed_ms',
}


class TestDiagnosticContext:
    def test_counters_are_nonnegative_integers(self, tmp_path: Path) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_schema', 0, Clock(10))
        context.cache_hit()
        context.cache_miss()
        context.cache_refresh()
        context.provider_call()

        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        counters = [*record['schema_cache'].values(), record['provider_call_count']]
        assert all(isinstance(value, int) and value >= 0 for value in counters)

    def test_finalization_rounds_and_reconciles(self, tmp_path: Path) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_query', 0, Clock(1, 34, 123_456_789))
        with context.span('provider_call'):
            pass

        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert record['duration_ms'] == 123.456789
        assert record['phases_ms'] == {'provider_call': 0.000033}
        assert record['unattributed_ms'] == 123.456756
        assert (
            abs(
                record['duration_ms']
                - sum(record['phases_ms'].values())
                - record['unattributed_ms']
            )
            <= 0.00001
        )

    def test_nested_span_suspends_parent(self, tmp_path: Path) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_query', 0, Clock(10, 20, 30, 40, 100))

        with context.span('query_validation'), context.span('schema_cache_lookup'):
            pass

        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert record['phases_ms'] == {
            'query_validation': 0.00002,
            'schema_cache_lookup': 0.00001,
        }
        assert record['unattributed_ms'] == 0.00007

    def test_nonmonotonic_clock_clamps_remainder_and_warns_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_query', 100, Clock(110, 120, 105))
        with context.span('query_validation'):
            pass

        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert record['duration_ms'] == 0.000005
        assert record['phases_ms'] == {'query_validation': 0.00001}
        assert record['unattributed_ms'] == 0.0
        assert capsys.readouterr().err == 'warning: diagnostic accounting failed\n'

    def test_one_nanosecond_span_remains_present(self, tmp_path: Path) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_query', 0, Clock(1, 2, 2))
        with context.span('rendering'):
            pass

        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert record['phases_ms']['rendering'] == 0.000001

    def test_repeated_spans_accumulate(self, tmp_path: Path) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_query', 0, Clock(10, 20, 30, 50, 100))

        with context.span('provider_call'):
            pass
        with context.span('provider_call'):
            pass

        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert record['phases_ms'] == {'provider_call': 0.00003}
        assert record['unattributed_ms'] == 0.00007

    def test_span_entry_clock_failure_preserves_body_result(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        context = DiagnosticContext(1, 'sg_query', 0, _FailingClock())

        def run() -> str:
            with context.span('query_validation'):
                return 'body result'

        assert run() == 'body result'
        assert capsys.readouterr().err == 'warning: diagnostic accounting failed\n'

    def test_span_exit_clock_failure_preserves_body_result(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        context = DiagnosticContext(1, 'sg_query', 0, _FailingClock(10))

        def run() -> str:
            with context.span('query_validation'):
                return 'body result'

        assert run() == 'body result'
        assert capsys.readouterr().err == 'warning: diagnostic accounting failed\n'

    def test_span_exit_clock_failure_preserves_original_exception(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        context = DiagnosticContext(1, 'sg_query', 0, _FailingClock(10))
        body_error = ValueError('body failed')

        with (
            pytest.raises(ValueError, match='body failed') as raised,
            context.span('query_validation'),
        ):
            raise body_error

        assert raised.value is body_error
        assert capsys.readouterr().err == 'warning: diagnostic accounting failed\n'


class _FailingClock:
    def __init__(self, *samples: int) -> None:
        self._samples = iter(samples)

    def __call__(self) -> int:
        try:
            return next(self._samples)
        except StopIteration:
            raise RuntimeError('diagnostic clock failed') from None


class _RecordingContext(DiagnosticContext):
    def __init__(self, request_id: int, tool: str, started_ns: int) -> None:
        super().__init__(request_id, tool, started_ns)
        self.cache_events: list[str] = []

    def cache_hit(self) -> None:
        self.cache_events.append('hit')
        super().cache_hit()

    def cache_miss(self) -> None:
        self.cache_events.append('miss')
        super().cache_miss()

    def cache_refresh(self) -> None:
        self.cache_events.append('refresh')
        super().cache_refresh()


class TestDiagnosticSink:
    def test_concurrent_writes_remain_complete_json_lines(self, tmp_path: Path) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        sink = _diagnostic_sink(path)
        contexts = [sink.start('sg_query', 0) for _ in range(40)]

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(context.finish, sink) for context in contexts]
            for future in futures:
                future.result()

        lines = path.read_text().splitlines()
        records = [json.loads(line) for line in lines]
        assert len(records) == 40
        assert all(record['tool'] == 'sg_query' for record in records)

    @pytest.mark.parametrize(
        ('known_tools', 'other_catalog_tool'),
        [
            (
                (
                    'tool_one',
                    'tool_two',
                    'tool_three',
                    'tool_four',
                    'tool_five',
                    'tool_six',
                ),
                'other_tool',
            ),
            (('tool_one', 'tool_two'), 'other_tool'),
        ],
        ids=['six-tool-catalog', 'two-tool-catalog'],
    )
    def test_configured_catalog_preserves_own_names_and_normalizes_others(
        self,
        known_tools: tuple[str, ...],
        other_catalog_tool: str,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        sink = DiagnosticSink(path, known_tools=known_tools)
        for tool in (*known_tools, other_catalog_tool, 'unknown_tool'):
            sink.start(tool, 0).finish(sink)

        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert [record['tool'] for record in records] == [*known_tools, 'unknown', 'unknown']
        assert all(set(record) == _EXPECTED_RECORD_KEYS for record in records)

    def test_disabled_sink_skips_file_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        open_calls: list[Path] = []

        def record_open(path: Path, *_args: object, **_kwargs: object) -> NoReturn:
            open_calls.append(path)
            raise AssertionError('disabled diagnostics must not open a file')

        monkeypatch.setattr(Path, 'open', record_open)
        sink = _diagnostic_sink(None)
        context = sink.start('sg_query', 0)

        context.cache_hit()
        context.cache_miss()
        context.cache_refresh()
        context.provider_call()
        with context.schema_reflection():
            pass
        with context.span('query_validation'):
            pass
        context.finish(sink)

        assert open_calls == []

    def test_enabled_record_has_exact_fields_and_normalizes_tool(self, tmp_path: Path) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        adversarial_tool = 'sg_query\nsecret-tool-arguments'
        sink = _diagnostic_sink(path)
        context = sink.start(adversarial_tool, 0)

        context.set_outcome('unknown_tool')
        context.finish(sink)

        line = path.read_text()
        record = _read_record(path)
        assert set(record) == _EXPECTED_RECORD_KEYS
        assert record['schema_version'] == 1
        assert record['request_id'] > 0
        assert record['tool'] == 'unknown'
        assert record['outcome'] == 'unknown_tool'
        assert adversarial_tool not in line

    def test_known_tool_catalog_is_detached_from_mutable_input(self, tmp_path: Path) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        known_tools = ['tool_one', 'tool_two']
        sink = DiagnosticSink(path, known_tools=known_tools)

        known_tools[0] = 'sg_query'
        sink.start('tool_one', 0).finish(sink)
        sink.start('sg_query', 0).finish(sink)

        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert [record['tool'] for record in records] == ['tool_one', 'unknown']

    def test_unwritable_path_warns_once_across_sink_instances(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blocked_parent = tmp_path / 'blocked'
        blocked_parent.write_text('not a directory')
        path = blocked_parent / 'diagnostics.jsonl'

        first = _diagnostic_sink(path)
        second = _diagnostic_sink(path)
        first.start('sg_query', 0).finish(first)
        second.start('sg_get', 0).finish(second)

        assert capsys.readouterr().err == 'warning: diagnostic write failed\n'


class TestObservedViews:
    def test_cold_query_combines_cache_and_provider_observations(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_query', time.monotonic_ns())
        cache = ObservedSchemaCache(SchemaCache(fixture_site, ttl_seconds=900), context)
        connection = ObservedConnection(fixture_site, context)

        query = validate_with_refresh(
            cache,
            {'entity_type': 'Shot', 'filters': [], 'fields': ['id']},
            default_limit=50,
            hard_limit=200,
            now=_NOW,
        )
        result = run_query(connection, query)
        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert result.kind == 'records'
        assert record['schema_cache'] == {'hits': 0, 'misses': 1, 'refreshes': 0}
        assert record['provider_call_count'] == 3
        assert set(record['phases_ms']) >= {
            'schema_cache_lookup',
            'schema_reflection',
            'provider_call',
        }

    @pytest.mark.parametrize('method', ['find', 'summarize'])
    def test_failed_observed_calls_count_the_attempt(
        self,
        method: str,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_query', time.monotonic_ns())
        connection = ObservedConnection(fixture_site, context)

        def fail_provider(*_args: object, **_kwargs: object) -> NoReturn:
            raise RuntimeError('provider failed')

        monkeypatch.setattr(fixture_site, method, fail_provider)

        def call_provider() -> object:
            if method == 'find':
                return connection.find('Shot', [], ['id'])
            return connection.summarize(
                'Shot',
                [],
                [{'field': 'id', 'type': 'record_count'}],
            )

        with pytest.raises(RuntimeError, match='provider failed'):
            call_provider()
        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert record['provider_call_count'] == 1
        assert set(record['phases_ms']) == {'provider_call'}

    def test_non_query_methods_are_protocol_compatible_pass_throughs(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_get', time.monotonic_ns())
        connection = ObservedConnection(fixture_site, context)

        assert isinstance(connection, SGReadConnection)
        assert connection.schema_read() == fixture_site.schema_read()
        assert connection.schema_entity_read() == fixture_site.schema_entity_read()
        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert record['provider_call_count'] == 0
        assert record['phases_ms'] == {}

    def test_summarize_records_one_provider_call(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        cache = SchemaCache(fixture_site, ttl_seconds=900)
        query = validate_with_refresh(
            cache,
            {
                'entity_type': 'Shot',
                'filters': [],
                'fields': [],
                'summarize': {'summary_fields': [{'field': 'id', 'type': 'record_count'}]},
            },
            default_limit=50,
            hard_limit=200,
            now=_NOW,
        )
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_query', time.monotonic_ns())

        result = run_query(ObservedConnection(fixture_site, context), query)
        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert result.kind == 'aggregate'
        assert result.aggregate == 0
        assert record['provider_call_count'] == 1
        assert set(record['phases_ms']) == {'provider_call'}

    def test_targeted_schema_read_forwards_arguments_under_schema_reflection(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = DiagnosticContext(1, 'sg_get', time.monotonic_ns())
        connection = ObservedConnection(fixture_site, context)
        calls: list[tuple[str, str | None, dict[str, Any] | None]] = []

        def schema_field_read(
            entity_type: str,
            field_name: str | None = None,
            project_entity: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            calls.append((entity_type, field_name, project_entity))
            return {'code': {'data_type': {'value': 'text'}}}

        monkeypatch.setattr(fixture_site, 'schema_field_read', schema_field_read)
        project: SGRecord = {'type': 'Project', 'id': 1}

        with context.span('schema_reflection'):
            result = connection.schema_field_read('Shot', 'code', project)
        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert result == {'code': {'data_type': {'value': 'text'}}}
        assert calls == [('Shot', 'code', project)]
        assert record['provider_call_count'] == 0
        assert set(record['phases_ms']) == {'schema_reflection'}

    def test_unknown_name_retries_with_one_refresh(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        path = tmp_path / 'diagnostics.jsonl'
        context = _RecordingContext(1, 'sg_query', time.monotonic_ns())
        cache = ObservedSchemaCache(SchemaCache(fixture_site, ttl_seconds=900), context)

        with pytest.raises(ValidationError):
            validate_with_refresh(
                cache,
                {'entity_type': 'Shot', 'filters': [], 'fields': ['still_missing']},
                default_limit=50,
                hard_limit=200,
                now=_NOW,
            )
        assert context.cache_events == ['miss', 'refresh']
        context.finish(_diagnostic_sink(path))

        record = _read_record(path)
        assert record['schema_cache'] == {'hits': 0, 'misses': 1, 'refreshes': 1}
        assert record['provider_call_count'] == 4


def _diagnostic_sink(path: Path | None) -> DiagnosticSink:
    return DiagnosticSink(path, known_tools=tuple(tool.name for tool in TOOLS))


def _read_record(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text().strip())
