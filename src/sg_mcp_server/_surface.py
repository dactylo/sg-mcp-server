"""Package-internal contract between the server adapter and one served tool surface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias

from mcp import types

from sg_mcp_server._json import JSONValue

if TYPE_CHECKING:
    import jsonschema

    from sg_mcp_server._config import Config
    from sg_mcp_server._diagnostics import DiagnosticContext
    from sg_mcp_server._engine import SchemaCache
    from sg_mcp_server._providers import SGReadConnection
    from sg_mcp_server._rendering import RenderResult

_ArgumentErrorFn = Callable[[str, dict[str, Any], 'jsonschema.ValidationError'], str]
MCPJSONValue: TypeAlias = JSONValue
MCPStructuredContent: TypeAlias = Mapping[str, MCPJSONValue]
ToolBodyResult: TypeAlias = tuple[str, MCPStructuredContent]
ToolHandler = Callable[['ToolRuntime', dict[str, Any], 'DiagnosticContext'], types.CallToolResult]


class ToolRuntime(Protocol):
    """Services the adapter provides to every handler call."""

    config: Config
    connection: SGReadConnection
    schema_cache: SchemaCache

    def audited(
        self,
        payload: Mapping[str, object],
        context: DiagnosticContext,
        run: Callable[[], ToolBodyResult],
    ) -> types.CallToolResult:
        """Run one tool body and record its outcome before returning or reraising.

        Args:
            payload: Complete validated arguments for the audit record.
            context: Diagnostic context for this call.
            run: Synchronous tool body.

        Returns:
            The successful result or a handled engine-error result.

        Raises:
            BaseException: If the tool body raises an unexpected exception.
        """
        ...

    def run_query_request(
        self,
        request: Mapping[str, object],
        entity_type: str,
        context: DiagnosticContext,
        *,
        require_record_id: int | None = None,
    ) -> RenderResult:
        """Validate, execute, and render one engine-level query request.

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
        ...


@dataclass(frozen=True, slots=True)
class ToolSurface:
    """One complete served tool table with its handlers and error shaping.

    Tool names are unique and match the handler keys exactly. Attributes are
    fixed; `handlers` is copied at construction but remains mutable.

    Attributes:
        tools: Tool definitions in listing order.
        handlers: Synchronous handlers keyed by the corresponding tool names.
        argument_error: Formatter for a tool's rejected input arguments.
    """

    tools: tuple[types.Tool, ...]
    handlers: Mapping[str, ToolHandler]
    argument_error: _ArgumentErrorFn

    def __post_init__(self) -> None:
        """Validate the served tool table and defensively copy its handlers.

        Raises:
            ValueError: If tool names are duplicated or handlers do not cover
                exactly those names.
        """
        names = tuple(tool.name for tool in self.tools)
        if len(set(names)) != len(names) or set(self.handlers) != set(names):
            raise ValueError('surface handlers must cover exactly its tool names')
        object.__setattr__(self, 'handlers', dict(self.handlers))


def base_argument_error(error: jsonschema.ValidationError) -> str:
    """Return the adapter's default schema-rejection message.

    Args:
        error: Best matching JSON Schema validation error.

    Returns:
        A concise invalid-arguments message.
    """
    return f'invalid arguments: {error.message}'
