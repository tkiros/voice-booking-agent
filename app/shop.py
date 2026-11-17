"""The fictional business this backend books for: who can be booked, when, and in which time zone.

Every time the caller hears is shop-local (America/New_York). Every time stored in Postgres is
UTC. Converting at the edges is what keeps "9 AM" meaning 9 AM on both sides of a DST change.
"""

import os
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NAME = "Maple Street Auto Repair"  # fictional
TZ = ZoneInfo("America/New_York")
SLOT = timedelta(hours=1)  # every appointment is one hour and starts on the hour

# resource_id -> mechanic. These are the bookable resources.
MECHANICS = {1: "Dana", 2: "Luis", 3: "Priya"}

# Weekday (Monday=0) -> (opening hour, closing hour), 24-hour clock. Sunday is missing: closed.
HOURS = {0: (8, 17), 1: (8, 17), 2: (8, 17), 3: (8, 17), 4: (8, 17), 5: (9, 13)}


def now() -> datetime:
    """The current shop-local time. FAKE_NOW freezes it so the scenario harness is repeatable."""
    fake = os.environ.get("FAKE_NOW")
    if fake:
        return datetime.fromisoformat(fake).replace(tzinfo=TZ)
    return datetime.now(TZ)


def at(day: date, clock: time) -> datetime:
    """A wall-clock time on `day` in the shop's time zone (zoneinfo picks the right UTC offset)."""
    return datetime.combine(day, clock, TZ)


def slots(day: date) -> list[datetime]:
    """Every bookable start time on `day`, earliest first. Empty when the shop is closed."""
    if day.weekday() not in HOURS:
        return []
    opens, closes = HOURS[day.weekday()]
    return [at(day, time(hour)) for hour in range(opens, closes)]


def why_not_bookable(start: datetime, now: datetime) -> str | None:
    """A short reason `start` can't be booked, or None when it is a real future slot."""
    if start <= now:
        return "that time has already passed"
    hours = HOURS.get(start.weekday())
    if hours is None:
        return f"we're closed on {start:%A}s"
    if start.minute or start.second:
        return "appointments start on the hour"
    opens, closes = hours
    if not opens <= start.hour < closes:
        return f"on {start:%A}s we're open {say_hour(opens)} to {say_hour(closes)}"
    return None


def slot_range(start: datetime) -> str:
    """The tstzrange literal for the slot starting at `start`, written in UTC.

    The end is computed in UTC so the slot is exactly one hour long even on a DST change day.
    """
    begin = start.astimezone(UTC)
    return f"[{begin.isoformat()},{(begin + SLOT).isoformat()})"


def say_day(day: date) -> str:
    """'Friday, November 20' -- how the agent should read a date back to the caller."""
    return f"{day:%A, %B} {day.day}"


def say_hour(hour: int) -> str:
    """'9:00 AM' for 9, '5:00 PM' for 17."""
    return time(hour).strftime("%I:%M %p").lstrip("0")


def say(moment: datetime) -> str:
    """'Friday, November 20 at 2:00 PM' in shop-local time, whatever zone `moment` is in."""
    local = moment.astimezone(TZ)
    return f"{say_day(local)} at {local:%I:%M %p}".replace(" at 0", " at ")
