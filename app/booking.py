"""The booking tools. Platform-agnostic: plain arguments in, a plain dict out.

Every tool returns {"ok": bool, "message": str}. The message is one sentence the voice agent can
act on: read it out, ask the question in it, or offer the alternatives in it. A Vapi or Retell
adapter only has to move those two fields into its own reply format.

Double-booking is prevented by Postgres (the exclusion constraint on `bookings`), not by the
checks in this file. The checks only decide what to SAY; the constraint decides what HAPPENS.
"""

from datetime import date, datetime, time, timedelta

from psycopg import Rollback

from app import outbox, shop
from app.dates import NeedsClarification, parse_when

ALTERNATIVES = 3  # how many other times to offer when the wanted one isn't free
SEARCH_DAYS = 7  # how far either side of the wanted day to look for them
ANYONE = ("", "any", "anyone", "anybody", "no preference")


def create_booking(conn, resource_id, during, caller):
    """Book `resource_id` for `during`, idempotent on (resource, slot, caller).

    A retry of a booking that already succeeded returns that booking ("duplicate"); anyone else
    gets "slot_taken". Postgres's exclusion constraint is what makes a double-booking
    impossible; this function only decides what to answer.

    Derived from the build guide's version, which caught ExclusionViolation instead of using
    ON CONFLICT DO NOTHING. Under a 20-way race that version raised DeadlockDetected for some
    requests: an exclusion constraint is checked after the row is inserted, so two inserts can
    wait on each other. ON CONFLICT DO NOTHING (which, with no target, also covers exclusion
    constraints) waits for the other insert to finish instead, so every request gets an answer.
    The second lookup relies on READ COMMITTED, where each statement sees newly committed rows.
    """
    find = ("SELECT id FROM bookings WHERE resource_id=%s AND during=%s::tstzrange "
            "AND caller=%s AND status='confirmed'")
    if row := conn.execute(find, (resource_id, during, caller)).fetchone():
        return {"ok": True, "booking_id": row[0], "duplicate": True}
    inserted = conn.execute(
        "INSERT INTO bookings (resource_id, during, caller) VALUES (%s, %s::tstzrange, %s)"
        " ON CONFLICT DO NOTHING RETURNING id",
        (resource_id, during, caller)).fetchone()
    if inserted:
        return {"ok": True, "booking_id": inserted[0], "duplicate": False}
    if row := conn.execute(find, (resource_id, during, caller)).fetchone():
        return {"ok": True, "booking_id": row[0], "duplicate": True}  # lost to our own retry
    return {"ok": False, "reason": "slot_taken"}


# --- the tools ---------------------------------------------------------------------------------


def check_availability(conn, when: str, mechanic: str = "", *, now: datetime) -> dict:
    """Is this day, or this exact time, open? Offers the nearest alternatives when it isn't."""
    try:
        ids = mechanic_ids(mechanic)
        day, clock = parse_when(when, now.date())
    except NeedsClarification as e:
        return fail(str(e))

    if clock is None:
        times = open_times(conn, day, ids, now)
        if times:
            return ok(f"Open times on {shop.say_day(day)}: {times}.")
        noon = shop.at(day, time(12))
        return ok(f"Nothing is open on {shop.say_day(day)}. "
                  f"Nearest open times: {nearest_open(conn, noon, ids, now)}.")

    start = shop.at(day, clock)
    if reason := shop.why_not_bookable(start, now):
        return ok(f"{shop.say(start)} can't be booked: {reason}. "
                  f"Nearest open times: {nearest_open(conn, start, ids, now)}.")
    free = open_slots(conn, [start], ids)
    if free:
        return ok(f"{shop.say(start)} is open with {names(free[0][1])}.")
    return ok(f"{shop.say(start)} is already booked. "
              f"Nearest open times: {nearest_open(conn, start, ids, now)}.")


def book(conn, caller: str, when: str, mechanic: str = "", email: str = "", *,
         now: datetime) -> dict:
    """Book the slot `when` names with `mechanic`, or with whichever mechanic is free."""
    try:
        ids = mechanic_ids(mechanic)
        start = wanted_start(conn, when, ids, now)
    except NeedsClarification as e:
        return fail(str(e))
    if reason := shop.why_not_bookable(start, now):
        return fail(f"Can't book {shop.say(start)}: {reason}. "
                    f"Nearest open times: {nearest_open(conn, start, ids, now)}.")

    during = shop.slot_range(start)
    if mine := own_booking(conn, caller, during, ids):  # a retry, or the caller asking twice
        return ok(booked_message(start, *mine, repeat=True))

    with conn.transaction():  # the booking and its confirmation commit together, or neither
        result = first_free(conn, ids, during, caller)
        if result["ok"] and not result["duplicate"]:
            body = confirmation(start, result["booking_id"], result["resource_id"])
            outbox.add(conn, "confirmed", caller, body, result["booking_id"], email)
    if not result["ok"]:
        return fail(f"{shop.say(start)} is already booked. "
                    f"Nearest open times: {nearest_open(conn, start, ids, now)}.")
    message = booked_message(start, result["booking_id"], result["resource_id"],
                             repeat=result["duplicate"])
    return ok(message + email_note(email))


def reschedule(conn, caller: str, when: str, booking_id: int = 0, mechanic: str = "", *,
               now: datetime) -> dict:
    """Move one of the caller's upcoming bookings to the slot `when` names.

    The old booking is released and the new one taken in ONE transaction: if the new slot is
    taken, the rollback puts the old booking back, so the caller never ends up with neither.
    """
    try:
        ids = mechanic_ids(mechanic)
        start = wanted_start(conn, when, ids, now)
    except NeedsClarification as e:
        return fail(str(e))
    if reason := shop.why_not_bookable(start, now):
        return fail(f"Can't move to {shop.say(start)}: {reason}. "
                    f"Nearest open times: {nearest_open(conn, start, ids, now)}.")

    during = shop.slot_range(start)
    if mine := own_booking(conn, caller, during, ids):  # a retry of a move that already worked
        return ok(booked_message(start, *mine, repeat=True))
    try:
        old_id, old_resource, old_start = pick_booking(conn, caller, booking_id, now)
    except NeedsClarification as e:
        return fail(str(e))
    if old_start == start and old_resource in ids:  # a copy of this request committed just now
        return ok(booked_message(start, old_id, old_resource, repeat=True))
    if not mechanic:  # keep the same mechanic when they're free
        ids = [old_resource] + [r for r in ids if r != old_resource]

    result = {"ok": False}
    with conn.transaction():
        released = conn.execute(
            "UPDATE bookings SET status = 'rescheduled' WHERE id = %s AND status = 'confirmed'"
            " RETURNING id", (old_id,)).fetchone()
        if released:
            result = first_free(conn, ids, during, caller)
        if not result["ok"]:
            raise Rollback()  # undo the release: the old booking stays confirmed
        if not result["duplicate"]:
            body = confirmation(start, result["booking_id"], result["resource_id"])
            outbox.add(conn, "rescheduled", caller, body, result["booking_id"],
                       outbox.email_for(conn, old_id))

    if result["ok"]:
        return ok(f"Moved booking number {old_id} from {shop.say(old_start)} to "
                  f"{shop.say(start)} with {shop.MECHANICS[result['resource_id']]}. "
                  f"The new booking number is {result['booking_id']}.")
    if not released:  # a concurrent retry of this same request released it first
        if mine := own_booking(conn, caller, during, ids):
            return ok(booked_message(start, *mine, repeat=True))
        return fail(f"Booking number {old_id} was changed a moment ago. Ask the caller to "
                    "confirm what they have now.")
    return fail(f"{shop.say(start)} is already booked, so booking number {old_id} stays at "
                f"{shop.say(old_start)}. "
                f"Nearest open times: {nearest_open(conn, start, ids, now)}.")


def cancel(conn, caller: str, booking_id: int = 0, *, now: datetime) -> dict:
    """Cancel one of the caller's bookings. Cancelling twice is fine: the second time says so."""
    if booking_id:
        row = conn.execute(
            "SELECT id, lower(during), status FROM bookings WHERE id = %s AND caller = %s",
            (booking_id, caller)).fetchone()
        if row is None:
            return fail(f"There's no booking number {booking_id} for this phone number.")
    else:
        bookings = upcoming(conn, caller, now)
        if len(bookings) > 1:
            return fail(which_one(bookings))
        if bookings:
            row = (bookings[0][0], bookings[0][2], "confirmed")
        else:  # nothing confirmed: maybe this is a retry of a cancel that already worked
            row = conn.execute(
                "SELECT id, lower(during), status FROM bookings WHERE caller = %s"
                " AND status = 'cancelled' AND upper(during) > %s ORDER BY id DESC LIMIT 1",
                (caller, now)).fetchone()
            if row is None:
                return fail("There's no upcoming booking for this phone number.")

    found_id, start, status = row
    if status == "rescheduled":
        return fail(f"Booking number {found_id} was moved to another time. Cancel the new "
                    "booking instead.")
    with conn.transaction():
        cancelled = conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = %s AND status = 'confirmed'"
            " RETURNING id", (found_id,)).fetchone()
        if cancelled:
            outbox.add(conn, "cancelled", caller,
                       f"{shop.NAME}: your appointment on {shop.say(start)} "
                       f"(booking number {found_id}) is cancelled.",
                       found_id, outbox.email_for(conn, found_id))
    if cancelled:
        return ok(f"Cancelled booking number {found_id} on {shop.say(start)}.")
    return ok(f"Booking number {found_id} on {shop.say(start)} is already cancelled.")


def request_callback(conn, caller: str, reason: str) -> dict:
    """Log that the caller wants a person to call them back (the human hand-off)."""
    with conn.transaction():
        outbox.add(conn, "callback", caller, f"Call back {caller}: {reason}")
    return ok("Callback request logged. A staff member will call back during opening hours.")


# --- helpers -----------------------------------------------------------------------------------


def ok(message: str) -> dict:
    return {"ok": True, "message": message}


def fail(message: str) -> dict:
    return {"ok": False, "message": message}


def mechanic_ids(name: str) -> list[int]:
    """[that mechanic's resource_id], or every mechanic when the caller has no preference."""
    wanted = name.strip().lower()
    if wanted in ANYONE:
        return list(shop.MECHANICS)
    for resource_id, mechanic in shop.MECHANICS.items():
        if mechanic.lower() == wanted:
            return [resource_id]
    raise NeedsClarification(
        f"There's no mechanic called {name}. Our mechanics are {names(shop.MECHANICS)}."
    )


def names(resource_ids) -> str:
    """'Dana, Luis and Priya'."""
    people = [shop.MECHANICS[r] for r in resource_ids]
    return people[0] if len(people) == 1 else ", ".join(people[:-1]) + " and " + people[-1]


def wanted_start(conn, when: str, ids: list[int], now: datetime) -> datetime:
    """The exact start time `when` names. Booking needs a time, so no time means: ask for one."""
    day, clock = parse_when(when, now.date())
    if clock is None:
        times = open_times(conn, day, ids, now) or "none that day"
        raise NeedsClarification(
            f"Ask the caller what time on {shop.say_day(day)}. Open times: {times}."
        )
    return shop.at(day, clock)


def open_slots(conn, starts: list[datetime], ids: list[int]) -> list[tuple[datetime, list[int]]]:
    """Which of `starts` still have a free mechanic among `ids`, and which mechanics."""
    if not starts:
        return []
    taken = set(conn.execute(
        "SELECT resource_id, lower(during) FROM bookings"
        " WHERE status = 'confirmed' AND during && tstzrange(%s, %s)",
        (min(starts), max(starts) + shop.SLOT)).fetchall())
    result = []
    for start in starts:
        free = [r for r in ids if (r, start) not in taken]
        if free:
            result.append((start, free))
    return result


def open_times(conn, day: date, ids: list[int], now: datetime) -> str:
    """'8:00 AM, 9:00 AM, 1:00 PM' -- the open start times on `day`, or '' if none."""
    future = [s for s in shop.slots(day) if s > now]
    return ", ".join(shop.say_hour(s.hour) for s, _ in open_slots(conn, future, ids))


def nearest_open(conn, wanted: datetime, ids: list[int], now: datetime) -> str:
    """The open slots closest to `wanted` (same day first), in time order, as one phrase."""
    first = max(now.date(), wanted.date() - timedelta(days=SEARCH_DAYS))
    last = wanted.date() + timedelta(days=SEARCH_DAYS)
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    starts = [s for day in days for s in shop.slots(day) if s > now]
    options = open_slots(conn, starts, ids)
    options.sort(key=lambda o: (o[0].date() != wanted.date(), abs(o[0] - wanted)))
    best = sorted(options[:ALTERNATIVES])
    if not best:
        return "none within a week; offer a callback"
    return "; ".join(f"{shop.say(start)} with {names(free)}" for start, free in best)


def own_booking(conn, caller: str, during: str, ids: list[int]) -> tuple[int, int] | None:
    """(booking_id, resource_id) if this caller already holds this slot with one of `ids`."""
    return conn.execute(
        "SELECT id, resource_id FROM bookings WHERE caller = %s AND during = %s::tstzrange"
        " AND status = 'confirmed' AND resource_id = ANY(%s)",
        (caller, during, ids)).fetchone()


def first_free(conn, ids: list[int], during: str, caller: str) -> dict:
    """create_booking with each mechanic in turn until one succeeds.

    Always the same order, so two copies of one request race for the SAME row: one inserts it
    and the other finds it, instead of each grabbing a different mechanic.
    """
    for resource_id in ids:
        result = create_booking(conn, resource_id, during, caller)
        if result["ok"]:
            return result | {"resource_id": resource_id}
    return {"ok": False, "reason": "slot_taken"}


def upcoming(conn, caller: str, now: datetime) -> list[tuple[int, int, datetime]]:
    """The caller's confirmed future bookings: (booking_id, resource_id, start), soonest first."""
    return conn.execute(
        "SELECT id, resource_id, lower(during) FROM bookings WHERE caller = %s"
        " AND status = 'confirmed' AND upper(during) > %s ORDER BY lower(during)",
        (caller, now)).fetchall()


def pick_booking(conn, caller: str, booking_id: int, now: datetime) -> tuple[int, int, datetime]:
    """The one upcoming booking the caller means: the numbered one, or their only one."""
    bookings = upcoming(conn, caller, now)
    if booking_id:
        bookings = [b for b in bookings if b[0] == booking_id]
        if not bookings:
            raise NeedsClarification(
                f"There's no upcoming booking number {booking_id} for this phone number.")
    if not bookings:
        raise NeedsClarification("There's no upcoming booking for this phone number.")
    if len(bookings) > 1:
        raise NeedsClarification(which_one(bookings))
    return bookings[0]


def which_one(bookings) -> str:
    listed = "; ".join(f"number {b[0]} on {shop.say(b[2])}" for b in bookings)
    return f"This phone number has several bookings: {listed}. Ask which one and pass its number."


def booked_message(start: datetime, booking_id: int, resource_id: int, repeat: bool) -> str:
    mechanic = shop.MECHANICS[resource_id]
    if repeat:
        return (f"Already booked: {shop.say(start)} with {mechanic}, "
                f"booking number {booking_id}.")
    return f"Booked {shop.say(start)} with {mechanic}. Booking number {booking_id}."


def confirmation(start: datetime, booking_id: int, resource_id: int) -> str:
    return (f"{shop.NAME}: your appointment with {shop.MECHANICS[resource_id]} on "
            f"{shop.say(start)} is confirmed (booking number {booking_id}). "
            "Call us to change or cancel it.")


def email_note(email: str) -> str:
    if not email:
        return ""
    if outbox.EMAIL.fullmatch(email.strip()):
        return " A confirmation email is on its way."
    return " That email address didn't look right, so no email will be sent; ask them to spell it."
