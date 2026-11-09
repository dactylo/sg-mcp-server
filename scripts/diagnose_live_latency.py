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
from sg_mcp_server._config import Config, ConfigError
from sg_mcp_server._providers import connect
from sg_mcp_server._server import build_server

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
_SCHEMA_TTL_SECONDS = 3600

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

    def take(self, expected_tool: str, ordinal: int) -> _ValidatedDiagnostic:
        """Return the next safe diagnostic record for a completed client call.

        Args:
            expected_tool: Tool name of the completed call.
            ordinal: Position of the call within that tool's diagnostic
                sequence.

        Raises:
            RuntimeError: The appended record is missing, malformed, or
                mismatched.
        """
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
    # Describe shares sg_schema's diagnostic ordinals with the entities call before it, so
    # its record is checked against the warm-schema expectations.
    diagnostic_ordinal = ordinal + 1 if operation == 'describe' else ordinal
    record = cursor.take(tool, diagnostic_ordinal)
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
    """Validate and copy one cache-counter object with exactly the documented keys."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, object]', candidate)
    if set(mapping) != set(_CACHE_KEYS):
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
        description='Measure repeated safe reads through one live MCP stdio session.',
        epilog=(
            'Set SG_MCP_SITE_URL, SG_MCP_SCRIPT_NAME, and SG_MCP_API_KEY before running. '
            'See docs/diagnostics.md.'
        ),
    )
    parser.add_argument(
        '--output-dir',
        required=True,
        type=Path,
        help='new directory for the artifacts; it must not exist, and its parent must',
    )
    parser.add_argument(
        '--repeats',
        default=_DEFAULT_REPEATS,
        type=_bounded_repeats,
        help=(
            f'measurements per operation, {_MIN_REPEATS} to {_MAX_REPEATS} '
            f'(default: {_DEFAULT_REPEATS})'
        ),
    )
    return parser.parse_args(argv)


def _phase_values(candidate: object) -> tuple[dict[str, float], Decimal]:
    """Validate phase values and return them plus their exact sum."""
    if not isinstance(candidate, dict):
        _malformed()
    mapping = cast('dict[str, object]', candidate)
    phases: dict[str, float] = {}
    total = Decimal(0)
    for name, raw_value in mapping.items():
        phases[name] = _milliseconds(raw_value)
        total += cast('Decimal', raw_value)
    return phases, total


def _project_record_id(record: dict[str, object]) -> int:
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


def _server_environment(
    output_dir: Path,
    inherited_environment: Mapping[str, str],
) -> dict[str, str]:
    """Build the child environment with isolated artifacts and fixed limits."""
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
    return environment


def _server_parameters(
    output_dir: Path,
    inherited_environment: dict[str, str],
) -> StdioServerParameters:
    """Build exact current-checkout stdio parameters with isolated artifacts."""
    repository_root = Path(__file__).resolve().parents[1]
    return StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve()), _PRIVATE_SERVER_MODE],
        env=_server_environment(output_dir, inherited_environment),
        cwd=repository_root,
    )


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _malformed()
        result[key] = value
    return result


def _parse_record(line: str) -> dict[str, object]:
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
    return cast('dict[str, object]', candidate)


def _structured_mapping(result: types.CallToolResult) -> dict[str, object]:
    """Return structured MCP content without retaining it in an artifact."""
    structured = result.structured_content
    if not isinstance(structured, dict):
        raise _MCPRequestError('MCP request failed')
    return cast('dict[str, object]', structured)


def _project_records(result: types.CallToolResult) -> list[dict[str, object]]:
    """Return the common records envelope after validating its row shapes."""
    records = _structured_mapping(result).get('records')
    if not isinstance(records, list):
        raise _MCPRequestError('MCP request failed')
    values = cast('list[object]', records)
    if not all(isinstance(record, dict) for record in values):
        raise _MCPRequestError('MCP request failed')
    return cast('list[dict[str, object]]', values)


def _project_id(result: types.CallToolResult) -> int | None:
    """Return only the local Project id needed for the conditional safe get."""
    records = _project_records(result)
    if len(records) > 1:
        raise _MCPRequestError('MCP request failed')
    if not records:
        return None
    return _project_record_id(records[0])


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
    if not {
        'entity',
        'display_name',
        'fields',
        'total',
        'offset',
        'contains_untrusted_site_text',
    } <= set(structured):
        raise _MCPRequestError('MCP request failed')
    fields = structured['fields']
    field_values = cast('list[object]', fields) if isinstance(fields, list) else None
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
    record: dict[str, object],
    expected_tool: str,
    ordinal: int,
) -> tuple[int, _ValidatedDiagnostic]:
    """Validate one server record's documented fields and return its safe sample fields."""
    if (
        set(record) != set(_DIAGNOSTIC_KEYS)
        or type(record['schema_version']) is not int
        or record['schema_version'] != 1
    ):
        _malformed()
    _timestamp(record['ts'])
    request_id = record['request_id']
    if type(request_id) is not int or request_id <= 0:
        _malformed()
    tool = record['tool']
    if tool != expected_tool:
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
    _operation_invariants(expected_tool, ordinal, phases, cache, provider_call_count)
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
    values = cast('list[object]', entity_types)
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


def _write_summary(output_dir: Path, summary: _LatencySummary) -> None:
    """Write one stable safe summary with a trailing newline."""
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
            Config.from_env(_server_environment(output_dir, environment))
        except ConfigError as error:
            raise _ConfigurationError(str(error)) from error
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
