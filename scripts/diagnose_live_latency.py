"""Measure safe read latency through one long-lived live MCP session.

Run `python scripts/diagnose_live_latency.py --output-dir PATH` from the
repository root. `PATH` must not exist and its parent must exist.
`--repeats` accepts 2 through 20 measurements and defaults to 5.

Set `SG_MCP_SITE_URL`, `SG_MCP_SCRIPT_NAME`, and `SG_MCP_API_KEY` before
running. The script manages a diagnostic server subprocess and performs
read-only schema and Project queries through one stdio session.

The private output directory contains `requests.jsonl` and
`security-audit.jsonl`; successful completion also writes `summary.json`.
Artifacts contain bounded diagnostic metadata rather than raw tool results. The
command exits with zero on success, one on a handled diagnostic failure, or two
for invalid command-line arguments.
"""

import argparse
import asyncio
import dataclasses
import datetime
import json
import math
import os
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, NoReturn, TypedDict, cast

from mcp import ClientSession, MCPError, StdioServerParameters, stdio_server, types
from mcp.client.stdio import stdio_client
from mcp.server import Server

from sg_mcp_server import _tools
from sg_mcp_server._config import Config
from sg_mcp_server._providers import connect
from sg_mcp_server._server import build_server

_AGGREGATE_KEYS = (
    'schema_all',
    'schema_warm',
    'describe_all',
    'describe_warm',
    'query_all',
    'query_warm',
)
_ALLOWED_OUTCOMES = frozenset(
    {
        'invalid_arguments',
        'ok',
        'query_error',
        'unexpected_error',
        'unknown_tool',
        'validation_error',
    }
)
_ALLOWED_PHASES = frozenset(
    {
        'argument_validation',
        'audit_write',
        'connection_lock_wait',
        'provider_call',
        'query_validation',
        'rendering',
        'schema_cache_lookup',
        'schema_reflection',
    }
)
_CACHE_KEYS = ('hits', 'misses', 'refreshes')
_DEFAULT_LIMIT = 50
_DEFAULT_REPEATS = 5
_DESCRIBE_ARGUMENTS = {'op': 'describe', 'entity_type': 'Project'}
_DIAGNOSTIC_CALLER = 'live-diagnostic'
_DIAGNOSTIC_KEYS = (
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
)
_EXPECTED_TOOL_NAMES = ('sg_get', 'sg_query', 'sg_schema')
_HARD_LIMIT = 200
_MAX_REPEATS = 20
_MILLISECOND_DECIMAL_EXPONENT = -6
_MIN_REPEATS = 2
_OPERATION_PHASES = {
    'describe': frozenset(
        {
            'argument_validation',
            'audit_write',
            'connection_lock_wait',
            'rendering',
            'schema_cache_lookup',
        }
    ),
    'get': frozenset(
        {
            'argument_validation',
            'audit_write',
            'connection_lock_wait',
            'provider_call',
            'query_validation',
            'rendering',
            'schema_cache_lookup',
        }
    ),
    'query': frozenset(
        {
            'argument_validation',
            'audit_write',
            'connection_lock_wait',
            'provider_call',
            'query_validation',
            'rendering',
            'schema_cache_lookup',
        }
    ),
    'schema_cold': frozenset(
        {
            'argument_validation',
            'audit_write',
            'connection_lock_wait',
            'rendering',
            'schema_cache_lookup',
            'schema_reflection',
        }
    ),
    'schema_warm': frozenset(
        {
            'argument_validation',
            'audit_write',
            'connection_lock_wait',
            'rendering',
            'schema_cache_lookup',
        }
    ),
}
_PRIVATE_SERVER_MODE = '--private-diagnostic-server'
_QUERY_ARGUMENTS = {
    'entity_type': 'Project',
    'filters': [],
    'fields': ['name'],
    'order': [{'field_name': 'id', 'direction': 'asc'}],
    'limit': 1,
}
_RECONCILIATION_TOLERANCE = Decimal('0.00001')
_REQUIRED_ENVIRONMENT = (
    'SG_MCP_API_KEY',
    'SG_MCP_SCRIPT_NAME',
    'SG_MCP_SITE_URL',
)
_SAMPLE_KEYS = (
    'operation',
    'ordinal',
    'round_trip_ms',
    'server_ms',
    'phases_ms',
    'unattributed_ms',
    'schema_cache',
    'provider_call_count',
    'ok',
)
_SCHEMA_TTL_SECONDS = 3600
_SETUP_KEYS = ('process_spawn', 'mcp_initialize', 'tool_listing')
_STATS_KEYS = ('minimum', 'median', 'maximum')
_SUMMARY_KEYS = (
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

_Operation = Literal['schema', 'describe', 'query', 'get']


class _AggregateStats(TypedDict):
    """Contain minimum, median, and maximum round-trip measurements."""

    minimum: float
    median: float
    maximum: float


class _Aggregates(TypedDict):
    """Contain fixed aggregate groups for repeated read operations."""

    schema_all: _AggregateStats
    schema_warm: _AggregateStats
    describe_all: _AggregateStats
    describe_warm: _AggregateStats
    query_all: _AggregateStats
    query_warm: _AggregateStats


class _ArtifactError(RuntimeError):
    """Report an output artifact failure without exposing its path."""


class _ConfigurationError(RuntimeError):
    """Report invalid configuration without exposing environment values."""


class _MCPRequestError(RuntimeError):
    """Report a failed safe read without exposing MCP content."""


class _MalformedDiagnosticError(RuntimeError):
    """Report unsafe or inconsistent diagnostic data."""


class _OperationCounts(TypedDict):
    """Contain exact fixed live-operation counts."""

    schema: int
    describe: int
    query: int
    get: int


class _OutputExistsError(RuntimeError):
    """Report refusal to reuse an output directory."""


class _SchemaCacheCounts(TypedDict):
    """Contain bounded schema-cache counters for one request."""

    hits: int
    misses: int
    refreshes: int


class _OperationSample(TypedDict):
    """Contain one safe correlated client and server timing sample."""

    operation: _Operation
    ordinal: int
    round_trip_ms: float
    server_ms: float
    phases_ms: dict[str, float]
    unattributed_ms: float
    schema_cache: _SchemaCacheCounts
    provider_call_count: int
    ok: bool


class _SetupTimings(TypedDict):
    """Contain bounded client setup measurements in milliseconds."""

    process_spawn: float
    mcp_initialize: float
    tool_listing: float


class _LatencySummary(TypedDict):
    """Contain only the approved safe latency-summary fields."""

    schema_version: int
    repeats: int
    schema_ttl_seconds: int
    default_limit: int
    hard_limit: int
    tool_names: list[str]
    setup_ms: _SetupTimings
    operation_counts: _OperationCounts
    samples: list[_OperationSample]
    aggregates_ms: _Aggregates
    project_found: bool
    get_attempted: bool
    get_ok: bool | None
    valid: bool


class _ValidatedDiagnostic(TypedDict):
    """Contain the safe subset retained from one validated diagnostic line."""

    outcome: str
    duration_ms: float
    phases_ms: dict[str, float]
    schema_cache: _SchemaCacheCounts
    provider_call_count: int
    unattributed_ms: float


class _DiagnosticCursor:
    """Read and validate exactly one newly appended diagnostic line per call."""

    def __init__(self, path: Path) -> None:
        """Initialize a cursor for one append-only diagnostic path."""
        self._last_request_id: int | None = None
        self._offset = 0
        self._path = path

    def _take(self, expected_tool: str, ordinal: int) -> _ValidatedDiagnostic:
        """Return one safe record correlated to the completed client call."""
        try:
            with self._path.open('rb') as handle:
                handle.seek(self._offset)
                appended = handle.read()
                next_offset = handle.tell()
        except OSError:
            _malformed()
        if not appended.endswith(b'\n') or appended.count(b'\n') != 1:
            _malformed()
        try:
            line = appended[:-1].decode('utf-8')
        except UnicodeDecodeError:
            _malformed()
        request_id, safe_record = _validate_record(
            _parse_record(line),
            expected_tool,
            ordinal,
        )
        if self._last_request_id is not None and request_id != self._last_request_id + 1:
            _malformed()
        self._last_request_id = request_id
        self._offset = next_offset
        return safe_record


def _bounded_repeats(value: str) -> int:
    """Parse one repeat count within the maintained probe's fixed bounds."""
    try:
        repeats = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('repeats must be an integer from 2 through 20') from error
    if not _MIN_REPEATS <= repeats <= _MAX_REPEATS:
        raise argparse.ArgumentTypeError('repeats must be an integer from 2 through 20')
    return repeats


def _diagnostic_server(environment: Mapping[str, str]) -> Server:
    """Build a server that authenticates secretly and audits with a fixed caller."""
    authentication_config = Config.from_env(environment)
    connection = connect(authentication_config)
    audit_config = dataclasses.replace(
        authentication_config,
        script_name=_DIAGNOSTIC_CALLER,
    )
    return build_server(audit_config, connection, _tools.surface())


def _elapsed_ms(started_ns: int) -> float:
    """Return one six-decimal monotonic elapsed measurement."""
    return round((time.monotonic_ns() - started_ns) / 1_000_000, 6)


async def _call_sample(
    session: ClientSession,
    cursor: _DiagnosticCursor,
    operation: _Operation,
    ordinal: int,
    arguments: dict[str, Any],
) -> tuple[types.CallToolResult, _OperationSample]:
    """Call one fixed tool and correlate its newly appended diagnostic line."""
    tool = 'sg_schema' if operation == 'describe' else f'sg_{operation}'
    started_ns = time.monotonic_ns()
    result = await session.call_tool(tool, arguments)
    round_trip_ms = _elapsed_ms(started_ns)
    diagnostic_ordinal = ordinal + 1 if operation == 'describe' else ordinal
    record = cursor._take(tool, diagnostic_ordinal)  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    if result.is_error or record['outcome'] != 'ok':
        raise _MCPRequestError('MCP request failed')
    sample: _OperationSample = {
        'operation': operation,
        'ordinal': ordinal,
        'round_trip_ms': round_trip_ms,
        'server_ms': record['duration_ms'],
        'phases_ms': record['phases_ms'],
        'unattributed_ms': record['unattributed_ms'],
        'schema_cache': record['schema_cache'],
        'provider_call_count': record['provider_call_count'],
        'ok': True,
    }
    return result, sample


def _failure_message(error: BaseException) -> str | None:
    """Return one bounded message for expected direct or grouped failures."""
    message = None
    if isinstance(error, BaseExceptionGroup):
        message = _group_failure_message(cast('BaseExceptionGroup[BaseException]', error))
    elif isinstance(error, _ConfigurationError):
        message = f'live latency diagnostic failed: {error}'
    elif isinstance(error, _OutputExistsError):
        message = 'live latency diagnostic failed: output directory already exists'
    elif isinstance(error, _MalformedDiagnosticError):
        message = 'live latency diagnostic failed: malformed diagnostic record'
    elif isinstance(error, (_MCPRequestError, MCPError)):
        message = 'live latency diagnostic failed: MCP request failed'
    elif isinstance(error, _ArtifactError):
        message = f'live latency diagnostic failed: {error}'
    elif isinstance(error, OSError):
        message = 'live latency diagnostic failed: server process could not start'
    return message


def _group_failure_message(error: BaseExceptionGroup[BaseException]) -> str | None:
    """Collapse one expected exception group to a single bounded message."""
    messages = [_failure_message(nested) for nested in error.exceptions]
    message = None
    if not any(candidate is None for candidate in messages):
        if 'live latency diagnostic failed: MCP request failed' in messages:
            message = 'live latency diagnostic failed: MCP request failed'
        else:
            message = next(candidate for candidate in messages if candidate is not None)
    return message


def _malformed() -> NoReturn:
    """Raise the single bounded malformed-record failure."""
    raise _MalformedDiagnosticError('malformed diagnostic record')


def _milliseconds(candidate: object) -> float:
    """Return one finite nonnegative value with at most six decimal places."""
    if not isinstance(candidate, Decimal):
        _malformed()
    exponent = candidate.as_tuple().exponent
    if not candidate.is_finite() or candidate < 0 or not isinstance(exponent, int):
        _malformed()
    if exponent < _MILLISECOND_DECIMAL_EXPONENT:
        _malformed()
    milliseconds = float(candidate)
    if not math.isfinite(milliseconds):
        _malformed()
    return milliseconds


def _nonnegative_counter(candidate: object) -> int:
    """Return one exact nonnegative integer counter."""
    if type(candidate) is not int or candidate < 0:
        _malformed()
    return candidate


def _cache_counts(candidate: object) -> _SchemaCacheCounts:
    """Validate and copy one exact cache-counter object."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, Any]', candidate)
    if tuple(mapping) != _CACHE_KEYS:
        _malformed()
    return {
        'hits': _nonnegative_counter(mapping['hits']),
        'misses': _nonnegative_counter(mapping['misses']),
        'refreshes': _nonnegative_counter(mapping['refreshes']),
    }


def _operation_invariants(
    tool: str,
    ordinal: int,
    phases: dict[str, float],
    cache: _SchemaCacheCounts,
    provider_call_count: int,
) -> None:
    """Enforce exact phase, cache, and provider work for the fixed probe."""
    if tool == 'sg_schema' and ordinal == 1:
        expected_phases = _OPERATION_PHASES['schema_cold']
        expected_cache = {'hits': 0, 'misses': 1, 'refreshes': 0}
        expected_provider_calls = 2
    elif tool == 'sg_schema':
        expected_phases = _OPERATION_PHASES['schema_warm']
        expected_cache = {'hits': 1, 'misses': 0, 'refreshes': 0}
        expected_provider_calls = 0
    else:
        expected_phases = _OPERATION_PHASES[tool.removeprefix('sg_')]
        expected_cache = {'hits': 1, 'misses': 0, 'refreshes': 0}
        expected_provider_calls = 1
    if (
        frozenset(phases) != expected_phases
        or cache != expected_cache
        or provider_call_count != expected_provider_calls
    ):
        _malformed()


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the fixed live diagnostic command-line interface."""
    parser = argparse.ArgumentParser(
        description='Measure repeated safe reads through one live MCP stdio session.'
    )
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--repeats', default=_DEFAULT_REPEATS, type=_bounded_repeats)
    return parser.parse_args(argv)


def _phase_values(candidate: object) -> tuple[dict[str, float], Decimal]:
    """Validate fixed phase names and return safe values plus their exact sum."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, Any]', candidate)
    if not set(mapping).issubset(_ALLOWED_PHASES):
        _malformed()
    phases: dict[str, float] = {}
    total = Decimal(0)
    for name, raw_value in mapping.items():
        phases[name] = _milliseconds(raw_value)
        total += cast('Decimal', raw_value)
    return phases, total


def _project_record_id(record: dict[str, Any]) -> int:
    """Return one positive Project id from a validated row shape."""
    candidate = record.get('id')
    if record.get('type') != 'Project' or type(candidate) is not int or candidate <= 0:
        raise _MCPRequestError('MCP request failed')
    return candidate


def _required_environment() -> dict[str, str]:
    """Return a copied environment or report every missing credential name."""
    missing = sorted(name for name in _REQUIRED_ENVIRONMENT if not os.environ.get(name))
    if missing:
        names = ', '.join(missing)
        raise _ConfigurationError(f'missing required environment variables: {names}')
    return dict(os.environ)


async def _serve_diagnostic_server(environment: Mapping[str, str]) -> None:
    """Serve the private diagnostic mode over stdio."""
    server = _diagnostic_server(environment)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _server_parameters(
    output_dir: Path,
    inherited_environment: dict[str, str],
) -> StdioServerParameters:
    """Build exact current-checkout stdio parameters with isolated artifacts."""
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(inherited_environment)
    environment['SG_MCP_DIAGNOSTICS_PATH'] = str(output_dir / 'requests.jsonl')
    environment['SG_MCP_AUDIT_PATH'] = str(output_dir / 'security-audit.jsonl')
    environment['SG_MCP_SCHEMA_TTL_SECONDS'] = str(_SCHEMA_TTL_SECONDS)
    environment['SG_MCP_DEFAULT_LIMIT'] = str(_DEFAULT_LIMIT)
    environment['SG_MCP_HARD_LIMIT'] = str(_HARD_LIMIT)
    source_path = str(repository_root / 'src')
    inherited_pythonpath = environment.get('PYTHONPATH')
    environment['PYTHONPATH'] = (
        f'{source_path}{os.pathsep}{inherited_pythonpath}' if inherited_pythonpath else source_path
    )
    return StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve()), _PRIVATE_SERVER_MODE],
        env=environment,
        cwd=repository_root,
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _malformed()
        result[key] = value
    return result


def _parse_record(line: str) -> dict[str, Any]:
    """Parse one diagnostic line with exact numeric and object preservation."""
    try:
        candidate = json.loads(
            line,
            parse_float=Decimal,
            object_pairs_hook=_strict_object,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        _malformed()
    if not isinstance(candidate, dict):
        _malformed()
    return cast('dict[str, Any]', candidate)


def _structured_mapping(result: types.CallToolResult) -> dict[str, Any]:
    """Return structured MCP content without retaining it in an artifact."""
    structured = result.structured_content
    if not isinstance(structured, dict):
        raise _MCPRequestError('MCP request failed')
    return cast('dict[str, Any]', structured)


def _project_records(result: types.CallToolResult) -> list[dict[str, Any]]:
    """Return the common records envelope after validating its row shapes."""
    records = _structured_mapping(result).get('records')
    if not isinstance(records, list):
        raise _MCPRequestError('MCP request failed')
    values = cast('list[Any]', records)
    if not all(isinstance(record, dict) for record in values):
        raise _MCPRequestError('MCP request failed')
    return cast('list[dict[str, Any]]', values)


def _project_id(result: types.CallToolResult) -> int | None:
    """Return only the local Project id needed for the conditional safe get."""
    records = _project_records(result)
    if len(records) > 1:
        raise _MCPRequestError('MCP request failed')
    if not records:
        return None
    return _project_record_id(records[0])


def _summary_milliseconds(candidate: object) -> float:
    """Validate a nonnegative finite summary timing value."""
    if type(candidate) is not float or not math.isfinite(candidate) or candidate < 0:
        _malformed()
    if round(candidate, 6) != candidate:
        _malformed()
    return candidate


def _summary_stats(values: list[float]) -> _AggregateStats:
    """Aggregate one nonempty fixed operation sample group."""
    return {
        'minimum': round(min(values), 6),
        'median': round(statistics.median(values), 6),
        'maximum': round(max(values), 6),
    }


def _summary_aggregates(samples: list[_OperationSample]) -> _Aggregates:
    """Aggregate client round trips for fixed cold and warm groups."""
    schema = [sample['round_trip_ms'] for sample in samples if sample['operation'] == 'schema']
    describe = [sample['round_trip_ms'] for sample in samples if sample['operation'] == 'describe']
    query = [sample['round_trip_ms'] for sample in samples if sample['operation'] == 'query']
    return {
        'schema_all': _summary_stats(schema),
        'schema_warm': _summary_stats(schema[1:]),
        'describe_all': _summary_stats(describe),
        'describe_warm': _summary_stats(describe[1:]),
        'query_all': _summary_stats(query),
        'query_warm': _summary_stats(query[1:]),
    }


def _timestamp(candidate: object) -> None:
    """Validate one UTC ISO-8601 timestamp without retaining it."""
    if not isinstance(candidate, str):
        _malformed()
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        _malformed()
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        _malformed()


def _validate_artifact_set(output_dir: Path, *, summary_written: bool) -> None:
    """Require exactly the approved regular files in the new output directory."""
    expected = {'requests.jsonl', 'security-audit.jsonl'}
    if summary_written:
        expected.add('summary.json')
    try:
        entries = {path.name: path for path in output_dir.iterdir()}
    except OSError as error:
        raise _ArtifactError('artifact validation failed') from error
    if set(entries) != expected or not all(path.is_file() for path in entries.values()):
        raise _ArtifactError('artifact validation failed')


def _validate_describe_result(result: types.CallToolResult) -> None:
    """Validate the Project describe shape without retaining field content."""
    structured = _structured_mapping(result)
    if set(structured) != {
        'entity',
        'display_name',
        'fields',
        'total',
        'offset',
        'contains_untrusted_site_text',
    }:
        raise _MCPRequestError('MCP request failed')
    fields = structured['fields']
    field_values = cast('list[Any]', fields) if isinstance(fields, list) else None
    if (
        structured['entity'] != 'Project'
        or not isinstance(structured['display_name'], str)
        or field_values is None
        or not all(isinstance(field, dict) for field in field_values)
        or type(structured['total']) is not int
        or structured['total'] < len(field_values)
        or structured['offset'] != 0
        or structured['contains_untrusted_site_text'] is not True
    ):
        raise _MCPRequestError('MCP request failed')


def _validate_project_get_result(result: types.CallToolResult, expected_id: int) -> None:
    """Require one returned Project whose id matches the requested id."""
    records = _project_records(result)
    if len(records) != 1 or _project_record_id(records[0]) != expected_id:
        raise _MCPRequestError('MCP request failed')


def _validate_record(
    record: dict[str, Any],
    expected_tool: str,
    ordinal: int,
) -> tuple[int, _ValidatedDiagnostic]:
    """Validate one exact server record and return only its safe sample fields."""
    if (
        tuple(record) != _DIAGNOSTIC_KEYS
        or type(record['schema_version']) is not int
        or record['schema_version'] != 1
    ):
        _malformed()
    _timestamp(record['ts'])
    request_id = record['request_id']
    if type(request_id) is not int or request_id <= 0:
        _malformed()
    tool = record['tool']
    if tool not in _EXPECTED_TOOL_NAMES or tool != expected_tool:
        _malformed()
    outcome = record['outcome']
    if not isinstance(outcome, str) or outcome not in _ALLOWED_OUTCOMES:
        _malformed()
    duration_ms = _milliseconds(record['duration_ms'])
    phases, phase_total = _phase_values(record['phases_ms'])
    cache = _cache_counts(record['schema_cache'])
    provider_call_count = _nonnegative_counter(record['provider_call_count'])
    unattributed_ms = _milliseconds(record['unattributed_ms'])
    raw_duration = cast('Decimal', record['duration_ms'])
    raw_unattributed = cast('Decimal', record['unattributed_ms'])
    if abs(raw_duration - phase_total - raw_unattributed) > _RECONCILIATION_TOLERANCE:
        _malformed()
    _operation_invariants(tool, ordinal, phases, cache, provider_call_count)
    return request_id, {
        'outcome': outcome,
        'duration_ms': duration_ms,
        'phases_ms': phases,
        'schema_cache': cache,
        'provider_call_count': provider_call_count,
        'unattributed_ms': unattributed_ms,
    }


def _validate_schema_result(result: types.CallToolResult) -> None:
    """Validate the schema response shape without retaining any site data."""
    entity_types = _structured_mapping(result).get('entity_types')
    if not isinstance(entity_types, list):
        raise _MCPRequestError('MCP request failed')
    values = cast('list[Any]', entity_types)
    if not all(isinstance(entity_type, str) for entity_type in values):
        raise _MCPRequestError('MCP request failed')


async def _probe_session(
    session: ClientSession,
    diagnostic_path: Path,
    repeats: int,
    process_spawn_ms: float = 0.0,
) -> _LatencySummary:
    """Run the fixed safe read sequence through one initialized MCP session."""
    initialize_started_ns = time.monotonic_ns()
    await session.initialize()
    mcp_initialize_ms = _elapsed_ms(initialize_started_ns)

    listing_started_ns = time.monotonic_ns()
    listed = await session.list_tools()
    tool_listing_ms = _elapsed_ms(listing_started_ns)
    tool_names = sorted(tool.name for tool in listed.tools)
    if tool_names != list(_EXPECTED_TOOL_NAMES):
        raise _MCPRequestError('MCP request failed')

    cursor = _DiagnosticCursor(diagnostic_path)
    samples: list[_OperationSample] = []
    project_id: int | None = None
    for ordinal in range(1, repeats + 1):
        result, sample = await _call_sample(
            session,
            cursor,
            'schema',
            ordinal,
            {'op': 'entities'},
        )
        _validate_schema_result(result)
        samples.append(sample)
        result, sample = await _call_sample(
            session,
            cursor,
            'describe',
            ordinal,
            _DESCRIBE_ARGUMENTS,
        )
        _validate_describe_result(result)
        samples.append(sample)
        result, sample = await _call_sample(
            session,
            cursor,
            'query',
            ordinal,
            _QUERY_ARGUMENTS,
        )
        candidate_id = _project_id(result)
        if ordinal == 1:
            project_id = candidate_id
        samples.append(sample)

    get_ok: bool | None = None
    if project_id is not None:
        result, sample = await _call_sample(
            session,
            cursor,
            'get',
            1,
            {'entity_type': 'Project', 'entity_id': project_id, 'fields': ['name']},
        )
        _validate_project_get_result(result, project_id)
        samples.append(sample)
        get_ok = True

    setup_ms: _SetupTimings = {
        'process_spawn': process_spawn_ms,
        'mcp_initialize': mcp_initialize_ms,
        'tool_listing': tool_listing_ms,
    }
    project_found = project_id is not None
    operation_counts: _OperationCounts = {
        'schema': sum(sample['operation'] == 'schema' for sample in samples),
        'describe': sum(sample['operation'] == 'describe' for sample in samples),
        'query': sum(sample['operation'] == 'query' for sample in samples),
        'get': sum(sample['operation'] == 'get' for sample in samples),
    }
    summary: _LatencySummary = {
        'schema_version': 1,
        'repeats': repeats,
        'schema_ttl_seconds': _SCHEMA_TTL_SECONDS,
        'default_limit': _DEFAULT_LIMIT,
        'hard_limit': _HARD_LIMIT,
        'tool_names': list(_EXPECTED_TOOL_NAMES),
        'setup_ms': setup_ms,
        'operation_counts': operation_counts,
        'samples': samples,
        'aggregates_ms': _summary_aggregates(samples),
        'project_found': project_found,
        'get_attempted': project_found,
        'get_ok': get_ok,
        'valid': True,
    }
    return summary


async def _run_live(
    output_dir: Path,
    repeats: int,
    environment: dict[str, str],
) -> _LatencySummary:
    """Launch one checkout process and probe one long-lived stdio session."""
    parameters = _server_parameters(output_dir, environment)
    with await asyncio.to_thread(Path(os.devnull).open, 'w', encoding='utf-8') as error_log:
        spawn_started_ns = time.monotonic_ns()
        async with stdio_client(parameters, errlog=error_log) as (read_stream, write_stream):
            process_spawn_ms = _elapsed_ms(spawn_started_ns)
            async with ClientSession(read_stream, write_stream) as session:
                return await _probe_session(
                    session,
                    output_dir / 'requests.jsonl',
                    repeats,
                    process_spawn_ms,
                )


def _validate_stats(candidate: object) -> _AggregateStats:
    """Validate one exact aggregate-statistics object."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, Any]', candidate)
    if tuple(mapping) != _STATS_KEYS:
        _malformed()
    stats: _AggregateStats = {
        'minimum': _summary_milliseconds(mapping['minimum']),
        'median': _summary_milliseconds(mapping['median']),
        'maximum': _summary_milliseconds(mapping['maximum']),
    }
    if not stats['minimum'] <= stats['median'] <= stats['maximum']:
        _malformed()
    return stats


def _validate_summary_aggregates(candidate: object) -> None:
    """Validate the closed aggregate shape without recomputing generated values."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, Any]', candidate)
    if tuple(mapping) != _AGGREGATE_KEYS:
        _malformed()
    for value in mapping.values():
        _validate_stats(value)


def _validate_summary_flags(summary: dict[str, Any]) -> None:
    """Validate the fixed Project/get relationship and expected validity state."""
    project_found = summary['project_found']
    get_attempted = summary['get_attempted']
    get_ok = summary['get_ok']
    if type(project_found) is not bool or type(get_attempted) is not bool:
        _malformed()
    if get_ok is not None and type(get_ok) is not bool:
        _malformed()
    invalid_relationship = project_found != get_attempted or (get_attempted and get_ok is not True)
    if invalid_relationship or (not get_attempted and get_ok is not None):
        _malformed()
    if summary['valid'] is not True:
        _malformed()


def _validate_summary_header(summary: dict[str, Any]) -> int:
    """Validate fixed summary metadata and return its repeat count."""
    if tuple(summary) != _SUMMARY_KEYS:
        _malformed()
    repeats = summary['repeats']
    if (
        type(summary['schema_version']) is not int
        or summary['schema_version'] != 1
        or type(repeats) is not int
        or not _MIN_REPEATS <= repeats <= _MAX_REPEATS
        or type(summary['schema_ttl_seconds']) is not int
        or summary['schema_ttl_seconds'] != _SCHEMA_TTL_SECONDS
        or type(summary['default_limit']) is not int
        or summary['default_limit'] != _DEFAULT_LIMIT
        or type(summary['hard_limit']) is not int
        or summary['hard_limit'] != _HARD_LIMIT
        or summary['tool_names'] != list(_EXPECTED_TOOL_NAMES)
    ):
        _malformed()
    return repeats


def _validate_summary_operation_counts(
    candidate: object,
    samples: list[_OperationSample],
) -> None:
    """Validate exact operation counts against the retained samples."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, Any]', candidate)
    if tuple(mapping) != ('schema', 'describe', 'query', 'get'):
        _malformed()
    counts = {
        operation: sum(sample['operation'] == operation for sample in samples)
        for operation in ('schema', 'describe', 'query', 'get')
    }
    if any(type(value) is not int for value in mapping.values()) or mapping != counts:
        _malformed()


def _validate_summary_sample(candidate: object) -> _OperationSample:
    """Validate one closed safe-summary sample object."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, Any]', candidate)
    if tuple(mapping) != _SAMPLE_KEYS:
        _malformed()
    operation = mapping['operation']
    if operation not in ('schema', 'describe', 'query', 'get'):
        _malformed()
    ordinal = mapping['ordinal']
    if type(ordinal) is not int or ordinal <= 0:
        _malformed()
    phases_candidate = mapping['phases_ms']
    if not isinstance(phases_candidate, dict):
        _malformed()
    phase_mapping = cast('dict[str, Any]', phases_candidate)
    if not set(phase_mapping).issubset(_ALLOWED_PHASES):
        _malformed()
    phases = {name: _summary_milliseconds(value) for name, value in phase_mapping.items()}
    server_ms = _summary_milliseconds(mapping['server_ms'])
    unattributed_ms = _summary_milliseconds(mapping['unattributed_ms'])
    if abs(server_ms - sum(phases.values()) - unattributed_ms) > float(_RECONCILIATION_TOLERANCE):
        _malformed()
    cache = _cache_counts(mapping['schema_cache'])
    provider_call_count = _nonnegative_counter(mapping['provider_call_count'])
    if type(mapping['ok']) is not bool:
        _malformed()
    return {
        'operation': operation,
        'ordinal': ordinal,
        'round_trip_ms': _summary_milliseconds(mapping['round_trip_ms']),
        'server_ms': server_ms,
        'phases_ms': phases,
        'unattributed_ms': unattributed_ms,
        'schema_cache': cache,
        'provider_call_count': provider_call_count,
        'ok': mapping['ok'],
    }


def _validate_summary_samples(
    summary: dict[str, Any],
    repeats: int,
) -> list[_OperationSample]:
    """Validate the exact fixed sample sequence and success flags."""
    raw_samples = summary['samples']
    if not isinstance(raw_samples, list):
        _malformed()
    samples = [_validate_summary_sample(sample) for sample in cast('list[Any]', raw_samples)]
    expected_operations = [
        operation
        for ordinal in range(1, repeats + 1)
        for operation in (
            ('schema', ordinal),
            ('describe', ordinal),
            ('query', ordinal),
        )
    ]
    if summary['get_attempted'] is True:
        expected_operations.append(('get', 1))
    actual_operations = [(sample['operation'], sample['ordinal']) for sample in samples]
    if actual_operations != expected_operations or not all(sample['ok'] for sample in samples):
        _malformed()
    return samples


def _validate_summary_setup(candidate: object) -> None:
    """Validate the exact safe setup-timing object."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, Any]', candidate)
    if tuple(mapping) != _SETUP_KEYS:
        _malformed()
    for value in mapping.values():
        _summary_milliseconds(value)


def _validate_summary(summary: dict[str, Any]) -> None:
    """Validate a complete summary at the serialization boundary."""
    repeats = _validate_summary_header(summary)
    _validate_summary_setup(summary['setup_ms'])
    samples = _validate_summary_samples(summary, repeats)
    _validate_summary_operation_counts(summary['operation_counts'], samples)
    _validate_summary_aggregates(summary['aggregates_ms'])
    _validate_summary_flags(summary)


def _write_summary(output_dir: Path, summary: _LatencySummary) -> None:
    """Validate and write one stable safe summary with a trailing newline."""
    _validate_summary(cast('dict[str, Any]', summary))
    try:
        (output_dir / 'summary.json').write_text(
            json.dumps(summary, indent=2) + '\n',
            encoding='utf-8',
        )
    except OSError as error:
        raise _ArtifactError('summary write failed') from error


def _main(argv: Sequence[str] | None = None) -> int:
    """Run the maintained live latency probe."""
    if argv is not None and tuple(argv) == (_PRIVATE_SERVER_MODE,):
        asyncio.run(_serve_diagnostic_server(os.environ))
        return 0
    arguments = _parse_arguments(argv)
    try:
        try:
            output_dir = arguments.output_dir.resolve()
        except (OSError, RuntimeError) as error:
            raise _ArtifactError('output directory could not be resolved') from error
        if output_dir.exists():
            raise _OutputExistsError
        environment = _required_environment()
        try:
            output_dir.mkdir()
        except FileExistsError as error:
            raise _OutputExistsError from error
        except OSError as error:
            raise _ArtifactError('output directory could not be created') from error
        summary = asyncio.run(_run_live(output_dir, arguments.repeats, environment))
        _validate_artifact_set(output_dir, summary_written=False)
        _write_summary(output_dir, summary)
        _validate_artifact_set(output_dir, summary_written=True)
    except (
        BaseExceptionGroup,
        MCPError,
        OSError,
        RuntimeError,
    ) as error:
        message = _failure_message(error)
        if message is None:
            raise
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv[1:]))
