"""Engine exceptions and the bounded-alternatives error contract."""

import contextlib
from collections.abc import Generator, Mapping

from sg_mcp_server._providers import provider_error_type, provider_transport_error_types

ENUMERATION_CAP = 40


class EngineError(Exception):
    """Base class for engine failures."""


class QueryError(EngineError):
    """Report provider failures during reflection or execution, or malformed results."""


class SchemaError(EngineError):
    """Report unknown schema names or malformed reflected schema."""


class ValidationError(EngineError):
    """A pre-execution validation failure with bounded legal alternatives.

    Near-miss names are presented as different questions, never corrections;
    nothing is auto-substituted.

    Attributes:
        message: Failure message, including an omission notice when needed.
        alternatives: Legal values retained in input order, capped at 40 values
            per key.
        alternatives_omitted: Omitted-value counts for keys whose alternatives
            exceeded the cap.
        unknown_name: Whether the failure should trigger the schema cache's
            refresh-on-miss retry.
    """

    def __init__(
        self,
        message: str,
        alternatives: Mapping[str, list[str]] | None = None,
        unknown_name: bool = False,
    ) -> None:
        """Store a validation failure and detached, bounded alternatives.

        Args:
            message: Failure text. An omission notice is appended if any
                alternatives list exceeds 40 values.
            alternatives: Legal values grouped by key, or `None` for none.
                Each list is copied in input order and capped at 40 values;
                omitted counts are retained separately.
            unknown_name: Whether schema refresh should be attempted before
                retrying validation.
        """
        full = alternatives or {}
        self.alternatives = {key: _capped(values) for key, values in full.items()}
        self.alternatives_omitted = {
            key: len(values) - ENUMERATION_CAP
            for key, values in full.items()
            if len(values) > ENUMERATION_CAP
        }
        if self.alternatives_omitted:
            message = (
                f'{message}; more alternatives exist than are listed: '
                f'only the first {ENUMERATION_CAP} are included'
            )
        super().__init__(message)
        self.message = message
        self.unknown_name = unknown_name


def _capped(values: list[str]) -> list[str]:
    """Cap an alternatives list to its first legal values."""
    return values[:ENUMERATION_CAP]


def _transport_query_error(error: Exception) -> QueryError:
    """Name a transport failure's class and detail in a query error."""
    return QueryError(
        f'the SG site request failed ({type(error).__name__}: {str(error) or "no details"})'
    )


@contextlib.contextmanager
def provider_errors_as_query_errors() -> Generator[None, None, None]:
    """Translate provider failures raised in the block into query errors.

    A backend error keeps its message. A supported transport error is described
    by its exception class and detail, or `no details` when its message is
    empty.

    Yields:
        Control to the block that calls the provider.

    Raises:
        QueryError: If the block raises the provider's backend error or a
            supported transport error; the original exception is the cause.
    """
    shotgun_error = provider_error_type()
    transport_errors = provider_transport_error_types()
    try:
        yield
    except shotgun_error as error:
        raise QueryError(str(error)) from error
    except transport_errors as error:
        raise _transport_query_error(error) from error
