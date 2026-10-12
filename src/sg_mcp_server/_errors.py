"""Shared argument-error guidance for read-only tool surfaces."""

import json
from collections.abc import Mapping
from typing import cast

_BARE_CONDITION_MIN_ITEMS = 3


def bare_condition_hint(arguments: Mapping[str, object]) -> str | None:
    """Return guidance when `filters` looks like one bare condition.

    Args:
        arguments: Rejected tool arguments.

    Returns:
        Wrap-it guidance when applicable, otherwise `None`.
    """
    filters = arguments.get('filters')
    if not isinstance(filters, list):
        return None
    items = cast('list[object]', filters)
    if len(items) < _BARE_CONDITION_MIN_ITEMS:
        return None
    if isinstance(items[0], str) and isinstance(items[1], str):
        return (
            '; filters must be a list of conditions and this looks like one bare '
            f'condition - wrap it in an outer list: [{json.dumps(items, default=str)}]'
        )
    return None
