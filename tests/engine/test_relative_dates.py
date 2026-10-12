import datetime
from typing import assert_type
from zoneinfo import ZoneInfo

import pytest

from sg_mcp_server._engine import _relative_dates as relative_dates
from sg_mcp_server._engine._errors import ValidationError
from sg_mcp_server._engine._relative_dates import _shift, calendar_range

_NOW = datetime.datetime.fromisoformat('2026-07-31T12:00:00')
_SHIFT_ZONE = ZoneInfo('America/Los_Angeles')


class TestCalendarRange:
    def test_current_week_is_inclusive_seven_days(self) -> None:
        low, high = calendar_range('in_calendar_week', 0, _NOW)
        assert low.weekday() == 0
        assert high - low == datetime.timedelta(days=7, seconds=-1)

    def test_offset_moves_the_window(self) -> None:
        this_low, _ = calendar_range('in_calendar_week', 0, _NOW)
        last_low, _ = calendar_range('in_calendar_week', -1, _NOW)
        assert this_low - last_low == datetime.timedelta(days=7)


class TestShift:
    def test_day_shift(self) -> None:
        assert _shift(_NOW, -7, 'DAY') == _NOW - datetime.timedelta(days=7)

    @pytest.mark.parametrize(
        ('moment', 'unit', 'expected'),
        [
            (
                datetime.datetime(2025, 1, 31, 12, 34, 56, 789_000, tzinfo=_SHIFT_ZONE),
                'MONTH',
                datetime.datetime(2025, 2, 28, 12, 34, 56, 789_000, tzinfo=_SHIFT_ZONE),
            ),
            (
                datetime.datetime(2024, 1, 31, 12, 34, 56, 789_000, tzinfo=_SHIFT_ZONE),
                'MONTH',
                datetime.datetime(2024, 2, 29, 12, 34, 56, 789_000, tzinfo=_SHIFT_ZONE),
            ),
            (
                datetime.datetime(2025, 12, 31, 12, 34, 56, 789_000, tzinfo=_SHIFT_ZONE),
                'MONTH',
                datetime.datetime(2026, 1, 31, 12, 34, 56, 789_000, tzinfo=_SHIFT_ZONE),
            ),
            (
                datetime.datetime(2024, 2, 29, 12, 34, 56, 789_000, tzinfo=_SHIFT_ZONE),
                'YEAR',
                datetime.datetime(2025, 2, 28, 12, 34, 56, 789_000, tzinfo=_SHIFT_ZONE),
            ),
        ],
        ids=['non-leap-clamp', 'leap-clamp', 'year-rollover', 'leap-day-year-clamp'],
    )
    def test_month_and_year_shifts_have_literal_calendar_results(
        self,
        moment: datetime.datetime,
        unit: str,
        expected: datetime.datetime,
    ) -> None:
        shifted = _shift(moment, 1, unit)

        assert shifted == expected
        assert shifted.tzinfo is moment.tzinfo


def test_calendar_day_bounds_are_aware_and_whole_second() -> None:
    zone = ZoneInfo('America/Los_Angeles')
    now = datetime.datetime(2026, 8, 23, 10, 0, tzinfo=zone)

    start, end = relative_dates.calendar_range('in_calendar_day', 0, now)

    assert start.tzinfo is not None
    assert start.astimezone(datetime.UTC) == datetime.datetime(
        2026, 8, 23, 7, 0, tzinfo=datetime.UTC
    )
    assert end - start == datetime.timedelta(hours=23, minutes=59, seconds=59)


def test_every_calendar_range_ends_at_whole_seconds() -> None:
    now = datetime.datetime(2026, 8, 23, 10, 0, tzinfo=datetime.UTC)
    for operator in relative_dates.CALENDAR_OPERATORS:
        _start, end = relative_dates.calendar_range(operator, 0, now)
        assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
        assert end.tzinfo is not None


def test_field_values_return_iso_strings_for_date_fields() -> None:
    bounds = (
        datetime.datetime(2026, 8, 29, 0, 0, tzinfo=datetime.UTC),
        datetime.datetime(2026, 8, 30, 23, 59, 59, tzinfo=datetime.UTC),
    )

    date_values = relative_dates.field_values(bounds, 'date')
    assert_type(date_values, list[str | datetime.datetime])
    assert date_values == ['2026-08-29', '2026-08-30']


@pytest.mark.parametrize(
    ('operator', 'count', 'unit'),
    [('in_next', 999999, 'YEAR'), ('in_last', 99999999, 'DAY'), ('in_next', 99999999, 'WEEK')],
)
def test_span_overflow_is_a_validation_error(operator: str, count: int, unit: str) -> None:
    now = datetime.datetime(2026, 8, 23, 10, 0, tzinfo=datetime.UTC)

    with pytest.raises(ValidationError, match='supported date range'):
        relative_dates.span_range(operator, count, unit, now)


@pytest.mark.parametrize(
    ('operator', 'offset'),
    [('in_calendar_year', 99999), ('in_calendar_month', 99999999), ('in_calendar_day', 999999999)],
)
def test_calendar_overflow_is_a_validation_error(operator: str, offset: int) -> None:
    now = datetime.datetime(2026, 8, 23, 10, 0, tzinfo=datetime.UTC)

    with pytest.raises(ValidationError, match='supported date range'):
        relative_dates.calendar_range(operator, offset, now)


@pytest.mark.parametrize('operator', relative_dates.SPAN_OPERATORS)
@pytest.mark.parametrize('transition', ['2026-03-08T10:30:00+00:00', '2026-11-01T09:30:00+00:00'])
def test_hour_spans_measure_elapsed_time_across_dst(operator: str, transition: str) -> None:
    anchor = datetime.datetime.fromisoformat(transition)
    if operator.endswith('in_next'):
        anchor -= datetime.timedelta(hours=1)
    now = anchor.astimezone(_SHIFT_ZONE)

    start, end = relative_dates.span_range(operator, 1, 'HOUR', now)

    assert end.astimezone(datetime.UTC) - start.astimezone(datetime.UTC) == datetime.timedelta(
        hours=1
    )
    assert start.tzinfo is _SHIFT_ZONE
    assert end.tzinfo is _SHIFT_ZONE
