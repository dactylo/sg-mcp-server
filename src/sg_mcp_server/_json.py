"""Define the JSON value type shared by schema descriptions and MCP content."""

from typing import TypeAlias

JSONValue: TypeAlias = bool | int | float | str | list['JSONValue'] | dict[str, 'JSONValue'] | None
