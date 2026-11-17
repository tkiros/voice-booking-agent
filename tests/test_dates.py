"""Relative dates are resolved on the server, in the shop's time zone. No database needed."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app import shop
from app.dates import NeedsClarification, parse_when

MONDAY = date(2026, 11, 16)


@pytest.mark.parametrize("words, day, clock", [
    ("tomorrow at 10am", date(2026, 11, 17), time(10)),
    ("today at 4 pm", MONDAY, time(16)),
    ("the day after tomorrow at 9", date(2026, 11, 18), time(9)),
    ("Friday at 2pm", date(2026, 11, 20), time(14)),
    ("this Friday", date(2026, 11, 20), None),
    ("Monday", date(2026, 11, 23), None),  # said on a Monday: next week's
    ("next Monday at 11am", date(2026, 11, 23), time(11)),  # already next week: no ambiguity
    ("Tuesday next week", date(2026, 11, 24), None),
    ("Tuesday after next", date(2026, 11, 24), None),
    ("Tuesday the 24th at 9am", date(2026, 11, 24), time(9)),
    ("November 30 at 8am", date(2026, 11, 30), time(8)),
    ("nov 30th", date(2026, 11, 30), None),
    ("the 24th of November at 10 o'clock", date(2026, 11, 24), time(10)),
    ("11/30 at 2:30 pm", date(2026, 11, 30), time(14, 30)),
    ("2026-11-20T14:00", date(2026, 11, 20), time(14)),
    ("January 5 at noon", date(2027, 1, 5), time(12)),  # already past this year
    ("the 3rd", date(2026, 12, 3), None),  # day-of-month already past in November
    ("the 31st", date(2026, 12, 31), None),  # November has no 31st
    ("Thursday at 3", date(2026, 11, 19), time(15)),  # a bare 3 means 3 PM at a garage
    ("Thursday 3 p.m.", date(2026, 11, 19), time(15)),
    ("Thursday at 12:00", date(2026, 11, 19), time(12)),
])
def test_resolves(words, day, clock):
    assert parse_when(words, MONDAY) == (day, clock)


@pytest.mark.parametrize("words, question", [
    ("next Tuesday at 9", "could mean Tuesday, November 17 or Tuesday, November 24"),
    ("Monday or Tuesday", "Monday or Tuesday"),
    ("Wednesday the 19th", "November 19 is a Thursday, not a Wednesday"),
    ("sometime next week", "Ask for a day"),
    ("February 30", "isn't a real date"),
    ("2026-11-01", "already passed"),
    ("Friday at 13pm", "isn't a real time"),
    ("Friday at 25:00", "isn't a real time"),
])
def test_asks_instead_of_guessing(words, question):
    with pytest.raises(NeedsClarification, match=question):
        parse_when(words, MONDAY)


def test_next_weekday_later_this_week_is_ambiguous_but_next_week_is_not():
    friday = date(2026, 11, 20)
    with pytest.raises(NeedsClarification, match="November 21 or Saturday, November 28"):
        parse_when("next Saturday", friday)
    assert parse_when("next Tuesday", friday) == (date(2026, 11, 24), None)


# --- DST: US clocks went forward on 8 March 2026 and back on 1 November 2026 ------------------


def test_9am_keeps_meaning_9am_across_spring_forward():
    saturday = date(2026, 3, 7)  # EST, UTC-5
    day, clock = parse_when("Monday at 9am", saturday)
    start = shop.at(day, clock)
    assert start.utcoffset() == timedelta(hours=-4)  # EDT now
    assert shop.slot_range(start) == "[2026-03-09T13:00:00+00:00,2026-03-09T14:00:00+00:00)"
    # The classic bug: adding 48 hours to Saturday 9 AM lands on Monday 10 AM.
    naive = shop.at(saturday, time(9)).astimezone(UTC) + timedelta(hours=48)
    assert naive.astimezone(shop.TZ).hour == 10


def test_9am_keeps_meaning_9am_across_fall_back():
    friday = date(2026, 10, 30)  # EDT, UTC-4
    day, clock = parse_when("Monday at 9am", friday)
    start = shop.at(day, clock)
    assert start.utcoffset() == timedelta(hours=-5)  # EST now
    assert shop.slot_range(start) == "[2026-11-02T14:00:00+00:00,2026-11-02T15:00:00+00:00)"


def test_tomorrow_is_a_calendar_day_even_on_a_25_hour_day():
    late_saturday = date(2026, 10, 31)
    assert parse_when("tomorrow", late_saturday)[0] == date(2026, 11, 1)  # 25 hours long


def test_every_slot_is_one_hour_on_dst_weeks():
    for day in (date(2026, 3, 9), date(2026, 11, 2)):
        for start in shop.slots(day):
            begin, end = shop.slot_range(start)[1:-1].split(",")
            length = datetime.fromisoformat(end) - datetime.fromisoformat(begin)
            assert length == timedelta(hours=1)


def test_times_read_back_in_shop_time_whatever_zone_they_come_from():
    assert shop.say(datetime(2026, 3, 9, 13, tzinfo=UTC)) == "Monday, March 9 at 9:00 AM"
    assert shop.say(datetime(2026, 11, 2, 14, tzinfo=UTC)) == "Monday, November 2 at 9:00 AM"


@pytest.mark.parametrize("when, reason", [
    (datetime(2026, 11, 16, 8, 0), "already passed"),  # now is 8:30
    (datetime(2026, 11, 22, 10, 0), "closed on Sundays"),
    (datetime(2026, 11, 17, 19, 0), "open 8:00 AM to 5:00 PM"),
    (datetime(2026, 11, 21, 14, 0), "open 9:00 AM to 1:00 PM"),  # Saturday
    (datetime(2026, 11, 17, 10, 30), "on the hour"),
])
def test_why_not_bookable(when, reason):
    now = datetime(2026, 11, 16, 8, 30, tzinfo=shop.TZ)
    assert reason in shop.why_not_bookable(when.replace(tzinfo=shop.TZ), now)


def test_the_hour_skipped_by_spring_forward_is_never_bookable():
    """2:30 AM on 8 March 2026 never happened. US clocks change at 2 AM on a Sunday, when the
    shop is closed anyway, so no slot can ever be a time that doesn't exist (or exists twice)."""
    before = datetime(2026, 3, 1, 9, tzinfo=shop.TZ)
    assert "closed on Sundays" in shop.why_not_bookable(
        datetime(2026, 3, 8, 2, 30, tzinfo=shop.TZ), before)
    assert all(s.hour >= 8 for s in shop.slots(date(2026, 3, 9)))


def test_a_real_slot_is_bookable():
    now = datetime(2026, 11, 16, 8, 30, tzinfo=shop.TZ)
    assert shop.why_not_bookable(datetime(2026, 11, 16, 9, tzinfo=shop.TZ), now) is None
