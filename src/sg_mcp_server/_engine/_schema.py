"""Site schema model, reflection, and cache."""

import contextlib
import time
from collections.abc import Mapping
from typing import Protocol, cast

from sg_mcp_server._engine._errors import (
    ENUMERATION_CAP,
    QueryError,
    SchemaError,
    transport_query_error,
)
from sg_mcp_server._json import JSONValue
from sg_mcp_server._providers import (
    RawSchema,
    SGReadConnection,
    provider_error_type,
    provider_transport_error_types,
)

LINKED_PATH_SEGMENTS = 3
_MAX_SEARCH_RESULTS = 40
_SUGGESTION_DISTANCE_RATIO = 0.5
_SUGGESTION_LIMIT = 3


class SiteSchema:
    """Read-only view over reflected SG field and entity metadata."""

    def __init__(self, fields: RawSchema, entities: RawSchema) -> None:
        """Store the raw field and entity dumps.

        Args:
            fields: Field dump keyed by entity type.
            entities: Entity dump keyed by entity type.
        """
        self._fields = fields
        self._entities = entities

    def _entity_fields(self, entity_type: str) -> dict[str, object]:
        """Return one entity's reflected field mapping."""
        return _raw_object(
            _raw_required(self._fields, entity_type, 'fields'),
            f'fields.{entity_type}',
        )

    def _field(self, entity_type: str, field_name: str) -> dict[str, object]:
        """Return one reflected field envelope."""
        fields = self._entity_fields(entity_type)
        return _raw_object(
            _raw_required(fields, field_name, f'fields.{entity_type}'),
            f'{entity_type}.{field_name}',
        )

    def entity_display_name(self, entity_type: str) -> str:
        """Return the site display name of an entity type.

        Args:
            entity_type: Entity type to look up.

        Returns:
            The display name, falling back to the entity type code.
        """
        entity = self._entities.get(entity_type)
        if entity is None:
            return entity_type
        entity_schema = _raw_object(entity, entity_type)
        return _raw_string(
            _raw_value(entity_schema, 'name', entity_type),
            f'{entity_type}.name.value',
        )

    def entity_types(self) -> list[str]:
        """Return every entity type in the schema, sorted.

        Returns:
            Sorted entity type codes.
        """
        return sorted(self._fields)

    def has_entity(self, entity_type: str) -> bool:
        """Report whether an entity type exists.

        Args:
            entity_type: Entity type to test.

        Returns:
            `True` when the entity type is present in the schema.
        """
        return entity_type in self._fields

    def has_field(self, entity_type: str, field_name: str) -> bool:
        """Report whether a direct field exists on an entity type.

        Args:
            entity_type: Entity type to test.
            field_name: Field to test.

        Returns:
            `True` when the field is present, treating `id` and `type` as
            present.
        """
        if not self.has_entity(entity_type):
            return False
        return field_name in {'id', 'type'} or field_name in self._entity_fields(entity_type)

    def suggest_entities(self, entity_type: str) -> list[str]:
        """Return entity types close to a misspelled one.

        Args:
            entity_type: The rejected entity type.

        Returns:
            Up to `_SUGGESTION_LIMIT` near-miss entity types.
        """
        return _nearest(entity_type, self.entity_types())

    def _require_entity(self, entity_type: str) -> None:
        """Raise unless the entity type exists."""
        if not self.has_entity(entity_type):
            suggestions = self.suggest_entities(entity_type)
            hint = f' Did you mean: {", ".join(suggestions)}?' if suggestions else ''
            raise SchemaError(f'{entity_type} is not a valid entity type.{hint}')

    def field_names(self, entity_type: str) -> list[str]:
        """Return the field names of an entity type, sorted.

        Args:
            entity_type: Entity type to inspect.

        Returns:
            Sorted field names.

        Raises:
            SchemaError: If the entity type does not exist.
        """
        self._require_entity(entity_type)
        return sorted(self._entity_fields(entity_type))

    def suggest_fields(self, entity_type: str, field_name: str) -> list[str]:
        """Return fields close to a misspelled one.

        Args:
            entity_type: Entity type that was queried.
            field_name: The rejected field name.

        Returns:
            Up to `_SUGGESTION_LIMIT` near-miss field names.
        """
        if not self.has_entity(entity_type):
            return []
        return _nearest(field_name, sorted(self._entity_fields(entity_type)))

    def _require_field(self, entity_type: str, field_name: str) -> None:
        """Raise unless the direct field exists on the entity type."""
        self._require_entity(entity_type)
        if not self.has_field(entity_type, field_name):
            suggestions = self.suggest_fields(entity_type, field_name)
            hint = f' Did you mean: {", ".join(suggestions)}?' if suggestions else ''
            raise SchemaError(f'{field_name} is not a valid field for entity {entity_type}.{hint}')

    def describe_field(self, entity_type: str, field_name: str) -> dict[str, JSONValue]:
        """Return one field description including bounded type constraints.

        Args:
            entity_type: Entity type owning the field.
            field_name: Field to describe.

        Returns:
            Field name, label, data type, optional description, and
            type-specific constraints. `valid_values`, `display_values`, and
            `valid_types` each retain at most 40 entries. A corresponding
            `valid_values_omitted`, `display_values_omitted`, or
            `valid_types_omitted` count is included when entries are omitted.

        Raises:
            SchemaError: If the field does not exist on the entity type or
                reflected field metadata is malformed.
        """
        self._require_field(entity_type, field_name)
        field = self._field(entity_type, field_name)
        field_path = f'{entity_type}.{field_name}'
        properties = _raw_object(
            _raw_required(field, 'properties', field_path),
            f'{field_path}.properties',
        )
        described: dict[str, JSONValue] = {
            'entity_type': entity_type,
            'field': field_name,
            'label': _raw_string(
                _raw_value(field, 'name', field_path),
                f'{field_path}.name.value',
            ),
            'data_type': _raw_string(
                _raw_value(field, 'data_type', field_path),
                f'{field_path}.data_type.value',
            ),
        }
        description = _raw_string(
            _raw_value(field, 'description', field_path),
            f'{field_path}.description.value',
        )
        if description:
            described['description'] = description
        if 'valid_values' in properties:
            values, omitted = _described_cap(
                _raw_string_list(
                    _raw_value(properties, 'valid_values', f'{field_path}.properties'),
                    f'{field_path}.properties.valid_values.value',
                )
            )
            described['valid_values'] = [*values]
            if omitted:
                described['valid_values_omitted'] = omitted
        if 'display_values' in properties:
            display_values = _raw_object(
                _raw_value(properties, 'display_values', f'{field_path}.properties'),
                f'{field_path}.properties.display_values.value',
            )
            if any(not isinstance(value, str) for value in display_values.values()):
                raise SchemaError(
                    'malformed reflected schema at '
                    f'{field_path}.properties.display_values.value: expected string values'
                )
            typed_display_values = cast('dict[str, str]', display_values)
            keys, omitted = _described_cap(list(typed_display_values))
            described['display_values'] = {key: typed_display_values.get(key, key) for key in keys}
            if omitted:
                described['display_values_omitted'] = omitted
        if 'valid_types' in properties:
            values, omitted = _described_cap(
                _raw_string_list(
                    _raw_value(properties, 'valid_types', f'{field_path}.properties'),
                    f'{field_path}.properties.valid_types.value',
                )
            )
            described['valid_types'] = [*values]
            if omitted:
                described['valid_types_omitted'] = omitted
        return described

    def describe_entity(self, entity_type: str) -> dict[str, JSONValue]:
        """Return every field of one entity type in a model-readable form.

        Args:
            entity_type: Entity type to describe.

        Returns:
            The entity display name and a sorted list of field descriptions.

        Raises:
            SchemaError: If the entity type does not exist.
        """
        self._require_entity(entity_type)
        return {
            'entity_type': entity_type,
            'display_name': self.entity_display_name(entity_type),
            'fields': [
                self.describe_field(entity_type, field_name)
                for field_name in sorted(self._entity_fields(entity_type))
            ],
        }

    def field_data_type(self, entity_type: str, field_name: str) -> str:
        """Return the data type of a direct field.

        Args:
            entity_type: Entity type owning the field.
            field_name: Field to look up.

        Returns:
            The schema data type.

        Raises:
            SchemaError: If the field does not exist on the entity type.
        """
        if field_name == 'id':
            return 'number'
        if field_name == 'type':
            return 'entity_type'
        self._require_field(entity_type, field_name)
        field = self._field(entity_type, field_name)
        path = f'{entity_type}.{field_name}.data_type.value'
        return _raw_string(
            _raw_value(field, 'data_type', f'{entity_type}.{field_name}'),
            path,
        )

    def search_fields(
        self, query: str, entity_type: str | None = None
    ) -> list[dict[str, JSONValue]]:
        """Return fields whose name or label contains a substring.

        Args:
            query: Case-insensitive substring to look for.
            entity_type: Optional entity type to restrict the search to.

        Returns:
            Up to `_MAX_SEARCH_RESULTS` matching field descriptions.

        Raises:
            SchemaError: If the query is empty or the entity type is unknown.
        """
        if not query:
            raise SchemaError('search_fields requires a non-empty query')
        if entity_type is not None:
            self._require_entity(entity_type)
            entity_types = [entity_type]
        else:
            entity_types = self.entity_types()
        needle = query.lower()
        matches: list[dict[str, JSONValue]] = []
        for candidate_type in entity_types:
            for field_name in sorted(self._entity_fields(candidate_type)):
                field = self._field(candidate_type, field_name)
                field_path = f'{candidate_type}.{field_name}'
                label = _raw_string(
                    _raw_value(field, 'name', field_path),
                    f'{field_path}.name.value',
                )
                if needle in field_name.lower() or needle in label.lower():
                    matches.append(self.describe_field(candidate_type, field_name))
        return matches[:_MAX_SEARCH_RESULTS]

    def valid_types(self, entity_type: str, field_name: str) -> list[str] | None:
        """Return the entity types an entity or multi_entity field may point at.

        Args:
            entity_type: Entity type owning the field.
            field_name: Field to look up.

        Returns:
            The permitted entity types, or `None` for other data types.

        Raises:
            SchemaError: If the field does not exist.
        """
        self._require_field(entity_type, field_name)
        field = self._field(entity_type, field_name)
        field_path = f'{entity_type}.{field_name}'
        properties = _raw_object(
            _raw_required(field, 'properties', field_path),
            f'{field_path}.properties',
        )
        if 'valid_types' not in properties:
            return None
        return list(
            _raw_string_list(
                _raw_value(properties, 'valid_types', f'{field_path}.properties'),
                f'{field_path}.properties.valid_types.value',
            )
        )

    def resolve_path(self, entity_type: str, path: str) -> str:
        """Return the data type of a direct or dotted linked field path.

        Args:
            entity_type: Entity type the path starts from.
            path: Either `field` or `link_field.LinkedType.subfield`.

        Returns:
            The data type of the final segment.

        Raises:
            SchemaError: If any segment of the path is invalid.
        """
        parts = path.split('.', 2)
        if len(parts) == 1:
            return self.field_data_type(entity_type, path)
        if len(parts) != LINKED_PATH_SEGMENTS:
            raise SchemaError(
                f'{path} is not a valid field path; use field or '
                'link_field.LinkedEntityType.subfield'
            )
        link_field, linked_type, subfield = parts
        link_data_type = self.field_data_type(entity_type, link_field)
        if link_data_type not in {'entity', 'multi_entity'}:
            raise SchemaError(
                f'{entity_type}.{link_field} is a {link_data_type} field, so it cannot '
                'be used as the link in a dotted field path'
            )
        allowed = self.valid_types(entity_type, link_field) or []
        if allowed and linked_type not in allowed:
            raise SchemaError(
                f'{entity_type}.{link_field} cannot link to {linked_type}; '
                f'valid types are {", ".join(allowed)}'
            )
        return self.resolve_path(linked_type, subfield)

    def resolve_terminal(self, entity_type: str, path: str) -> tuple[str, str]:
        """Return the owning entity type and field name of a path's terminal segment.

        Args:
            entity_type: Entity type the path starts from.
            path: Either `field` or `link_field.LinkedType.subfield`.

        Returns:
            Tuple of (terminal entity type, terminal field name).

        Raises:
            SchemaError: If any segment of the path is invalid.
        """
        parts = path.split('.', 2)
        if len(parts) == 1:
            self._require_field(entity_type, path)
            return entity_type, path
        self.resolve_path(entity_type, path)
        _link_field, linked_type, subfield = parts
        return self.resolve_terminal(linked_type, subfield)

    def valid_values(self, entity_type: str, field_name: str) -> list[str] | None:
        """Return the permitted values of a list or status_list field.

        Args:
            entity_type: Entity type owning the field.
            field_name: Field to look up.

        Returns:
            The permitted values, or `None` for other data types.

        Raises:
            SchemaError: If the field does not exist.
        """
        self._require_field(entity_type, field_name)
        field = self._field(entity_type, field_name)
        field_path = f'{entity_type}.{field_name}'
        properties = _raw_object(
            _raw_required(field, 'properties', field_path),
            f'{field_path}.properties',
        )
        if 'valid_values' not in properties:
            return None
        return list(
            _raw_string_list(
                _raw_value(properties, 'valid_values', f'{field_path}.properties'),
                f'{field_path}.properties.valid_values.value',
            )
        )


class _SchemaCacheObserver(Protocol):
    """Observation callbacks for one request's schema-cache work."""

    def cache_hit(self) -> None:
        """Report one unexpired cache hit."""
        ...

    def cache_miss(self) -> None:
        """Report one cache miss requiring reflection."""
        ...

    def cache_refresh(self) -> None:
        """Report one forced schema refresh."""
        ...

    def provider_call(self) -> None:
        """Report one attempted schema provider call."""
        ...

    def schema_reflection(self) -> contextlib.AbstractContextManager[None]:
        """Return the context manager for one reflection span.

        Returns:
            A context manager that records the reflection span.
        """
        ...


class SchemaCache:
    """Site-level schema cache with TTL and explicit refresh.

    Spec Section 8: TTL default 15 minutes. Refresh-on-validation-miss is
    driven from the seam (validate_with_schema in _validation.py) using
    ValidationError.unknown_name, so ALL referenced names -- entity, fields,
    filter paths, order and grouping fields -- trigger exactly one bounded
    re-reflect. Removed-name staleness surfaces as a backend QueryError and
    heals at the next TTL expiry or explicit refresh (a heuristic
    stale-sniffing hook was considered and cut:
    three freshness mechanisms suffice).
    """

    def __init__(self, connection: SGReadConnection, ttl_seconds: int) -> None:
        """Store the connection and TTL; no reflection happens here."""
        self._connection = connection
        self._ttl_seconds = ttl_seconds
        self._loaded_at = 0.0
        self._schema: SiteSchema | None = None

    def _load(self, *, force_refresh: bool, observer: _SchemaCacheObserver | None) -> SiteSchema:
        """Load the current schema while optionally reporting cache work."""
        if force_refresh:
            if observer is not None:
                observer.cache_refresh()
            self._schema = _reflect(self._connection, observer)
            self._loaded_at = time.monotonic()
            return self._schema
        now = time.monotonic()
        if self._schema is not None and now - self._loaded_at < self._ttl_seconds:
            if observer is not None:
                observer.cache_hit()
            return self._schema
        if observer is not None:
            observer.cache_miss()
        self._schema = _reflect(self._connection, observer)
        self._loaded_at = now
        return self._schema

    def current(self, observer: _SchemaCacheObserver | None = None) -> SiteSchema:
        """Return the cached schema, reflecting if absent or expired.

        Args:
            observer: Optional request-scoped callbacks for cache hits, misses,
                refreshes, provider calls, and the reflection span.

        Returns:
            The current `SiteSchema`.

        Raises:
            QueryError: If the schema provider rejects or fails reflection.
        """
        return self._load(force_refresh=False, observer=observer)

    def refresh(self, observer: _SchemaCacheObserver | None = None) -> SiteSchema:
        """Force a re-reflection and return the fresh schema.

        Args:
            observer: Optional request-scoped callbacks for cache hits, misses,
                refreshes, provider calls, and the reflection span.

        Returns:
            The freshly reflected `SiteSchema`.

        Raises:
            QueryError: If the schema provider rejects or fails reflection.
        """
        return self._load(force_refresh=True, observer=observer)


class SchemaSource(Protocol):
    """Supply cached schema reads and explicit refreshes to query validation."""

    def current(self) -> SiteSchema:
        """Return the current schema, reflecting when needed."""
        ...

    def refresh(self) -> SiteSchema:
        """Force reflection and return the refreshed schema."""
        ...


def _described_cap(values: list[str]) -> tuple[list[str], int]:
    """Return the bounded values and the count of omitted ones."""
    return values[:ENUMERATION_CAP], max(0, len(values) - ENUMERATION_CAP)


def _edit_distance(left: str, right: str) -> int:
    """Return the Levenshtein distance between two strings."""
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[right_index] + 1,
                    current[right_index - 1] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _nearest(needle: str, candidates: list[str]) -> list[str]:
    """Rank candidates by substring containment then edit distance."""
    lowered = needle.lower()
    scored: list[tuple[int, int, str]] = []
    threshold = max(2, int(len(needle) * _SUGGESTION_DISTANCE_RATIO))
    for candidate in candidates:
        lowered_candidate = candidate.lower()
        distance = _edit_distance(lowered, lowered_candidate)
        contains = lowered in lowered_candidate or lowered_candidate in lowered
        if contains:
            scored.append((0, distance, candidate))
        elif distance <= threshold:
            scored.append((1, distance, candidate))
    scored.sort()
    return [candidate for _tier, _distance, candidate in scored[:_SUGGESTION_LIMIT]]


def _raw_object(value: object, path: str) -> dict[str, object]:
    """Return one reflected object or raise a bounded schema error."""
    if not isinstance(value, dict):
        raise SchemaError(f'malformed reflected schema at {path}: expected an object')
    mapping = cast('dict[object, object]', value)
    if any(not isinstance(key, str) for key in mapping):
        raise SchemaError(f'malformed reflected schema at {path}: expected string keys')
    return cast('dict[str, object]', mapping)


def _raw_required(mapping: Mapping[str, object], key: str, path: str) -> object:
    """Return one required reflected value or raise a bounded schema error."""
    if key not in mapping:
        raise SchemaError(f'malformed reflected schema at {path}.{key}: missing value')
    return mapping[key]


def _raw_string(value: object, path: str) -> str:
    """Return one reflected string or raise a bounded schema error."""
    if not isinstance(value, str):
        raise SchemaError(f'malformed reflected schema at {path}: expected a string')
    return value


def _raw_string_list(value: object, path: str) -> list[str]:
    """Return one reflected string list or raise a bounded schema error."""
    if not isinstance(value, list):
        raise SchemaError(f'malformed reflected schema at {path}: expected a list of strings')
    items = cast('list[object]', value)
    if any(not isinstance(item, str) for item in items):
        raise SchemaError(f'malformed reflected schema at {path}: expected a list of strings')
    return cast('list[str]', items)


def _raw_value(mapping: dict[str, object], key: str, path: str) -> object:
    """Return the `value` leaf below one reflected metadata key."""
    envelope = _raw_object(_raw_required(mapping, key, path), f'{path}.{key}')
    return _raw_required(envelope, 'value', f'{path}.{key}')


def _reflect(connection: SGReadConnection, observer: _SchemaCacheObserver | None) -> SiteSchema:
    """Read the full site schema through the connection."""
    shotgun_error = provider_error_type()
    transport_errors = provider_transport_error_types()
    try:
        if observer is not None:
            with observer.schema_reflection():
                observer.provider_call()
                fields = connection.schema_read()
                observer.provider_call()
                entities = connection.schema_entity_read()
            return SiteSchema(fields=fields, entities=entities)
        return SiteSchema(
            fields=connection.schema_read(),
            entities=connection.schema_entity_read(),
        )
    except shotgun_error as error:
        raise QueryError(str(error)) from error
    except transport_errors as error:
        raise transport_query_error(error) from error
