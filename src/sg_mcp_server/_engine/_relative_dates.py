"""Relative-date arithmetic against an injected query clock."""

import calendar
import datetime

from sg_mcp_server._engine._errors import ValidationError

CALENDAR_OPERATORS: tuple[str, ...] = (
    'in_calendar_day',
    'in_calendar_month',
    'in_calendar_week',
    'in_calendar_year',
)
RELATIVE_UNITS: tuple[str, ...] = ('HOUR', 'DAY', 'WEEK', 'MONTH', 'YEAR')
SPAN_OPERATORS: tuple[str, ...] = ('in_last', 'in_next', 'not_in_last', 'not_in_next')


class _RelativeDateError(ValidationError):
    """Report a relative-date relation that SG itself would reject."""


def _period_bounds(
    start_date: datetime.date,
    end_date: datetime.date,
    zone: datetime.tzinfo | None,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Return aware whole-second inclusive bounds spanning two dates."""
    return (
        datetime.datetime.combine(start_date, datetime.time.min, tzinfo=zone),
        datetime.datetime.combine(end_date, datetime.time(23, 59, 59), tzinfo=zone),
    )


def _shift(moment: datetime.datetime, count: int, unit: str) -> datetime.datetime:
    """Return a timestamp moved by a signed number of calendar units."""
    normalized = unit.upper()
    try:
        if normalized == 'HOUR':
            shifted = moment.astimezone(datetime.UTC) + datetime.timedelta(hours=count)
            return shifted.astimezone(moment.tzinfo)
        if normalized == 'DAY':
            return moment + datetime.timedelta(days=count)
        if normalized == 'WEEK':
            return moment + datetime.timedelta(weeks=count)
        if normalized == 'MONTH':
            total = moment.year * 12 + (moment.month - 1) + count
            year, month = divmod(total, 12)
            day = min(moment.day, calendar.monthrange(year, month + 1)[1])
            return moment.replace(year=year, month=month + 1, day=day)
        if normalized == 'YEAR':
            year = moment.year + count
            day = min(moment.day, calendar.monthrange(year, moment.month)[1])
            return moment.replace(year=year, day=day)
    except (OverflowError, ValueError) as error:
        raise _RelativeDateError(
            f'shifting {unit!r} reaches beyond the supported date range; use a smaller count'
        ) from error
    raise _RelativeDateError(
        f'{unit!r} is not a valid time unit; use one of {", ".join(RELATIVE_UNITS)}'
    )


def calendar_range(
    operator: str,
    offset: int,
    now: datetime.datetime,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Return the inclusive bounds of a calendar day, week, month or year.

    Args:
        operator: One of `CALENDAR_OPERATORS`.
        offset: Signed offset from the current period; `0` is the current one.
        now: Aware configured-zone timestamp anchoring the requested period.

    Returns:
        Start and end timestamps of the requested period.

    Raises:
        _RelativeDateError: If the operator is not a calendar relation or its
            arithmetic exceeds the supported date range.
    """
    try:
        if operator == 'in_calendar_day':
            target = now.date() + datetime.timedelta(days=offset)
            return _period_bounds(target, target, now.tzinfo)
        if operator == 'in_calendar_week':
            monday = now.date() - datetime.timedelta(days=now.weekday())
            start_date = monday + datetime.timedelta(weeks=offset)
            return _period_bounds(
                start_date,
                start_date + datetime.timedelta(days=6),
                now.tzinfo,
            )
        if operator == 'in_calendar_month':
            total = now.year * 12 + (now.month - 1) + offset
            year, month = divmod(total, 12)
            start_date = datetime.date(year, month + 1, 1)
            end_date = datetime.date(year, month + 1, calendar.monthrange(year, month + 1)[1])
            return _period_bounds(start_date, end_date, now.tzinfo)
        if operator == 'in_calendar_year':
            year = now.year + offset
            return _period_bounds(
                datetime.date(year, 1, 1),
                datetime.date(year, 12, 31),
                now.tzinfo,
            )
    except (OverflowError, ValueError) as error:
        raise _RelativeDateError(
            f'{operator} reaches beyond the supported date range; use a smaller offset'
        ) from error
    raise _RelativeDateError(f'{operator!r} is not a calendar relation')


def field_values(
    bounds: tuple[datetime.datetime, datetime.datetime],
    data_type: str,
) -> list[str | datetime.datetime]:
    """Convert a timestamp range into the literals a field's data type accepts.

    Args:
        bounds: Inclusive start and end timestamps.
        data_type: `date` or `date_time`.

    Returns:
        Two literals: ISO date strings for `date`, datetimes for `date_time`.

    Raises:
        _RelativeDateError: If the data type cannot hold a relative-date range.
    """
    start, end = bounds
    if data_type == 'date':
        return [start.date().isoformat(), end.date().isoformat()]
    if data_type == 'date_time':
        return [start, end]
    raise _RelativeDateError(
        f'relative-date relations only apply to date and date_time fields, not {data_type}'
    )


def rewritten_relation(operator: str) -> str:
    """Return the concrete SG relation a relative operator becomes.

    Args:
        operator: A span or calendar relative operator.

    Returns:
        `between` for inclusive operators, `not_between` for negated ones.
    """
    return 'not_between' if operator.startswith('not_') else 'between'


def span_range(
    operator: str,
    count: object,
    unit: str,
    now: datetime.datetime,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Return the inclusive bounds of an `in_last` / `in_next` style window.

    Args:
        operator: One of `SPAN_OPERATORS`.
        count: Positive whole number of units in the window.
        unit: One of `RELATIVE_UNITS`; hours measure elapsed time while other
            units follow the configured calendar.
        now: Aware configured-zone timestamp anchoring the requested window.

    Returns:
        Start and end timestamps of the window.

    Raises:
        _RelativeDateError: If the operator or count is invalid, or if
            `_shift()` rejects the unit or exceeds the supported date range.
    """
    if operator not in SPAN_OPERATORS:
        raise _RelativeDateError(f'{operator!r} is not a relative time-span relation')
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise _RelativeDateError(
            f'{operator} needs a positive whole number of units, got {count!r}'
        )
    if operator.endswith('in_last'):
        return _shift(now, -count, unit), now
    return now, _shift(now, count, unit)
