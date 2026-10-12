"""Fail-open latency diagnostic collection and JSONL output."""

from __future__ import annotations

import contextlib
import datetime
import itertools
import json
import sys
import threading
import time
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator
    from pathlib import Path

    from sg_mcp_server._engine._schema import SchemaCache, SiteSchema
    from sg_mcp_server._providers import (
        FindOptions,
        RawSchema,
        SGReadConnection,
        SGRecord,
        SummaryField,
        SummaryGrouping,
        SummaryResponse,
        ValidatedFilters,
    )

DiagnosticOutcome = Literal[
    'ok',
    'validation_error',
    'query_error',
    'unexpected_error',
    'invalid_arguments',
    'unknown_tool',
]
_DiagnosticPhase = Literal[
    'argument_validation',
    'connection_lock_wait',
    'schema_cache_lookup',
    'schema_reflection',
    'query_validation',
    'provider_call',
    'rendering',
    'audit_write',
]
_FailureCategory = Literal['diagnostic accounting failed', 'diagnostic write failed']
_REQUEST_IDS = itertools.count(1)
_SCHEMA_VERSION = 1
_STATE_LOCK = threading.Lock()
_WARNING_CATEGORIES: set[_FailureCategory] = set()
_WRITE_LOCK = threading.Lock()


class _SchemaCacheCounts(TypedDict):
    """Schema-cache observations for one request."""

    hits: int
    misses: int
    refreshes: int


class _DiagnosticRecord(TypedDict):
    """One stable diagnostic JSON record."""

    schema_version: int
    ts: str
    request_id: int
    tool: str
    outcome: DiagnosticOutcome
    duration_ms: float
    phases_ms: dict[str, float]
    schema_cache: _SchemaCacheCounts
    provider_call_count: int
    unattributed_ms: float


class DiagnosticContext:
    """Accumulate exclusive timing spans and bounded request counters."""

    def __init__(
        self,
        request_id: int,
        tool: str,
        started_ns: int,
        clock: Callable[[], int] = time.monotonic_ns,
        *,
        enabled: bool = True,
    ) -> None:
        """Initialize request-scoped diagnostic accounting.

        Args:
            request_id: Positive process-local identifier for an enabled
                request; ignored when diagnostics are disabled.
            tool: Fixed tool name recorded for the request.
            started_ns: Monotonic request-start timestamp in nanoseconds.
            clock: Monotonic nanosecond clock used for measured spans.
            enabled: Whether to collect and emit diagnostic data.
        """
        self._cache_counts: _SchemaCacheCounts = {'hits': 0, 'misses': 0, 'refreshes': 0}
        self._clock = clock
        self._enabled = enabled
        self._outcome: DiagnosticOutcome = 'unexpected_error'
        self._phases_ns: dict[_DiagnosticPhase, int] = {}
        self._provider_call_count = 0
        self._request_id = request_id
        self._span_stack: list[tuple[_DiagnosticPhase, int]] = []
        self._started_ns = started_ns
        self._tool = tool

    def _accrue(self, phase: _DiagnosticPhase, elapsed_ns: int) -> None:
        """Accumulate integer nanoseconds for one phase."""
        self._phases_ns[phase] = self._phases_ns.get(phase, 0) + elapsed_ns

    def _finalize(self) -> _DiagnosticRecord | None:
        """Create the stable record without performing any file I/O."""
        if not self._enabled:
            return None
        finished_ns = self._clock()
        duration_ns = finished_ns - self._started_ns
        remainder_ns = duration_ns - sum(self._phases_ns.values())
        if remainder_ns < 0:
            remainder_ns = 0
            _warn_once('diagnostic accounting failed')
        return {
            'schema_version': _SCHEMA_VERSION,
            'ts': datetime.datetime.now(datetime.UTC).isoformat(),
            'request_id': self._request_id,
            'tool': self._tool,
            'outcome': self._outcome,
            'duration_ms': round(duration_ns / 1_000_000, 6),
            'phases_ms': {
                phase: round(elapsed_ns / 1_000_000, 6)
                for phase, elapsed_ns in self._phases_ns.items()
            },
            'schema_cache': self._cache_counts.copy(),
            'provider_call_count': self._provider_call_count,
            'unattributed_ms': round(remainder_ns / 1_000_000, 6),
        }

    @contextlib.contextmanager
    def _span(self, phase: _DiagnosticPhase) -> Generator[None, None, None]:
        """Accrue one enabled phase while maintaining the active stack."""
        # Entry and exit must not raise: _ServerAdapter._call_handler acquires the connection
        # lock inside a span, before entering the try/finally that releases it.
        entered = False
        with contextlib.suppress(Exception):
            started_ns = self._clock()
            if self._span_stack:
                parent_phase, parent_started_ns = self._span_stack[-1]
                self._accrue(parent_phase, started_ns - parent_started_ns)
            self._span_stack.append((phase, started_ns))
            entered = True
        if not entered:
            _warn_once('diagnostic accounting failed')
        try:
            yield
        finally:
            if entered:
                exited = False
                with contextlib.suppress(Exception):
                    finished_ns = self._clock()
                    active_phase, active_started_ns = self._span_stack.pop()
                    self._accrue(active_phase, finished_ns - active_started_ns)
                    if self._span_stack:
                        parent_phase, _ = self._span_stack[-1]
                        self._span_stack[-1] = (parent_phase, finished_ns)
                    exited = True
                if not exited:
                    _warn_once('diagnostic accounting failed')

    def cache_hit(self) -> None:
        """Record one unexpired schema-cache hit."""
        if self._enabled:
            self._cache_counts['hits'] += 1

    def cache_miss(self) -> None:
        """Record one schema-cache miss that requires reflection."""
        if self._enabled:
            self._cache_counts['misses'] += 1

    def cache_refresh(self) -> None:
        """Record one explicit or recovery-driven schema refresh."""
        if self._enabled:
            self._cache_counts['refreshes'] += 1

    def finish(self, writer: Callable[[_DiagnosticRecord], None]) -> None:
        """Finalize this context through a fail-open record writer.

        Args:
            writer: Destination for one completed diagnostic record.
        """
        finalized = False
        record: _DiagnosticRecord | None = None
        with contextlib.suppress(Exception):
            record = self._finalize()
            finalized = True
        if not finalized:
            _warn_once('diagnostic accounting failed')
            return
        if record is None:
            return
        written = False
        with contextlib.suppress(Exception):
            writer(record)
            written = True
        if not written:
            _warn_once('diagnostic write failed')

    def provider_call(self) -> None:
        """Record one measured remote provider call."""
        if self._enabled:
            self._provider_call_count += 1

    def set_outcome(self, outcome: DiagnosticOutcome) -> None:
        """Set the bounded request outcome written at finalization.

        Args:
            outcome: Fixed outcome category for the completed request.
        """
        if self._enabled:
            self._outcome = outcome

    def span(self, phase: _DiagnosticPhase) -> contextlib.AbstractContextManager[None]:
        """Measure one exclusive phase, pausing any active parent.

        Args:
            phase: Fixed diagnostic phase to accumulate.

        Returns:
            A context manager that accrues elapsed time on exit.
        """
        if not self._enabled:
            return contextlib.nullcontext()
        return self._span(phase)

    def schema_reflection(self) -> contextlib.AbstractContextManager[None]:
        """Measure one exclusive schema-reflection span.

        Returns:
            A context manager that accrues elapsed time on exit.
        """
        return self.span('schema_reflection')


class DiagnosticSink:
    """Create request contexts and append finalized diagnostic records."""

    def __init__(
        self,
        path: Path | None,
        *,
        known_tools: Collection[str],
    ) -> None:
        """Initialize a sink for one optional diagnostic path.

        Args:
            path: JSONL destination, or `None` to disable diagnostics.
            known_tools: Tool names preserved verbatim in emitted records;
                other names normalize to `unknown`.
        """
        self._known_tools = frozenset(known_tools)
        self._path = path

    def __call__(self, record: _DiagnosticRecord) -> None:
        """Append one finalized record when diagnostics are enabled."""
        if self._path is None:
            return
        line = json.dumps(record, separators=(',', ':')) + '\n'
        with _WRITE_LOCK:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open('a', encoding='utf-8') as handle:
                handle.write(line)

    def start(self, tool: str, started_ns: int) -> DiagnosticContext:
        """Create one request timing context.

        Args:
            tool: Fixed server tool name, or an untrusted unknown name.
            started_ns: Monotonic request-start timestamp in nanoseconds.

        Returns:
            An enabled context when a destination exists; otherwise a no-op
            context.
        """
        if self._path is None:
            return DiagnosticContext(0, 'unknown', started_ns, enabled=False)
        normalized_tool = tool if tool in self._known_tools else 'unknown'
        return DiagnosticContext(_next_request_id(), normalized_tool, started_ns)


class ObservedConnection:
    """Request-scoped diagnostic view over one shared SG connection."""

    def __init__(self, connection: SGReadConnection, context: DiagnosticContext) -> None:
        """Store the shared connection and request context.

        Args:
            connection: Shared SG read connection.
            context: Request-scoped diagnostic context.
        """
        self._connection = connection
        self._context = context

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
        **options: Unpack[FindOptions],
    ) -> list[SGRecord]:
        """Run one observed SG `find` call.

        Args:
            entity_type: Entity type to query.
            filters: SG filter expression.
            fields: Fields to return.
            **options: Supported SG `find` options.

        Returns:
            Matching SG records.
        """
        with self._context.span('provider_call'):
            self._context.provider_call()
            return self._connection.find(entity_type, filters, fields, **options)

    def schema_entity_read(self) -> RawSchema:
        """Pass one entity-schema read through unchanged.

        Returns:
            The SG entity-schema dump.
        """
        return self._connection.schema_entity_read()

    def schema_field_read(
        self,
        entity_type: str,
        field_name: str | None = None,
        project_entity: SGRecord | None = None,
    ) -> RawSchema:
        """Pass one targeted field-schema read through unchanged.

        Args:
            entity_type: Entity type whose fields to read.
            field_name: Optional exact field name.
            project_entity: Optional project visibility context.

        Returns:
            The targeted SG field-schema dump.
        """
        return self._connection.schema_field_read(entity_type, field_name, project_entity)

    def schema_read(self) -> RawSchema:
        """Pass one field-schema read through unchanged.

        Returns:
            The SG field-schema dump.
        """
        return self._connection.schema_read()

    def summarize(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        summary_fields: list[SummaryField],
        filter_operator: str | None = None,
        grouping: list[SummaryGrouping] | None = None,
    ) -> SummaryResponse:
        """Run one observed SG `summarize` call.

        Args:
            entity_type: Entity type to summarize.
            filters: SG filter expression.
            summary_fields: Native SG summary specifications.
            filter_operator: Top-level filter conjunction.
            grouping: Optional native SG grouping specifications.

        Returns:
            The native SG summary response.
        """
        with self._context.span('provider_call'):
            self._context.provider_call()
            return self._connection.summarize(
                entity_type,
                filters,
                summary_fields,
                filter_operator=filter_operator,
                grouping=grouping,
            )


class ObservedSchemaCache:
    """Request-scoped diagnostic view over one shared schema cache."""

    def __init__(self, cache: SchemaCache, context: DiagnosticContext) -> None:
        """Store the shared cache and request context.

        Args:
            cache: Shared site-level schema cache.
            context: Request-scoped diagnostic context.
        """
        self._cache = cache
        self._context = context

    def current(self) -> SiteSchema:
        """Return the observed current schema.

        Returns:
            The current `SiteSchema`.

        Raises:
            QueryError: If the schema provider rejects or fails reflection.
        """
        with self._context.span('schema_cache_lookup'):
            return self._cache.current(self._context)

    def refresh(self) -> SiteSchema:
        """Return a freshly reflected observed schema.

        Returns:
            The freshly reflected `SiteSchema`.

        Raises:
            QueryError: If the schema provider rejects or fails reflection.
        """
        with self._context.span('schema_cache_lookup'):
            return self._cache.refresh(self._context)


def _next_request_id() -> int:
    """Allocate one positive process-local request identifier."""
    with _STATE_LOCK:
        return next(_REQUEST_IDS)


def _warn_once(category: _FailureCategory) -> None:
    """Emit one bounded warning for a fixed failure category."""
    with _STATE_LOCK:
        if category in _WARNING_CATEGORIES:
            return
        _WARNING_CATEGORIES.add(category)
    with contextlib.suppress(Exception):
        print(f'warning: {category}', file=sys.stderr)
