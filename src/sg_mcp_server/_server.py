"""Assemble the MCP server that serves the read-only tools."""

import asyncio
import contextlib
import datetime
import importlib.metadata
import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import jsonschema
import jsonschema.exceptions as jsonschema_exceptions
from jsonschema.validators import validator_for
from mcp import MCPError, types
from mcp.server import Server, ServerRequestContext

from sg_mcp_server import _engine, _rendering
from sg_mcp_server._audit import AuditLog
from sg_mcp_server._config import Config
from sg_mcp_server._diagnostics import (
    DiagnosticContext,
    DiagnosticOutcome,
    DiagnosticSink,
    ObservedConnection,
    ObservedSchemaCache,
)
from sg_mcp_server._providers import SGReadConnection
from sg_mcp_server._surface import (
    MCPJSONValue,
    MCPStructuredContent,
    ToolBodyResult,
    ToolHandler,
    ToolSurface,
)

if TYPE_CHECKING:
    from jsonschema.protocols import Validator

_BestMatchFn = Callable[[Iterable[jsonschema.ValidationError]], jsonschema.ValidationError | None]
# jsonschema leaves best_match's return type unannotated.
_BEST_MATCH = cast('_BestMatchFn', jsonschema_exceptions.best_match)  # pyright: ignore[reportUnknownMemberType]

_DISPATCHED_TOOL: ContextVar[str] = ContextVar('dispatched_tool')
_DISTRIBUTION = 'sg-mcp-server'

_SERVER_INSTRUCTIONS = (
    'Read-only access to Autodesk Flow Production Tracking (FPT), formerly ShotGrid and '
    'Shotgun, also called SG. Use these tools for requests about any of those names. Treat '
    "'Flow' as this service only in a production-tracking context, such as projects, shots, "
    'assets, versions, tasks, or notes. Inspect the live site schema before guessing entity '
    'types, fields, or status values.'
)
_SERVER_NAME = 'sg-mcp-server'

_RunFn = Callable[[], ToolBodyResult]


class _ServerAdapter:
    """Bind the read tools to one configured SG connection."""

    def __init__(
        self,
        config: Config,
        connection: SGReadConnection,
        surface: ToolSurface,
        clock: Callable[[], datetime.datetime] | None,
    ) -> None:
        """Initialize the shared cache, audit sink, and dispatch maps."""
        self._audit = AuditLog(config.audit_path, caller=config.script_name)
        self._clock = clock
        self.config = config
        self.connection = connection
        self._connection_lock = threading.Lock()
        self._diagnostics = DiagnosticSink(
            config.diagnostics_path,
            known_tools=tuple(tool.name for tool in surface.tools),
        )
        self._handlers = dict(surface.handlers)
        self._validators: dict[str, Validator] = {}
        for tool in surface.tools:
            validator_class = validator_for(tool.input_schema)
            validator_class.check_schema(tool.input_schema)
            self._validators[tool.name] = validator_class(tool.input_schema)
        self._surface = surface
        self.schema_cache = _engine.SchemaCache(connection, ttl_seconds=config.schema_ttl_seconds)
        self._zone = ZoneInfo(config.timezone)

    def __call__(self) -> Server:
        """Build the low-level SDK server, identified by the installed version."""
        return Server(
            _SERVER_NAME,
            version=importlib.metadata.version(_DISTRIBUTION),
            instructions=_SERVER_INSTRUCTIONS,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

    def _call_handler(
        self,
        handler: ToolHandler,
        arguments: dict[str, Any],
        context: DiagnosticContext,
    ) -> types.CallToolResult:
        """Run a complete handler while owning the shared connection."""
        with context.span('connection_lock_wait'):
            self._connection_lock.acquire()
        try:
            return handler(self, arguments, context)
        finally:
            self._connection_lock.release()

    def _error_result(
        self,
        body: Mapping[str, MCPJSONValue],
        *,
        contains_untrusted_site_text: bool = False,
    ) -> types.CallToolResult:
        """Return one MCP tool error through both content channels."""
        structured = dict(body)
        if contains_untrusted_site_text:
            structured['contains_untrusted_site_text'] = True
        text = json.dumps(structured)
        if contains_untrusted_site_text:
            text = _rendering.wrap_untrusted(text)
        return types.CallToolResult(
            content=[types.TextContent(type='text', text=text)],
            structured_content=dict(structured),
            is_error=True,
        )

    async def _on_list_tools(
        self,
        _ctx: ServerRequestContext[Any],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        """Return the served tool table exactly."""
        return types.ListToolsResult(tools=list(self._surface.tools))

    def _record_outcome(
        self,
        context: DiagnosticContext,
        payload: Mapping[str, object],
        outcome: DiagnosticOutcome,
        duration_ms: float,
        *,
        entity_ids: list[int] | None = None,
    ) -> None:
        """Record one typed outcome in both diagnostics and the audit log."""
        context.set_outcome(outcome)
        with context.span('audit_write'):
            self._audit.record_tool_call(
                _DISPATCHED_TOOL.get(), payload, outcome, duration_ms, entity_ids
            )

    async def _on_call_tool(
        self,
        _ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        """Validate and dispatch one tool call."""
        received_ns = time.monotonic_ns()
        started = time.monotonic()
        context = self._diagnostics.start(params.name, received_ns)
        arguments = params.arguments or {}
        token = _DISPATCHED_TOOL.set(params.name)
        try:
            handler = self._handlers.get(params.name)
            if handler is None:
                # Unknown tool = protocol-level error (MCP spec), not a tool
                # result. MCPError preserves its message; an ordinary exception
                # becomes an opaque 'Internal server error'. Rejections are tool
                # calls too, so the audit log records them before they exit.
                duration_ms = (time.monotonic() - started) * 1000
                self._record_outcome(context, arguments, 'unknown_tool', duration_ms)
                raise MCPError(code=types.INVALID_PARAMS, message=f'unknown tool {params.name!r}')
            try:
                with context.span('argument_validation'):
                    error = _BEST_MATCH(self._validators[params.name].iter_errors(arguments))
                    if error is not None:
                        raise error
            except jsonschema.ValidationError as error:
                # Strict rejection (decision 0002): never coerce, drop, or
                # guess at arguments the schema does not accept.
                duration_ms = (time.monotonic() - started) * 1000
                self._record_outcome(context, arguments, 'invalid_arguments', duration_ms)
                message = self._surface.argument_error(params.name, arguments, error)
                return self._error_result({'error': message})
            # Engine and SG calls are synchronous; a worker thread keeps the
            # connection responsive to pings while a query runs.
            worker = asyncio.create_task(
                asyncio.to_thread(self._call_handler, handler, arguments, context)
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                while not worker.done():
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(worker)
                if not worker.cancelled():
                    worker.exception()
                raise
        finally:
            try:
                context.finish(self._diagnostics)
            finally:
                _DISPATCHED_TOOL.reset(token)

    def _site_now(self) -> datetime.datetime:
        """Return the configured site's current aware timestamp."""
        if self._clock is None:
            return datetime.datetime.now(self._zone)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return an aware datetime')
        return now.astimezone(self._zone)

    def audited(
        self,
        payload: Mapping[str, object],
        context: DiagnosticContext,
        run: _RunFn,
    ) -> types.CallToolResult:
        """Run one tool and record its outcome before returning or reraising.

        Args:
            payload: Complete validated arguments for the audit record.
            context: Diagnostic context for this call.
            run: Synchronous tool body.

        Returns:
            The successful result or a handled engine-error result.

        Raises:
            BaseException: If the tool body raises an unexpected exception.
        """
        started = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - started) * 1000

        try:
            text, structured = run()
        except _engine.ValidationError as error:
            duration_ms = elapsed()
            self._record_outcome(context, payload, 'validation_error', duration_ms)
            body: dict[str, MCPJSONValue] = {
                'error': error.message,
                'alternatives': {
                    key: cast('MCPJSONValue', list(values))
                    for key, values in error.alternatives.items()
                },
            }
            if error.alternatives_omitted:
                body['alternatives_omitted'] = dict(error.alternatives_omitted)
            return self._error_result(
                body,
                contains_untrusted_site_text=True,
            )
        except _engine.EngineError as error:
            duration_ms = elapsed()
            self._record_outcome(context, payload, 'query_error', duration_ms)
            return self._error_result(
                {'error': str(error)},
                contains_untrusted_site_text=True,
            )
        except BaseException:
            duration_ms = elapsed()
            self._record_outcome(context, payload, 'unexpected_error', duration_ms)
            raise
        ids = _record_ids(structured)
        duration_ms = elapsed()
        self._record_outcome(context, payload, 'ok', duration_ms, entity_ids=ids)
        return types.CallToolResult(
            content=[types.TextContent(type='text', text=text)],
            structured_content=dict(structured),
        )

    def run_query_request(
        self,
        request: Mapping[str, object],
        entity_type: str,
        context: DiagnosticContext,
        *,
        require_record_id: int | None = None,
    ) -> _rendering.RenderResult:
        """Validate, execute, and render one shared engine request.

        Args:
            request: Engine-level query request.
            entity_type: Entity type used to render the result.
            context: Diagnostic context for this call.
            require_record_id: ID included in the missing-record error when a
                records query returns no rows. The caller must constrain the
                request to this ID; this argument does not filter the query.

        Returns:
            Rendered text and structured content.

        Raises:
            EngineError: If validation or provider execution fails, or a
                records query returns no rows with `require_record_id` set.
        """
        cache = ObservedSchemaCache(self.schema_cache, context)
        connection = ObservedConnection(self.connection, context)
        with context.span('query_validation'):
            query, site_schema = _engine.validate_with_schema(
                cache,
                request,
                self.config.default_limit,
                self.config.hard_limit,
                self._site_now(),
            )
        result = _engine.run_query(connection, query)
        if require_record_id is not None and result.kind == 'records' and not result.records:
            raise _engine.QueryError(
                f'no results: no visible {entity_type} with id {require_record_id}; '
                'the record may not exist or permissions may hide it '
                '(the query itself was valid)'
            )
        with context.span('rendering'):
            return _rendering.render_result(
                result,
                site_schema,
                entity_type,
                self.config.site_url,
                self.config.timezone,
            )


def _record_ids(structured: MCPStructuredContent) -> list[int]:
    """Extract only checked integer ids from a records envelope."""
    records = structured.get('records')
    if not isinstance(records, list):
        return []
    ids: list[int] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        record_id = record.get('id')
        if isinstance(record_id, int) and not isinstance(record_id, bool):
            ids.append(record_id)
    return ids


def build_server(
    config: Config,
    connection: SGReadConnection,
    surface: ToolSurface,
    *,
    clock: Callable[[], datetime.datetime] | None = None,
) -> Server:
    """Assemble the MCP server with tools bound to one connection.

    Args:
        config: Resolved configuration.
        connection: SG connection (live in production, mockgun in tests).
        surface: Complete served tool table, handlers, and error shaping.
        clock: Optional aware clock; timestamps are converted to the site zone.

    Returns:
        The configured low-level Server; its tool list is the surface's static
        table, every call is schema-validated before dispatch, and it reports
        the installed distribution version to clients.

    Raises:
        PackageNotFoundError: If the installed distribution metadata is
            unavailable.
    """
    return _ServerAdapter(config, connection, surface, clock)()
