"""Turn a caller's words ("next Tuesday at 2pm") into a shop-local day and time.

The voice model passes the caller's words through unchanged and this module does the date
arithmetic, because language models are unreliable at it. All arithmetic is on calendar dates,
never "now + 24 hours", so DST changes can't shift a day or an hour.

Anything that doesn't name exactly one day raises NeedsClarification, whose message is the
question the agent should ask.
"""

import re
from datetime import date, time, timedelta

from app.shop import say_day

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


class NeedsClarification(ValueError):
    """The request can't be acted on as stated. The message says what to ask the caller."""


def parse_when(text: str, today: date) -> tuple[date, time | None]:
    """The day (always) and time (if the caller gave one) that `text` names."""
    words = " ".join(text.lower().split())
    return parse_day(words, today), parse_time(words)


def parse_day(words: str, today: date) -> date:
    """The single day `words` names, judged from `today`."""
    named = sorted({d for d in WEEKDAYS if re.search(rf"\b{d}\b", words)}, key=WEEKDAYS.index)
    if len(named) > 1:
        options = " or ".join(d.title() for d in named)
        raise NeedsClarification(f"The caller mentioned {options}. Ask which one day they want.")

    day = explicit_date(words, today) or relative_date(words, today)
    if day and named and WEEKDAYS[day.weekday()] != named[0]:
        raise NeedsClarification(
            f"{day:%B} {day.day} is a {day:%A}, not a {named[0].title()}. Ask which they meant."
        )
    if day is None and named:
        day = weekday_date(words, named[0], today)
    if day is None:
        raise NeedsClarification(
            f"Couldn't tell which day from '{words}'. Ask for a day, like 'tomorrow' or 'Friday'."
        )
    if day < today:
        raise NeedsClarification(f"{say_day(day)} has already passed. Ask for a future day.")
    return day


def explicit_date(words: str, today: date) -> date | None:
    """A date written out: 2026-11-20, November 20, 20 November, 11/20, or 'the 20th'."""
    try:
        if m := re.search(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", words):  # 2026-11-20T14:00
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.search(rf"\b{MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", words):
            return next_month_day(MONTHS.index(m[1]) + 1, int(m[2]), today)
        if m := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH}", words):
            return next_month_day(MONTHS.index(m[2]) + 1, int(m[1]), today)
        if m := re.search(r"\b(\d{1,2})/(\d{1,2})\b", words):  # US order: month/day
            return next_month_day(int(m[1]), int(m[2]), today)
    except ValueError:  # e.g. February 30
        raise NeedsClarification(f"'{words}' isn't a real date. Ask the caller again.") from None
    if m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", words):
        return next_day_of_month(int(m[1]), today)
    return None


def relative_date(words: str, today: date) -> date | None:
    """today / tomorrow / the day after tomorrow."""
    if "day after tomorrow" in words:
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b", words):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b", words):
        return today
    return None


def weekday_date(words: str, name: str, today: date) -> date:
    """The date of a named weekday.

    'Friday' and 'this Friday' mean the soonest Friday after today. 'Next Friday' means the
    same when that Friday is already in next week; when it is later THIS week, people mean
    different things by it, so we ask instead of guessing.
    """
    target = WEEKDAYS.index(name)
    coming = today + timedelta(days=(target - today.weekday()) % 7 or 7)
    a_week_later = coming + timedelta(days=7)
    if re.search(rf"\b{name} after next\b", words):
        return a_week_later
    if "next week" in words:
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=target)
    if re.search(rf"\bnext {name}\b", words) and same_week(coming, today):
        raise NeedsClarification(
            f"'next {name.title()}' could mean {say_day(coming)} or {say_day(a_week_later)}. "
            "Ask the caller which one."
        )
    return coming


def parse_time(words: str) -> time | None:
    """The time of day in `words`, or None if there isn't one.

    A bare hour from 1 to 7 ("at 3") is read as afternoon: the shop opens at 8 AM.
    """
    if re.search(r"\b(noon|midday)\b", words):
        return time(12)
    if m := re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b", words):  # 2pm, 2:30 p.m.
        hour, minute = int(m[1]), int(m[2] or 0)
        if not 1 <= hour <= 12:
            raise NeedsClarification(f"'{m[0]}' isn't a real time. Ask the caller again.")
        hour = hour % 12 + (12 if m[3] == "p" else 0)
    elif m := re.search(r"(?<!\d)(\d{1,2}):(\d{2})\b", words):  # 14:00, 2:30
        hour, minute = afternoon_guess(int(m[1])), int(m[2])
    elif m := re.search(r"\b(?:at (\d{1,2})|(\d{1,2}) ?o'?clock)\b(?!\s*(?:st|nd|rd|th|/|-))",
                        words):  # at 3, 3 o'clock
        hour, minute = afternoon_guess(int(m[1] or m[2])), 0
    else:
        return None
    if hour > 23 or minute > 59:
        raise NeedsClarification(f"'{m[0]}' isn't a real time. Ask the caller again.")
    return time(hour, minute)


def afternoon_guess(hour: int) -> int:
    """3 -> 15: nobody books a car repair for 3 AM."""
    return hour + 12 if 1 <= hour <= 7 else hour


def next_month_day(month: int, day: int, today: date) -> date:
    """The next `month`/`day` on or after today (this year, or next year if it has passed)."""
    candidate = date(today.year, month, day)
    return candidate if candidate >= today else date(today.year + 1, month, day)


def next_day_of_month(day: int, today: date) -> date:
    """The next date whose day-of-month is `day`: 'the 3rd' said on Nov 16 is Dec 3."""
    year, month = today.year, today.month
    for _ in range(3):  # 3 months always contain a 29th, 30th or 31st
        try:
            candidate = date(year, month, day)
            if candidate >= today:
                return candidate
        except ValueError:
            pass  # this month has no such day
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise NeedsClarification(f"There's no day {day} in the next few months. Ask again.")


def same_week(a: date, b: date) -> bool:
    """Both dates fall in the same Monday-to-Sunday week."""
    return a.isocalendar()[:2] == b.isocalendar()[:2]
