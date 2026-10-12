"""Provide the typed SG read boundary and live connection factory."""

import http.client
import importlib
import xmlrpc.client
from collections.abc import Mapping
from datetime import date, datetime
from typing import (
    Literal,
    NotRequired,
    Protocol,
    TypeAlias,
    TypedDict,
    Unpack,
    cast,
    runtime_checkable,
)

from sg_mcp_server._config import Config

RawSchema: TypeAlias = Mapping[str, object]
SGValue: TypeAlias = (
    bool | int | float | str | date | datetime | list['SGValue'] | dict[str, 'SGValue'] | None
)
SGRecord: TypeAlias = dict[str, SGValue]
ValidatedFilters: TypeAlias = list[SGValue]


class FindOptions(TypedDict, total=False):
    """Optional keyword arguments forwarded to the SG `find()` boundary."""

    filter_operator: str | None
    limit: int
    order: list[dict[str, str]] | None
    page: int
    retired_only: bool


class SummaryField(TypedDict):
    """One native summarize field and operation specification."""

    field: str
    type: str


class SummaryGroup(TypedDict):
    """One provider summary group with its key, label, aggregates, and optional nested children."""

    group_name: SGValue
    group_value: SGValue
    groups: NotRequired[list['SummaryGroup']]
    summaries: dict[str, SGValue]


class SummaryGrouping(TypedDict):
    """One native summarize grouping specification."""

    field: str
    direction: NotRequired[Literal['asc', 'desc']]
    type: NotRequired[Literal['exact']]


class SummaryRequest(TypedDict):
    """Native provider arguments for one summary query."""

    summary_fields: list[SummaryField]
    grouping: NotRequired[list[SummaryGrouping]]


class SummaryResponse(TypedDict):
    """Raw grouped or ungrouped response from the SG summary boundary."""

    summaries: dict[str, SGValue]
    groups: list[SummaryGroup]


@runtime_checkable
class SGReadConnection(Protocol):
    """Structural type for the read subset of the SG API."""

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
        **options: Unpack[FindOptions],
    ) -> list[SGRecord]:
        """Find records through the supported read boundary.

        Args:
            entity_type: Entity type to query.
            filters: Validated native SG filters.
            fields: Fields to return.
            **options: Supported ordering, limit, filter, and retired options.

        Returns:
            Matching records in provider order.
        """
        ...

    def schema_entity_read(self) -> RawSchema:
        """Return the raw entity schema.

        Returns:
            The provider's raw entity-schema mapping.
        """
        ...

    def schema_field_read(
        self,
        entity_type: str,
        field_name: str | None = None,
        project_entity: SGRecord | None = None,
    ) -> RawSchema:
        """Return raw field schema for an entity or exact field.

        Args:
            entity_type: Entity type whose fields to read.
            field_name: Optional exact field name.
            project_entity: Optional provider visibility context.

        Returns:
            The provider's raw field-schema mapping.
        """
        ...

    def schema_read(self) -> RawSchema:
        """Return the complete raw field schema.

        Returns:
            The provider's all-entity field-schema mapping.
        """
        ...

    def summarize(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        summary_fields: list[SummaryField],
        filter_operator: str | None = None,
        grouping: list[SummaryGrouping] | None = None,
    ) -> SummaryResponse:
        """Summarize records through the supported read boundary.

        Args:
            entity_type: Entity type to summarize.
            filters: Validated native SG filters.
            summary_fields: Aggregate operations to compute.
            filter_operator: Optional top-level filter operator.
            grouping: Optional grouping instructions.

        Returns:
            Provider summary values and groups.
        """
        ...


class _HttpLib2Module(Protocol):
    """Describe the optional provider transport exception base."""

    HttpLib2Error: type[Exception]


class _ShotgunConfig(Protocol):
    """Describe the provider's socket timeout configuration."""

    timeout_secs: float | None


class _ShotgunClient(SGReadConnection, Protocol):
    """Describe a read client with configurable connection timeouts."""

    config: _ShotgunConfig


class _ShotgunFactory(Protocol):
    """Describe construction of the installed provider client."""

    def __call__(
        self,
        site_url: str,
        *,
        script_name: str,
        api_key: str,
        connect: bool,
    ) -> _ShotgunClient:
        """Construct a client through the provider's supported arguments."""
        ...


class _ShotgunModule(Protocol):
    """Describe the installed provider definitions used at runtime."""

    Shotgun: _ShotgunFactory
    ShotgunError: type[Exception]


def connect(config: Config) -> SGReadConnection:
    """Create the live shotgun_api3 client for the configured site.

    Args:
        config: Resolved server configuration.

    Returns:
        A connection satisfying `SGReadConnection`. Construction uses
        `connect=False`: no network happens here, so the server starts and
        lists tools instantly. Each connection attempt and each wait for
        response data time out after `config.timeout_seconds`.

    Raises:
        ModuleNotFoundError: If the official SG client is not installed; the
            CLI reports this as a startup error.
    """
    provider = cast('_ShotgunModule', importlib.import_module('shotgun_api3.shotgun'))
    client = provider.Shotgun(
        config.site_url,
        script_name=config.script_name,
        api_key=config.api_key,
        connect=False,
    )
    client.config.timeout_secs = config.timeout_seconds
    return client


def provider_error_type() -> type[Exception]:
    """Resolve the optional provider's backend exception type lazily.

    Returns:
        The installed provider's base backend exception type.
    """
    provider = cast('_ShotgunModule', importlib.import_module('shotgun_api3.shotgun'))
    return provider.ShotgunError


def provider_transport_error_types() -> tuple[type[Exception], ...]:
    """Resolve transport failures, including the optional provider's errors.

    Returns:
        Socket, HTTP, and XML-RPC transport exception bases, followed by the
        official client's httplib2 base when installed. A missing optional
        provider module leaves only the standard-library exception types.
    """
    errors = (OSError, http.client.HTTPException, xmlrpc.client.ProtocolError)
    try:
        provider = cast('_HttpLib2Module', importlib.import_module('shotgun_api3.lib.httplib2'))
    except ModuleNotFoundError:
        return errors
    return (*errors, provider.HttpLib2Error)
