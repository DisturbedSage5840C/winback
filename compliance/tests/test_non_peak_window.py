"""NPCI OC-215-A restricts mandate execution to non-peak hours.

Peak, in IST: 10:00-13:00 and 17:00-21:30. Written test-first, because the entire
rule is boundary conditions and an off-by-one minute here is a compliance breach that
no amount of downstream cleverness detects.

Convention, fixed here and nowhere else: a peak window is **half-open**, `[start,
end)`. 10:00:00 is peak; 13:00:00 is not. This is the reading that makes two adjacent
windows tile without overlapping or leaving a gap, and it is asserted below rather
than left to the reader.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from compliance.non_peak_window import (
    IST,
    PEAK_WINDOWS,
    SLOT_ANCHORS,
    is_non_peak,
    next_slots,
    time_to_next_transition,
)


def ist(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A moment in September 2026, IST. Day is the day-of-month."""
    return datetime(2026, 9, day, hour, minute, second, tzinfo=IST)


# --------------------------------------------------------------------- boundaries

PEAK_BOUNDARY_CASES = [
    # (hour, minute, is_non_peak, why)
    (9, 59, True, "one minute before the morning peak opens"),
    (10, 0, False, "the morning peak opens on the stroke of 10:00"),
    (10, 1, False, "inside the morning peak"),
    (12, 59, False, "one minute before the morning peak closes"),
    (13, 0, True, "13:00 is the first non-peak minute -- windows are half-open"),
    (16, 59, True, "one minute before the evening peak opens"),
    (17, 0, False, "the evening peak opens on the stroke of 17:00"),
    (21, 29, False, "one minute before the evening peak closes"),
    (21, 30, True, "21:30 is the first non-peak minute"),
    (21, 31, True, "clear of the evening peak"),
    (0, 0, True, "midnight"),
    (3, 0, True, "the deep-night band, when bank batches settle"),
    (23, 59, True, "the last minute of the day"),
]


@pytest.mark.parametrize(("hour", "minute", "expected", "why"), PEAK_BOUNDARY_CASES)
def test_peak_boundaries(hour: int, minute: int, expected: bool, why: str) -> None:
    assert is_non_peak(ist(15, hour, minute)) is expected, why


def test_the_boundary_second_belongs_to_the_peak() -> None:
    """13:00:00 is clear but 12:59:59 is not -- the resolution is the second, not the
    minute, because `attempted_at` is a timestamp and not a clock face."""
    assert is_non_peak(ist(15, 12, 59, 59)) is False
    assert is_non_peak(ist(15, 13, 0, 0)) is True
    assert is_non_peak(ist(15, 21, 29, 59)) is False
    assert is_non_peak(ist(15, 21, 30, 0)) is True


def test_windows_do_not_overlap_and_are_ordered() -> None:
    """A structural check on the constant itself. If someone later adds a third window
    out of order, or overlapping, this fails before any behaviour does."""
    for start, end in PEAK_WINDOWS:
        assert start < end, f"window {start}-{end} is inverted"
    for (_, first_end), (second_start, _) in pairwise(PEAK_WINDOWS):
        assert first_end < second_start, "peak windows overlap or touch"


# --------------------------------------------------------------------- time zones


def test_a_utc_timestamp_is_converted_not_assumed() -> None:
    """04:45 UTC is 10:15 IST -- peak. A system that compared UTC wall-clock against
    an IST rule would call this legal, and would be wrong every single morning."""
    utc_moment = datetime(2026, 9, 15, 4, 45, tzinfo=UTC)
    assert is_non_peak(utc_moment) is False


def test_a_non_ist_zone_is_converted() -> None:
    """00:45 New York on the 15th is 10:15 IST on the 15th -- still peak."""
    ny = datetime(2026, 9, 15, 0, 45, tzinfo=ZoneInfo("America/New_York"))
    assert is_non_peak(ny) is False


def test_a_naive_datetime_is_refused() -> None:
    """Guessing a timezone is how a compliance bug ships. Refuse instead."""
    with pytest.raises(ValueError, match="timezone-aware"):
        is_non_peak(datetime(2026, 9, 15, 10, 15))


# --------------------------------------------------------------------- slot selection


def test_next_slots_returns_only_non_peak_moments() -> None:
    slots = next_slots(ist(15, 9, 0), n=6)
    assert len(slots) == 6
    assert all(is_non_peak(slot) for slot in slots)


def test_next_slots_are_strictly_increasing() -> None:
    slots = next_slots(ist(15, 9, 0), n=8)
    assert slots == sorted(slots)
    assert len(set(slots)) == len(slots)


def test_next_slots_are_all_in_the_future() -> None:
    after = ist(15, 14, 30)
    assert all(slot > after for slot in next_slots(after, n=5))


def test_a_proposal_inside_peak_is_redirected_not_dropped() -> None:
    """The behaviour that earns money: an action proposed at 11:00 is legal, it is
    merely mistimed. Discarding it would forfeit a recovery for no compliance gain."""
    slot = next_slots(ist(15, 11, 0), n=1)[0]
    assert is_non_peak(slot)
    assert slot.date() == ist(15, 0, 0).date(), "the same day still has non-peak room"
    assert slot.hour >= 13


def test_slots_cross_midnight_when_the_day_is_exhausted() -> None:
    """Proposed at 23:50, the next anchors are on the following morning. A window
    calculation that cannot cross midnight silently drops late-evening failures."""
    slots = next_slots(ist(15, 23, 50), n=3)
    assert all(slot.date() > ist(15, 0, 0).date() for slot in slots)
    assert all(is_non_peak(slot) for slot in slots)


def test_slot_anchors_are_themselves_legal() -> None:
    """The anchor grid is a constant, so it can be wrong quietly. Check it directly."""
    for anchor in SLOT_ANCHORS:
        moment = ist(15, anchor.hour, anchor.minute)
        assert is_non_peak(moment), f"anchor {anchor} sits inside a peak window"


def test_slots_span_more_than_one_day_when_many_are_requested() -> None:
    slots = next_slots(ist(15, 2, 0), n=12)
    assert len({slot.date() for slot in slots}) > 1
    assert all(is_non_peak(slot) for slot in slots)


def test_next_slots_rejects_a_non_positive_count() -> None:
    with pytest.raises(ValueError, match="at least one"):
        next_slots(ist(15, 9, 0), n=0)


# --------------------------------------------------------------------- countdown


def test_time_to_next_transition_inside_a_peak_counts_to_its_end() -> None:
    """Drives the dashboard's live countdown. Inside the morning peak at 11:00, the
    next transition is 13:00 -- two hours."""
    assert time_to_next_transition(ist(15, 11, 0)) == timedelta(hours=2)


def test_time_to_next_transition_outside_a_peak_counts_to_the_next_open() -> None:
    assert time_to_next_transition(ist(15, 9, 0)) == timedelta(hours=1)
    assert time_to_next_transition(ist(15, 16, 0)) == timedelta(hours=1)


def test_time_to_next_transition_crosses_midnight() -> None:
    """At 23:00 the next transition is tomorrow's 10:00 -- eleven hours."""
    assert time_to_next_transition(ist(15, 23, 0)) == timedelta(hours=11)


def test_time_to_next_transition_is_never_zero_or_negative() -> None:
    """A zero would render as a stuck countdown; a negative would render as nonsense."""
    for hour in range(24):
        for minute in (0, 30, 59):
            delta = time_to_next_transition(ist(15, hour, minute))
            assert delta > timedelta(0), f"{hour:02d}:{minute:02d} produced {delta}"
