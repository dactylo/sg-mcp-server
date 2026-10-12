import asyncio
import copy
import dataclasses
import io
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self, cast

import pytest
from mcp import ClientSession, StdioServerParameters, types

from scripts import diagnose_live_latency
from sg_mcp_server._config import Config
from tests.server._helpers import build_test_server, call, read_json_lines
from tests.support import FixtureConnection

_EXPECTED_DESCRIBE_ARGUMENTS = {'op': 'describe', 'entity_type': 'Project'}
_EXPECTED_GET_ARGUMENTS = {
    'entity_type': 'Project',
    'entity_id': 982_451_653,
    'fields': ['name'],
}
_EXPECTED_QUERY_ARGUMENTS = {
    'entity_type': 'Project',
    'filters': [],
    'fields': ['name'],
    'order': [{'field_name': 'id', 'direction': 'asc'}],
    'limit': 1,
}
_EXPECTED_SUMMARY_KEYS = (
    'schema_version',
    'repeats',
    'schema_ttl_seconds',
    'default_limit',
    'hard_limit',
    'tool_names',
    'setup_ms',
    'operation_counts',
    'samples',
    'aggregates_ms',
    'project_found',
    'get_attempted',
    'get_ok',
    'valid',
)
_RECORD_MUTATIONS: list[tuple[Callable[[dict[str, Any]], object], int]] = [
    (lambda record: record.update(schema_version=True), 1),
    (lambda record: record.__setitem__('raw_content', 'private-QX7V'), 1),
    (lambda record: record['phases_ms'].__setitem__('private_phase', 0.0), 1),
    (lambda record: record['schema_cache'].__setitem__('private_cache', 0), 1),
    (lambda record: record.__setitem__('outcome', 'private_error'), 1),
    (lambda record: record.__setitem__('duration_ms', -0.1), 1),
    (lambda record: record['phases_ms'].__setitem__('rendering', -0.1), 1),
    (lambda record: record['schema_cache'].__setitem__('hits', -1), 1),
    (lambda record: record.__setitem__('provider_call_count', -1), 1),
    (lambda record: record.__setitem__('unattributed_ms', -0.1), 1),
    (lambda record: record.__setitem__('duration_ms', 99.0), 1),
    (lambda record: record['schema_cache'].__setitem__('misses', 0), 1),
    (lambda record: record['phases_ms'].pop('schema_reflection'), 1),
    (lambda record: record['phases_ms'].__setitem__('schema_reflection', 0.1), 2),
]
_REQUIRED_CREDENTIALS = (
    'SG_MCP_API_KEY',
    'SG_MCP_SCRIPT_NAME',
    'SG_MCP_SITE_URL',
)
_SUMMARY_MUTATIONS: list[Callable[[dict[str, Any]], object]] = [
    lambda summary: summary.update(schema_version=True),
    lambda summary: summary.__setitem__('raw_mcp_content', {'text': 'private-QX7V'}),
    lambda summary: summary['samples'][0].__setitem__('raw_mcp_content', {'text': 'private-QX7V'}),
    lambda summary: summary['setup_ms'].__setitem__('repository_path', '/private/QX7V'),
    lambda summary: summary['samples'][0]['schema_cache'].__setitem__('site_id', 982_451_653),
]


class _DiagnosticSession:
    def __init__(
        self,
        diagnostic_path: Path,
        *,
        include_project: bool = True,
        record_mutator: Callable[[dict[str, Any], int], None] | None = None,
        repeats: int = 2,
        response_mutator: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.call_count = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.diagnostic_path = diagnostic_path
        self.enter_count = 0
        self.exit_count = 0
        self._include_project = include_project
        self._record_mutator = record_mutator
        self._expected_names = ('sg_schema', 'sg_schema', 'sg_query') * repeats + ('sg_get',)
        self._response_mutator = response_mutator

    async def __aenter__(self) -> Self:
        self.enter_count += 1
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info
        self.exit_count += 1

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        assert arguments is not None
        self.calls.append((name, arguments))
        call_index = self.call_count
        self.call_count += 1
        expected_name = self._expected_names[call_index]
        assert name == expected_name
        if name == 'sg_schema':
            if arguments == {'op': 'entities'}:
                text = 'private schema content'
                structured_content: dict[str, Any] = {'entity_types': ['PrivateEntity']}
            else:
                assert arguments == _EXPECTED_DESCRIBE_ARGUMENTS
                text = 'private describe content'
                structured_content = {
                    'entity': 'Project',
                    'display_name': 'private project display name',
                    'fields': [
                        {
                            'name': 'private_field_QX7V',
                            'data_type': 'text',
                            'editable': True,
                            'sortable': False,
                            'filterable': False,
                        }
                    ],
                    'total': 1,
                    'offset': 0,
                    'contains_untrusted_site_text': True,
                }
        elif name == 'sg_query':
            assert arguments == _EXPECTED_QUERY_ARGUMENTS
            text = 'private query content'
            records = [
                {
                    'type': 'Project',
                    'id': 982_451_653,
                    'name': 'private project name',
                }
            ]
            structured_content = {'records': records if self._include_project else []}
        else:
            assert self._include_project
            assert arguments == _EXPECTED_GET_ARGUMENTS
            text = 'private get content'
            structured_content = {
                'records': [
                    {
                        'type': 'Project',
                        'id': 982_451_653,
                        'name': 'private project name',
                    }
                ]
            }
        if self._response_mutator is not None:
            self._response_mutator(name, structured_content)
        result = types.CallToolResult(
            content=[types.TextContent(type='text', text=text)],
            structured_content=structured_content,
        )
        record = _diagnostic_record(call_index + 31, name, cold=call_index == 0)
        if self._record_mutator is not None:
            self._record_mutator(record, call_index)
        _append_record(self.diagnostic_path, record)
        return result

    async def initialize(self) -> types.InitializeResult:
        return types.InitializeResult(
            protocol_version='2025-11-25',
            capabilities=types.ServerCapabilities(),
            server_info=types.Implementation(name='private name', version='private version'),
        )

    async def list_tools(self) -> types.ListToolsResult:
        tools = [
            types.Tool(name=name, input_schema={'type': 'object'})
            for name in ('sg_schema', 'sg_get', 'sg_query')
        ]
        return types.ListToolsResult(tools=tools)


class _StdioContext:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> tuple[object, object]:
        self.enter_count += 1
        return object(), object()

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info
        self.exit_count += 1


def _append_record(path: Path, record: dict[str, Any]) -> None:
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, separators=(',', ':')) + '\n')


def _diagnostic_record(
    request_id: int,
    tool: str,
    *,
    cold: bool = False,
) -> dict[str, Any]:
    if cold:
        phases = {
            'argument_validation': 0.125,
            'connection_lock_wait': 0.125,
            'schema_cache_lookup': 0.125,
            'schema_reflection': 1.25,
            'rendering': 0.25,
            'audit_write': 0.125,
        }
        cache = {'hits': 0, 'misses': 1, 'refreshes': 0}
        provider_call_count = 2
    elif tool == 'sg_schema':
        phases = {
            'argument_validation': 0.125,
            'connection_lock_wait': 0.125,
            'schema_cache_lookup': 0.125,
            'rendering': 0.25,
            'audit_write': 0.125,
        }
        cache = {'hits': 1, 'misses': 0, 'refreshes': 0}
        provider_call_count = 0
    else:
        phases = {
            'argument_validation': 0.125,
            'connection_lock_wait': 0.125,
            'schema_cache_lookup': 0.25,
            'query_validation': 0.125,
            'provider_call': 1.0,
            'rendering': 0.25,
            'audit_write': 0.125,
        }
        cache = {'hits': 1, 'misses': 0, 'refreshes': 0}
        provider_call_count = 1
    duration = round(sum(phases.values()) + 0.375, 6)
    return {
        'schema_version': 1,
        'ts': '2026-08-08T12:34:56.123456+00:00',
        'request_id': request_id,
        'tool': tool,
        'outcome': 'ok',
        'duration_ms': duration,
        'phases_ms': phases,
        'schema_cache': cache,
        'provider_call_count': provider_call_count,
        'unattributed_ms': 0.375,
    }


def _probe(
    session: _DiagnosticSession,
    diagnostic_path: Path,
    *,
    repeats: int = 2,
) -> diagnose_live_latency._LatencySummary:
    return asyncio.run(
        diagnose_live_latency._probe_session(
            cast('ClientSession', session),
            diagnostic_path,
            repeats,
        )
    )


def _safe_summary() -> dict[str, Any]:
    sample = {
        'operation': 'schema',
        'ordinal': 1,
        'round_trip_ms': 2.25,
        'server_ms': 2.375,
        'phases_ms': {
            'argument_validation': 0.125,
            'connection_lock_wait': 0.125,
            'schema_cache_lookup': 0.125,
            'schema_reflection': 1.25,
            'rendering': 0.25,
            'audit_write': 0.125,
        },
        'unattributed_ms': 0.375,
        'schema_cache': {'hits': 0, 'misses': 1, 'refreshes': 0},
        'provider_call_count': 2,
        'ok': True,
    }
    warm_schema = copy.deepcopy(sample)
    warm_schema['ordinal'] = 2
    warm_schema['server_ms'] = 1.125
    cast('dict[str, float]', warm_schema['phases_ms']).pop('schema_reflection')
    warm_schema['schema_cache'] = {'hits': 1, 'misses': 0, 'refreshes': 0}
    warm_schema['provider_call_count'] = 0
    describe = copy.deepcopy(warm_schema)
    describe['operation'] = 'describe'
    describe['ordinal'] = 1
    warm_describe = copy.deepcopy(describe)
    warm_describe['ordinal'] = 2
    query = copy.deepcopy(sample)
    query['operation'] = 'query'
    query['server_ms'] = 2.375
    query['phases_ms'] = {
        'argument_validation': 0.125,
        'connection_lock_wait': 0.125,
        'schema_cache_lookup': 0.25,
        'query_validation': 0.125,
        'provider_call': 1.0,
        'rendering': 0.25,
        'audit_write': 0.125,
    }
    query['schema_cache'] = {'hits': 1, 'misses': 0, 'refreshes': 0}
    query['provider_call_count'] = 1
    warm_query = copy.deepcopy(query)
    warm_query['ordinal'] = 2
    get = copy.deepcopy(query)
    get['operation'] = 'get'
    get['ordinal'] = 1
    stats = {'minimum': 2.25, 'median': 2.25, 'maximum': 2.25}
    return {
        'schema_version': 1,
        'repeats': 2,
        'schema_ttl_seconds': 3600,
        'default_limit': 50,
        'hard_limit': 200,
        'tool_names': ['sg_get', 'sg_query', 'sg_schema'],
        'setup_ms': {
            'process_spawn': 0.5,
            'mcp_initialize': 0.5,
            'tool_listing': 0.5,
        },
        'operation_counts': {'schema': 2, 'describe': 2, 'query': 2, 'get': 1},
        'samples': [
            sample,
            describe,
            query,
            warm_schema,
            warm_describe,
            warm_query,
            get,
        ],
        'aggregates_ms': {
            'schema_all': stats,
            'schema_warm': stats,
            'describe_all': stats,
            'describe_warm': stats,
            'query_all': stats,
            'query_warm': stats,
        },
        'project_found': True,
        'get_attempted': True,
        'get_ok': True,
        'valid': True,
    }


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SG_MCP_API_KEY', 'private-api-key-QX7V')
    monkeypatch.setenv('SG_MCP_SCRIPT_NAME', 'private-script-name-QX7V')
    monkeypatch.setenv('SG_MCP_SITE_URL', 'https://private-site-QX7V.example')


@pytest.mark.parametrize('repeats', [2, 7, 20])
def test_arguments_accept_default_and_bounded_repeat_counts(repeats: int) -> None:
    default = diagnose_live_latency._parse_arguments(['--output-dir', 'new-output'])
    selected = diagnose_live_latency._parse_arguments(
        ['--output-dir', 'new-output', '--repeats', str(repeats)]
    )

    assert default.repeats == 5
    assert selected.repeats == repeats
    assert selected.output_dir == Path('new-output')


@pytest.mark.parametrize('request_ids', [(8, 8), (8, 10), (8, 7)])
def test_cursor_rejects_duplicate_gapped_or_nonincreasing_request_ids(
    request_ids: tuple[int, int],
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / 'requests.jsonl'
    cursor = diagnose_live_latency._DiagnosticCursor(diagnostic_path)
    _append_record(
        diagnostic_path,
        _diagnostic_record(request_ids[0], 'sg_schema', cold=True),
    )
    cursor._take('sg_schema', 1)
    _append_record(diagnostic_path, _diagnostic_record(request_ids[1], 'sg_schema'))

    with pytest.raises(RuntimeError, match=r'^malformed diagnostic record$'):
        cursor._take('sg_schema', 2)


def test_cursor_rejects_millisecond_value_that_overflows_float(tmp_path: Path) -> None:
    diagnostic_path = tmp_path / 'requests.jsonl'
    line = json.dumps(_diagnostic_record(14, 'sg_schema', cold=True), separators=(',', ':'))
    line = line.replace('"duration_ms":2.375', '"duration_ms":1e309').replace(
        '"unattributed_ms":0.375',
        '"unattributed_ms":1e309',
    )
    diagnostic_path.write_text(line + '\n', encoding='utf-8')
    cursor = diagnose_live_latency._DiagnosticCursor(diagnostic_path)

    with pytest.raises(RuntimeError, match=r'^malformed diagnostic record$'):
        cursor._take('sg_schema', 1)


@pytest.mark.parametrize('mode', ['missing', 'extra', 'malformed'])
def test_cursor_rejects_missing_extra_or_malformed_records(
    mode: str,
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / 'requests.jsonl'
    if mode == 'extra':
        _append_record(diagnostic_path, _diagnostic_record(1, 'sg_schema', cold=True))
        _append_record(diagnostic_path, _diagnostic_record(2, 'sg_schema'))
    elif mode == 'malformed':
        diagnostic_path.write_text('{not-json}\n', encoding='utf-8')
    cursor_type = diagnose_live_latency._DiagnosticCursor
    cursor = cursor_type(diagnostic_path)

    with pytest.raises(RuntimeError, match=r'^malformed diagnostic record$'):
        cursor._take('sg_schema', 1)


@pytest.mark.parametrize(
    ('mutation', 'ordinal'),
    _RECORD_MUTATIONS,
    ids=[
        'boolean-schema-version',
        'unknown-top-level-field',
        'unknown-phase-field',
        'unknown-cache-field',
        'unapproved-outcome',
        'negative-duration',
        'negative-phase',
        'negative-cache-counter',
        'negative-provider-counter',
        'negative-unattributed',
        'nonreconciling-sum',
        'first-schema-without-miss',
        'first-schema-without-reflection',
        'warm-schema-with-reflection',
    ],
)
def test_cursor_rejects_unsafe_record_invariants(
    mutation: Callable[[dict[str, Any]], object],
    ordinal: int,
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / 'requests.jsonl'
    cursor = diagnose_live_latency._DiagnosticCursor(diagnostic_path)
    if ordinal == 2:
        _append_record(diagnostic_path, _diagnostic_record(14, 'sg_schema', cold=True))
        cursor._take('sg_schema', 1)
        record = _diagnostic_record(15, 'sg_schema')
    else:
        record = _diagnostic_record(14, 'sg_schema', cold=True)
    mutation(record)
    _append_record(diagnostic_path, record)

    with pytest.raises(RuntimeError, match=r'^malformed diagnostic record$'):
        cursor._take('sg_schema', ordinal)


def test_cursor_rejects_wrong_tool_order(tmp_path: Path) -> None:
    diagnostic_path = tmp_path / 'requests.jsonl'
    _append_record(diagnostic_path, _diagnostic_record(1, 'sg_query'))
    cursor = diagnose_live_latency._DiagnosticCursor(diagnostic_path)

    with pytest.raises(RuntimeError, match=r'^malformed diagnostic record$'):
        cursor._take('sg_schema', 1)


def test_diagnostic_server_uses_secret_only_for_connection_and_fixed_audit_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        'SG_MCP_SITE_URL': 'https://private-site-QX7V.example',
        'SG_MCP_SCRIPT_NAME': 'private-script-name-QX7V',
        'SG_MCP_API_KEY': 'private-api-key-QX7V',
    }
    connection = object()
    server = object()
    events: list[tuple[str, Config, object | None]] = []

    def fake_connect(config: Config) -> object:
        events.append(('connect', config, None))
        return connection

    def fake_build_server(config: Config, selected_connection: object, _surface: object) -> object:
        events.append(('build_server', config, selected_connection))
        return server

    monkeypatch.setattr(diagnose_live_latency, 'connect', fake_connect)
    monkeypatch.setattr(diagnose_live_latency, 'build_server', fake_build_server)
    selected = diagnose_live_latency._diagnostic_server(environment)

    assert selected is server
    assert [event[0] for event in events] == ['connect', 'build_server']
    authentication_config = events[0][1]
    audit_config = events[1][1]
    assert authentication_config.script_name == 'private-script-name-QX7V'
    assert audit_config == dataclasses.replace(
        authentication_config,
        script_name='live-diagnostic',
    )
    assert events[1][2] is connection


def test_diagnostic_summary_reports_literal_partitioned_aggregates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / 'requests.jsonl'
    session = _DiagnosticSession(diagnostic_path, repeats=3)
    durations_ms = (
        0.25,
        0.5,
        9.0,
        8.0,
        7.0,
        1.0,
        2.0,
        3.0,
        5.0,
        6.0,
        4.0,
        10.0,
    )
    ticks: list[int] = []
    elapsed_ns = 0
    for duration_ms in durations_ms:
        duration_ns = round(duration_ms * 1_000_000)
        ticks.extend((elapsed_ns, elapsed_ns + duration_ns))
        elapsed_ns += duration_ns
    clock = iter(ticks)
    monkeypatch.setattr(diagnose_live_latency.time, 'monotonic_ns', lambda: next(clock))

    summary = _probe(session, diagnostic_path, repeats=3)

    assert summary['operation_counts'] == {
        'schema': 3,
        'describe': 3,
        'query': 3,
        'get': 1,
    }
    assert summary['aggregates_ms'] == {
        'schema_all': {'minimum': 1.0, 'median': 5.0, 'maximum': 9.0},
        'schema_warm': {'minimum': 1.0, 'median': 3.0, 'maximum': 5.0},
        'describe_all': {'minimum': 2.0, 'median': 6.0, 'maximum': 8.0},
        'describe_warm': {'minimum': 2.0, 'median': 4.0, 'maximum': 6.0},
        'query_all': {'minimum': 3.0, 'median': 4.0, 'maximum': 7.0},
        'query_warm': {'minimum': 3.0, 'median': 3.5, 'maximum': 4.0},
    }
    assert 'private_field_QX7V' not in json.dumps(summary)


def test_existing_output_directory_is_refused_and_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_credentials(monkeypatch)
    output_dir = tmp_path / 'existing'
    output_dir.mkdir()
    marker = output_dir / 'keep-me'
    marker.write_text('original', encoding='utf-8')

    assert diagnose_live_latency._main(['--output-dir', str(output_dir)]) == 1

    assert marker.read_text(encoding='utf-8') == 'original'
    assert list(output_dir.iterdir()) == [marker]
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'live latency diagnostic failed: output directory already exists\n'
    assert str(output_dir) not in captured.err


@pytest.mark.parametrize('repeats', ['1', '21', 'not-an-integer'])
def test_invalid_repeat_count_fails_before_server_launch(
    repeats: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launched = False

    async def fail_if_launched(*args: object) -> dict[str, Any]:
        nonlocal launched
        del args
        launched = True
        raise AssertionError('server launch attempted')

    monkeypatch.setattr(diagnose_live_latency, '_run_live', fail_if_launched)

    with pytest.raises(SystemExit) as caught:
        diagnose_live_latency._main(['--output-dir', str(tmp_path / 'new'), '--repeats', repeats])

    assert caught.value.code == 2
    assert launched is False
    assert not (tmp_path / 'new').exists()


def test_main_bounds_malformed_diagnostic_without_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_credentials(monkeypatch)

    async def fail(*args: object) -> dict[str, Any]:
        del args
        raise diagnose_live_latency._MalformedDiagnosticError('malformed diagnostic record')

    monkeypatch.setattr(diagnose_live_latency, '_run_live', fail)

    assert diagnose_live_latency._main(['--output-dir', str(tmp_path / 'new')]) == 1

    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'live latency diagnostic failed: malformed diagnostic record\n'


def test_main_bounds_mcp_failure_without_raw_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_credentials(monkeypatch)

    async def fail(*args: object) -> dict[str, Any]:
        del args
        raise diagnose_live_latency.MCPError(
            code=-32603,
            message='private provider error QX7V',
        )

    monkeypatch.setattr(diagnose_live_latency, '_run_live', fail)

    assert diagnose_live_latency._main(['--output-dir', str(tmp_path / 'new')]) == 1

    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'live latency diagnostic failed: MCP request failed\n'
    assert 'private provider error QX7V' not in captured.err


def test_main_does_not_hide_unexpected_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_credentials(monkeypatch)

    async def fail(*args: object) -> dict[str, Any]:
        del args
        raise AssertionError('programming defect')

    monkeypatch.setattr(diagnose_live_latency, '_run_live', fail)

    with pytest.raises(AssertionError, match='programming defect'):
        diagnose_live_latency._main(['--output-dir', str(tmp_path / 'new')])


def test_main_resolution_failure_is_bounded_without_path_or_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_credentials(monkeypatch)
    adversarial_path = 'private-output-path-QX7V'

    def fail_resolution(path: Path) -> Path:
        assert str(path) == adversarial_path
        raise OSError('private resolution error QX7V')

    monkeypatch.setattr(Path, 'resolve', fail_resolution)

    assert diagnose_live_latency._main(['--output-dir', adversarial_path]) == 1

    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == (
        'live latency diagnostic failed: output directory could not be resolved\n'
    )
    assert adversarial_path not in captured.err
    assert 'private resolution error QX7V' not in captured.err


def test_main_resolution_runtime_failure_is_bounded_without_path_or_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_credentials(monkeypatch)
    adversarial_path = 'private-symlink-loop-path-QX7V'

    def fail_resolution(path: Path) -> Path:
        assert str(path) == adversarial_path
        raise RuntimeError('private symlink loop error QX7V')

    monkeypatch.setattr(Path, 'resolve', fail_resolution)

    assert diagnose_live_latency._main(['--output-dir', adversarial_path]) == 1

    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == (
        'live latency diagnostic failed: output directory could not be resolved\n'
    )
    assert adversarial_path not in captured.err
    assert 'private symlink loop error QX7V' not in captured.err


def test_main_writes_only_three_safe_artifacts_with_stable_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_credentials(monkeypatch)
    monkeypatch.setenv('UNRELATED_PRIVATE_VALUE', 'private-environment-QX7V')
    output_dir = tmp_path / 'new'

    async def fake_run_live(
        selected_output_dir: Path,
        repeats: int,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        assert repeats == 2
        assert environment['UNRELATED_PRIVATE_VALUE'] == 'private-environment-QX7V'
        (selected_output_dir / 'requests.jsonl').write_text(
            json.dumps(_diagnostic_record(982_451_653, 'sg_schema', cold=True)) + '\n',
            encoding='utf-8',
        )
        (selected_output_dir / 'security-audit.jsonl').write_text(
            '{"entity_ids":[982451653],"private":"private-audit-QX7V"}\n',
            encoding='utf-8',
        )
        return _safe_summary()

    monkeypatch.setattr(diagnose_live_latency, '_run_live', fake_run_live)

    assert diagnose_live_latency._main(['--output-dir', str(output_dir), '--repeats', '2']) == 0

    assert sorted(path.name for path in output_dir.iterdir()) == [
        'requests.jsonl',
        'security-audit.jsonl',
        'summary.json',
    ]
    summary_text = (output_dir / 'summary.json').read_text(encoding='utf-8')
    assert summary_text.endswith('\n')
    assert tuple(json.loads(summary_text)) == _EXPECTED_SUMMARY_KEYS
    assert '\n  "schema_version": 1,\n  "repeats": 2,' in summary_text
    for private_value in (
        '982451653',
        'private-audit-QX7V',
        'private-environment-QX7V',
        str(output_dir),
    ):
        assert private_value not in summary_text


def test_missing_credentials_report_all_names_without_values_or_directory_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in _REQUIRED_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('UNRELATED_PRIVATE_VALUE', 'private-environment-QX7V')
    output_dir = tmp_path / 'new'

    assert diagnose_live_latency._main(['--output-dir', str(output_dir)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == (
        'live latency diagnostic failed: missing required environment variables: '
        'SG_MCP_API_KEY, SG_MCP_SCRIPT_NAME, SG_MCP_SITE_URL\n'
    )
    assert 'private-environment-QX7V' not in captured.err
    assert not output_dir.exists()


def test_probe_record_invariants_accept_the_served_tools(
    fixture_site: FixtureConnection, tmp_path: Path
) -> None:
    project = fixture_site.create('Project', {'name': 'Probe'})
    server = build_test_server(fixture_site, tmp_path, diagnostics=True)
    for _ in range(2):
        call(server, 'sg_schema', {'op': 'entities'})
        call(server, 'sg_schema', {'op': 'describe', 'entity_type': 'Project'})
        call(
            server,
            'sg_query',
            {'entity_type': 'Project', 'filters': [], 'fields': ['name'], 'limit': 1},
        )
    call(
        server,
        'sg_get',
        {'entity_type': 'Project', 'entity_id': project['id'], 'fields': ['name']},
    )

    ordinals: dict[str, int] = {}
    for record in read_json_lines(tmp_path / 'diagnostics.jsonl'):
        ordinal = ordinals[record['tool']] = ordinals.get(record['tool'], 0) + 1
        diagnose_live_latency._operation_invariants(
            record['tool'],
            ordinal,
            record['phases_ms'],
            record['schema_cache'],
            record['provider_call_count'],
        )


@pytest.mark.parametrize(
    'get_content',
    [
        pytest.param({'records': []}, id='empty-records'),
        pytest.param({'records': [{'id': 982_451_653}]}, id='missing-type'),
        pytest.param(
            {'records': [{'type': 'Asset', 'id': 982_451_653}]},
            id='wrong-type',
        ),
        pytest.param({'records': [{'type': 'Project', 'id': 0}]}, id='nonpositive-id'),
        pytest.param({'records': [{'type': 'Project', 'id': True}]}, id='boolean-id'),
        pytest.param(
            {'records': [{'type': 'Project', 'id': 982_451_654}]},
            id='mismatched-id',
        ),
        pytest.param(
            {
                'records': [
                    {'type': 'Project', 'id': 982_451_653},
                    {'type': 'Project', 'id': 982_451_653},
                ]
            },
            id='multiple-records',
        ),
        pytest.param({'record': {'type': 'Project', 'id': 982_451_653}}, id='wrong-envelope'),
        pytest.param(
            {'records': {'type': 'Project', 'id': 982_451_653}},
            id='wrong-records-shape',
        ),
        pytest.param({'records': ['private-row-QX7V']}, id='wrong-row-shape'),
    ],
)
def test_probe_rejects_invalid_conditional_get_records(
    get_content: dict[str, Any],
    tmp_path: Path,
) -> None:

    def replace_get_content(name: str, structured_content: dict[str, Any]) -> None:
        if name == 'sg_get':
            structured_content.clear()
            structured_content.update(copy.deepcopy(get_content))

    diagnostic_path = tmp_path / 'requests.jsonl'
    session = _DiagnosticSession(
        diagnostic_path,
        response_mutator=replace_get_content,
    )

    with pytest.raises(RuntimeError) as caught:
        _probe(session, diagnostic_path)

    assert str(caught.value) == 'MCP request failed'
    assert '982451653' not in str(caught.value)
    assert 'private-row-QX7V' not in str(caught.value)


@pytest.mark.parametrize(
    'query_content',
    [
        pytest.param({'records': [{'id': 982_451_653}]}, id='missing-type'),
        pytest.param(
            {'records': [{'type': 'Asset', 'id': 982_451_653}]},
            id='wrong-type',
        ),
        pytest.param({'records': [{'type': 'Project', 'id': 0}]}, id='nonpositive-id'),
        pytest.param({'records': [{'type': 'Project', 'id': True}]}, id='boolean-id'),
        pytest.param(
            {
                'records': [
                    {'type': 'Project', 'id': 982_451_653},
                    {'type': 'Project', 'id': 982_451_654},
                ]
            },
            id='multiple-records',
        ),
        pytest.param({'record': {'type': 'Project', 'id': 982_451_653}}, id='wrong-envelope'),
        pytest.param(
            {'records': {'type': 'Project', 'id': 982_451_653}},
            id='wrong-records-shape',
        ),
        pytest.param({'records': ['private-row-QX7V']}, id='wrong-row-shape'),
    ],
)
def test_probe_rejects_invalid_project_query_rows(
    query_content: dict[str, Any],
    tmp_path: Path,
) -> None:

    def replace_query_content(name: str, structured_content: dict[str, Any]) -> None:
        if name == 'sg_query':
            structured_content.clear()
            structured_content.update(copy.deepcopy(query_content))

    diagnostic_path = tmp_path / 'requests.jsonl'
    session = _DiagnosticSession(
        diagnostic_path,
        response_mutator=replace_query_content,
    )

    with pytest.raises(RuntimeError) as caught:
        _probe(session, diagnostic_path)

    assert str(caught.value) == 'MCP request failed'
    assert '982451653' not in str(caught.value)
    assert 'private-row-QX7V' not in str(caught.value)


def test_probe_skips_get_when_first_query_has_no_project(tmp_path: Path) -> None:
    diagnostic_path = tmp_path / 'requests.jsonl'
    session = _DiagnosticSession(diagnostic_path, include_project=False)

    summary = _probe(session, diagnostic_path)

    assert len(session.calls) == 6
    assert summary['project_found'] is False
    assert summary['get_attempted'] is False
    assert summary['get_ok'] is None
    assert [sample['operation'] for sample in summary['samples']] == [
        'schema',
        'describe',
        'query',
        'schema',
        'describe',
        'query',
    ]


def test_probe_uses_fixed_sequence_in_one_session_and_returns_safe_summary(
    tmp_path: Path,
) -> None:
    diagnostic_path = tmp_path / 'requests.jsonl'
    session = _DiagnosticSession(diagnostic_path)

    summary = _probe(session, diagnostic_path)

    assert session.calls == [
        ('sg_schema', {'op': 'entities'}),
        ('sg_schema', _EXPECTED_DESCRIBE_ARGUMENTS),
        ('sg_query', _EXPECTED_QUERY_ARGUMENTS),
        ('sg_schema', {'op': 'entities'}),
        ('sg_schema', _EXPECTED_DESCRIBE_ARGUMENTS),
        ('sg_query', _EXPECTED_QUERY_ARGUMENTS),
        ('sg_get', _EXPECTED_GET_ARGUMENTS),
    ]
    assert tuple(summary) == _EXPECTED_SUMMARY_KEYS
    assert summary['repeats'] == 2
    assert summary['tool_names'] == ['sg_get', 'sg_query', 'sg_schema']
    assert [sample['operation'] for sample in summary['samples']] == [
        'schema',
        'describe',
        'query',
        'schema',
        'describe',
        'query',
        'get',
    ]
    assert [sample['ordinal'] for sample in summary['samples']] == [1, 1, 1, 2, 2, 2, 1]
    assert summary['project_found'] is True
    assert summary['get_attempted'] is True
    assert summary['get_ok'] is True
    assert summary['valid'] is True
    serialized = json.dumps(summary)
    for private_value in (
        '982451653',
        'private schema content',
        'private describe content',
        'private query content',
        'private get content',
        'private project name',
        'PrivateEntity',
        'private_field_QX7V',
        str(tmp_path),
    ):
        assert private_value not in serialized


def test_run_live_uses_one_stdio_transport_and_one_client_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / 'new'
    output_dir.mkdir()
    diagnostic_path = output_dir / 'requests.jsonl'
    session = _DiagnosticSession(diagnostic_path)
    stdio_context = _StdioContext()
    parameters_seen: list[StdioServerParameters] = []
    session_streams: list[tuple[object, object]] = []

    def fake_stdio_client(
        parameters: StdioServerParameters,
        **kwargs: object,
    ) -> _StdioContext:
        error_log = kwargs['errlog']
        assert isinstance(error_log, io.TextIOBase)
        assert not error_log.closed
        assert error_log.writable()
        parameters_seen.append(parameters)
        return stdio_context

    def fake_client_session(read_stream: object, write_stream: object) -> _DiagnosticSession:
        session_streams.append((read_stream, write_stream))
        return session

    monkeypatch.setattr(diagnose_live_latency, 'stdio_client', fake_stdio_client)
    monkeypatch.setattr(diagnose_live_latency, 'ClientSession', fake_client_session)
    summary = asyncio.run(
        diagnose_live_latency._run_live(output_dir, 2, {'PYTHONPATH': '/opaque'})
    )

    assert len(parameters_seen) == 1
    assert len(session_streams) == 1
    assert stdio_context.enter_count == stdio_context.exit_count == 1
    assert session.enter_count == session.exit_count == 1
    assert summary['setup_ms']['process_spawn'] >= 0.0


def test_server_parameters_use_checkout_module_and_isolated_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_credentials(monkeypatch)
    monkeypatch.setenv('PYTHONPATH', '/opaque/inherited/pythonpath')
    monkeypatch.setattr(diagnose_live_latency.sys, 'executable', '/opaque/python')
    output_dir = tmp_path / 'new'
    parameters = diagnose_live_latency._server_parameters(
        output_dir,
        diagnose_live_latency._required_environment(),
    )

    repository_root = Path(diagnose_live_latency.__file__).resolve().parents[1]
    assert parameters.command == '/opaque/python'
    assert parameters.args == [
        str(Path(diagnose_live_latency.__file__).resolve()),
        '--private-diagnostic-server',
    ]
    assert parameters.cwd == repository_root
    assert parameters.env is not None
    assert parameters.env['PYTHONPATH'] == (
        f'{repository_root / "src"}{os.pathsep}/opaque/inherited/pythonpath'
    )
    assert parameters.env['SG_MCP_DIAGNOSTICS_PATH'] == str(output_dir / 'requests.jsonl')
    assert parameters.env['SG_MCP_AUDIT_PATH'] == str(output_dir / 'security-audit.jsonl')
    assert parameters.env['SG_MCP_SCHEMA_TTL_SECONDS'] == '3600'
    assert parameters.env['SG_MCP_DEFAULT_LIMIT'] == '50'
    assert parameters.env['SG_MCP_HARD_LIMIT'] == '200'


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('schema_ttl_seconds', 3600.0),
        ('default_limit', 50.0),
        ('hard_limit', 200.0),
    ],
)
def test_summary_validation_rejects_float_configuration_limits(
    field: str,
    value: float,
) -> None:
    summary = _safe_summary()
    summary[field] = value
    with pytest.raises(RuntimeError, match=r'^malformed diagnostic record$'):
        diagnose_live_latency._validate_summary(summary)


@pytest.mark.parametrize(
    'mutation',
    _SUMMARY_MUTATIONS,
)
def test_summary_validation_rejects_raw_or_unknown_content(
    mutation: Callable[[dict[str, Any]], object],
) -> None:
    summary = _safe_summary()
    mutation(summary)
    with pytest.raises(RuntimeError, match=r'^malformed diagnostic record$'):
        diagnose_live_latency._validate_summary(summary)


def test_summary_write_rejects_unvalidated_content(tmp_path: Path) -> None:
    summary = _safe_summary()
    summary['raw_mcp_content'] = {'text': 'private-QX7V'}

    with pytest.raises(RuntimeError, match=r'^malformed diagnostic record$'):
        diagnose_live_latency._write_summary(
            tmp_path, cast('diagnose_live_latency._LatencySummary', summary)
        )

    assert not (tmp_path / 'summary.json').exists()
