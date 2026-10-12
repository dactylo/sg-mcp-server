import datetime
import functools
import tempfile
from pathlib import Path
from typing import Protocol, TypedDict, Unpack, cast

import summarize_stub
import synthetic_schema
from shotgun_api3.lib.mockgun.mockgun import Shotgun as MockgunShotgun

from sg_mcp_server._providers import (
    FindOptions,
    RawSchema,
    SGRecord,
    SGValue,
    SummaryField,
    SummaryGrouping,
    SummaryResponse,
    ValidatedFilters,
)

FIXTURE_BASE_URL = 'https://fixture.shotgunstudio.com'


class Clock:
    def __init__(self, *samples: int) -> None:
        self._samples = iter(samples)

    def __call__(self) -> int:
        return next(self._samples)


class FixtureConnection:
    def __init__(self, site: '_MockgunSite') -> None:
        self.site = site

    def create(self, entity_type: str, data: SGRecord) -> SGRecord:
        return self.site.create(entity_type, data)

    def delete(self, entity_type: str, entity_id: int) -> bool:
        return self.site.delete(entity_type, entity_id)

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
        **options: Unpack[FindOptions],
    ) -> list[SGRecord]:
        filter_operator = options.get('filter_operator')
        limit = options.get('limit', 0)
        order = options.get('order')
        # mockgun applies stable sort clauses from first to last, making the
        # final clause primary. Reverse them at this adapter boundary so the
        # first requested clause remains primary, matching live SG.
        mockgun_order = list(reversed(order)) if order else order
        page = options.get('page', 0)
        retired_only = options.get('retired_only', False)
        rows = self.site.find(
            entity_type,
            filters=_mockgun_filters(filters),
            fields=fields or [],
            order=mockgun_order,
            filter_operator=filter_operator,
            retired_only=retired_only,
        )
        if limit <= 0:
            return rows
        start = (max(page, 1) - 1) * limit
        return rows[start : start + limit]

    def find_one(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
    ) -> SGRecord | None:
        rows = self.find(entity_type, filters, fields, limit=1)
        return rows[0] if rows else None

    def schema_entity_read(self) -> RawSchema:
        return self.site.schema_entity_read()

    def schema_field_read(
        self,
        entity_type: str,
        field_name: str | None = None,
        project_entity: SGRecord | None = None,
    ) -> RawSchema:
        del project_entity
        try:
            return self.site.schema_field_read(entity_type, field_name)
        except KeyError:
            return {}

    def schema_read(self) -> RawSchema:
        return self.site.schema_read()

    def summarize(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        summary_fields: list[SummaryField],
        filter_operator: str | None = None,
        grouping: list[SummaryGrouping] | None = None,
    ) -> SummaryResponse:
        return summarize_stub.native_response(
            self,
            entity_type,
            filters,
            summary_fields,
            filter_operator=filter_operator,
            grouping=grouping,
        )


class _MockgunFindOptions(TypedDict):
    fields: list[str]
    order: list[dict[str, str]] | None
    filter_operator: str | None
    retired_only: bool


class _MockgunSite(Protocol):
    def create(self, entity_type: str, data: SGRecord) -> SGRecord: ...

    def delete(self, entity_type: str, entity_id: int) -> bool: ...

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        **options: Unpack[_MockgunFindOptions],
    ) -> list[SGRecord]: ...

    def schema_entity_read(self) -> RawSchema: ...

    def schema_field_read(
        self,
        entity_type: str,
        field_name: str | None = None,
    ) -> RawSchema: ...

    def schema_read(self) -> RawSchema: ...


class _SchemaPathOwner(Protocol):
    def set_schema_paths(self, schema_path: str, schema_entity_path: str) -> None: ...


def _mockgun_operand(value: SGValue) -> SGValue:
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        return value.astimezone(datetime.UTC).replace(tzinfo=None)
    if isinstance(value, list):
        return [_mockgun_operand(item) for item in value]
    return value


def _mockgun_filters(filters: ValidatedFilters) -> ValidatedFilters:
    adapted: ValidatedFilters = []
    for condition in filters:
        if isinstance(condition, dict):
            group = cast('dict[str, SGValue]', condition)
            adapted.append(
                {
                    **group,
                    'filters': _mockgun_filters(cast('ValidatedFilters', group['filters'])),
                }
            )
            continue
        field, operator, *values = cast('list[SGValue]', condition)
        adapted.append([field, operator, *[_mockgun_operand(value) for value in values]])
    return adapted


@functools.cache
def _synthetic_schema_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix='sg-product-schema-')


@functools.cache
def _synthetic_schema_paths() -> tuple[Path, Path]:
    return synthetic_schema.write_schema_pickles(Path(_synthetic_schema_directory().name))


def new_fixture_site(schema_paths: tuple[Path, Path] | None = None) -> FixtureConnection:
    fields, entities = _synthetic_schema_paths() if schema_paths is None else schema_paths
    schema_path_owner = cast('_SchemaPathOwner', MockgunShotgun)
    schema_path_owner.set_schema_paths(str(fields), str(entities))
    site = cast('_MockgunSite', MockgunShotgun(FIXTURE_BASE_URL, script_name='tests', api_key='x'))
    return FixtureConnection(site)
