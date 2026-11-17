"""The booking tools against a real Postgres. Run `make up` first; these wipe the booking tables."""

from datetime import datetime

from app import booking, shop
from tests.conftest import NOW, confirmed, outbox_kinds

ANA, BEN, CAL = "+15550100001", "+15550100002", "+15550100003"


def book(conn, caller, when, **kw):
    return booking.book(conn, caller, when, now=NOW, **kw)


# --- create_booking (the primitive) ---------------------------------------------------------


def test_create_booking_is_idempotent_for_the_same_caller(conn):
    slot = shop.slot_range(datetime(2026, 11, 20, 14, tzinfo=shop.TZ))
    first = booking.create_booking(conn, 2, slot, ANA)
    again = booking.create_booking(conn, 2, slot, ANA)
    other = booking.create_booking(conn, 2, slot, BEN)
    assert first == {"ok": True, "booking_id": 1, "duplicate": False}
    assert again == {"ok": True, "booking_id": 1, "duplicate": True}
    assert other == {"ok": False, "reason": "slot_taken"}


def test_cancelled_booking_frees_the_slot(conn):
    slot = shop.slot_range(datetime(2026, 11, 20, 14, tzinfo=shop.TZ))
    booking.create_booking(conn, 2, slot, ANA)
    conn.execute("UPDATE bookings SET status = 'cancelled'")
    assert booking.create_booking(conn, 2, slot, BEN)["ok"]


# --- book ----------------------------------------------------------------------------------


def test_book_writes_the_booking_and_its_confirmation_together(conn):
    out = book(conn, ANA, "tomorrow at 10am", email="ana@example.com")
    assert out == {"ok": True, "message": "Booked Tuesday, November 17 at 10:00 AM with Dana. "
                                          "Booking number 1. A confirmation email is on its way."}
    assert confirmed(conn) == [(1, 1, ANA)]
    row = conn.execute("SELECT booking_id, kind, email, body FROM outbox").fetchone()
    assert row[:3] == (1, "confirmed", "ana@example.com")
    assert "Tuesday, November 17 at 10:00 AM" in row[3]


def test_asking_twice_returns_the_same_booking_and_one_email(conn):
    book(conn, ANA, "Friday at 2pm")
    out = book(conn, ANA, "Friday at 2pm")
    assert out["ok"] and "Already booked" in out["message"] and "booking number 1" in out["message"]
    assert len(confirmed(conn)) == 1
    assert outbox_kinds(conn) == ["confirmed"]


def test_any_mechanic_fills_dana_then_luis_then_priya_then_offers_alternatives(conn):
    for caller in (ANA, BEN, CAL):
        assert book(conn, caller, "Thursday at 10am")["ok"]
    assert [r[1] for r in confirmed(conn)] == [1, 2, 3]
    out = book(conn, "+15550100004", "Thursday at 10am")
    assert not out["ok"]
    assert "already booked" in out["message"]
    assert "Thursday, November 19 at 9:00 AM" in out["message"]  # nearest, same day first
    assert "Thursday, November 19 at 11:00 AM" in out["message"]


def test_a_named_mechanic_who_is_taken_gets_their_other_times(conn):
    book(conn, ANA, "Friday at 2pm", mechanic="Luis")
    out = book(conn, BEN, "Friday at 2pm", mechanic="luis")
    assert not out["ok"]
    assert "Friday, November 20 at 1:00 PM with Luis" in out["message"]
    assert "Friday, November 20 at 3:00 PM with Luis" in out["message"]


def test_unknown_mechanic_is_a_question(conn):
    out = book(conn, ANA, "Friday at 2pm", mechanic="Bob")
    assert out == {"ok": False, "message": "There's no mechanic called Bob. "
                                           "Our mechanics are Dana, Luis and Priya."}


def test_booking_needs_a_time(conn):
    out = book(conn, ANA, "Saturday")
    assert not out["ok"]
    assert out["message"] == ("Ask the caller what time on Saturday, November 21. "
                              "Open times: 9:00 AM, 10:00 AM, 11:00 AM, 12:00 PM.")


def test_closed_times_are_refused_with_alternatives(conn):
    out = book(conn, ANA, "Sunday at 10am")
    assert not out["ok"] and "closed on Sundays" in out["message"]
    assert "Saturday, November 21 at 12:00 PM" in out["message"]
    assert "Monday, November 23 at 8:00 AM" in out["message"]
    assert confirmed(conn) == []


def test_bad_email_still_books_but_says_no_email(conn):
    out = book(conn, ANA, "Friday at 2pm", email="ana at example dot com")
    assert out["ok"] and "didn't look right" in out["message"]
    assert conn.execute("SELECT email FROM outbox").fetchone() == (None,)


def test_dst_booking_is_stored_in_utc_and_read_back_in_shop_time(conn):
    saturday = datetime(2026, 3, 7, 9, tzinfo=shop.TZ)
    out = booking.book(conn, ANA, "Monday at 9am", now=saturday)
    assert "Monday, March 9 at 9:00 AM" in out["message"]
    start = conn.execute("SELECT lower(during) FROM bookings").fetchone()[0]
    assert start == datetime.fromisoformat("2026-03-09T13:00:00+00:00")


# --- check_availability ----------------------------------------------------------------------


def test_availability_for_a_day_skips_full_and_past_slots(conn):
    for caller in (ANA, BEN, CAL):
        book(conn, caller, "today at 10am")
    out = booking.check_availability(conn, "today", now=NOW)
    assert out["message"] == ("Open times on Monday, November 16: 9:00 AM, 11:00 AM, 12:00 PM, "
                              "1:00 PM, 2:00 PM, 3:00 PM, 4:00 PM.")


def test_availability_for_an_exact_time(conn):
    book(conn, ANA, "Friday at 2pm", mechanic="Dana")
    out = booking.check_availability(conn, "Friday at 2pm", now=NOW)
    assert out["message"] == "Friday, November 20 at 2:00 PM is open with Luis and Priya."
    taken = booking.check_availability(conn, "Friday at 2pm", "Dana", now=NOW)
    assert taken["ok"] and "already booked" in taken["message"]


def test_availability_on_a_closed_day_offers_the_nearest_open_times(conn):
    out = booking.check_availability(conn, "Sunday", now=NOW)
    assert out["ok"] and out["message"].startswith("Nothing is open on Sunday, November 22.")


# --- reschedule ------------------------------------------------------------------------------


def test_reschedule_moves_the_booking_and_keeps_the_mechanic_and_email(conn):
    book(conn, ANA, "Wednesday at 9am", mechanic="Priya", email="ana@example.com")
    out = booking.reschedule(conn, ANA, "Thursday at 1pm", now=NOW)
    assert out == {"ok": True, "message": "Moved booking number 1 from Wednesday, November 18 at "
                   "9:00 AM to Thursday, November 19 at 1:00 PM with Priya. The new booking "
                   "number is 2."}
    assert confirmed(conn) == [(2, 3, ANA)]
    assert conn.execute("SELECT status FROM bookings WHERE id = 1").fetchone() == ("rescheduled",)
    assert conn.execute("SELECT kind, email FROM outbox ORDER BY id").fetchall() == [
        ("confirmed", "ana@example.com"), ("rescheduled", "ana@example.com")]


def test_reschedule_into_a_taken_slot_keeps_the_original(conn):
    book(conn, BEN, "Thursday at 1pm", mechanic="Luis")
    book(conn, ANA, "Wednesday at 9am", mechanic="Luis")
    out = booking.reschedule(conn, ANA, "Thursday at 1pm", mechanic="Luis", now=NOW)
    assert not out["ok"]
    assert "booking number 2 stays at Wednesday, November 18 at 9:00 AM" in out["message"]
    assert confirmed(conn) == [(1, 2, BEN), (2, 2, ANA)]  # rolled back: nothing lost
    assert outbox_kinds(conn) == ["confirmed", "confirmed"]


def test_reschedule_to_another_mechanic_at_the_same_time(conn):
    book(conn, ANA, "Friday at 2pm", mechanic="Dana")
    out = booking.reschedule(conn, ANA, "Friday at 2pm", mechanic="Priya", now=NOW)
    assert out["ok"] and "with Priya" in out["message"]
    assert confirmed(conn) == [(2, 3, ANA)]


def test_repeating_a_reschedule_is_harmless(conn):
    book(conn, ANA, "Wednesday at 9am")
    booking.reschedule(conn, ANA, "Thursday at 1pm", now=NOW)
    again = booking.reschedule(conn, ANA, "Thursday at 1pm", booking_id=1, now=NOW)
    assert again["ok"] and "Already booked" in again["message"]
    assert len(confirmed(conn)) == 1


def test_reschedule_asks_which_booking_when_there_are_several(conn):
    book(conn, ANA, "Tuesday at 9am")
    book(conn, ANA, "Thursday at 9am")
    out = booking.reschedule(conn, ANA, "Friday at 9am", now=NOW)
    assert not out["ok"] and "several bookings" in out["message"]
    assert "number 1 on Tuesday" in out["message"] and "number 2 on Thursday" in out["message"]


def test_reschedule_with_no_booking(conn):
    out = booking.reschedule(conn, ANA, "Friday at 9am", now=NOW)
    assert out == {"ok": False, "message": "There's no upcoming booking for this phone number."}


# --- cancel and callback -------------------------------------------------------------------


def test_cancel_the_only_booking_then_cancel_again(conn):
    book(conn, ANA, "Friday at 2pm", email="ana@example.com")
    first = booking.cancel(conn, ANA, now=NOW)
    again = booking.cancel(conn, ANA, now=NOW)
    assert first["message"] == "Cancelled booking number 1 on Friday, November 20 at 2:00 PM."
    assert again["ok"] and "already cancelled" in again["message"]
    assert confirmed(conn) == []
    assert conn.execute("SELECT kind, email FROM outbox ORDER BY id").fetchall() == [
        ("confirmed", "ana@example.com"), ("cancelled", "ana@example.com")]


def test_cancel_cannot_touch_someone_elses_booking(conn):
    book(conn, BEN, "Friday at 2pm")
    out = booking.cancel(conn, ANA, booking_id=1, now=NOW)
    assert out == {"ok": False, "message": "There's no booking number 1 for this phone number."}
    assert len(confirmed(conn)) == 1


def test_cancel_a_rescheduled_booking_points_to_the_new_one(conn):
    book(conn, ANA, "Wednesday at 9am")
    booking.reschedule(conn, ANA, "Thursday at 1pm", now=NOW)
    out = booking.cancel(conn, ANA, booking_id=1, now=NOW)
    assert not out["ok"] and "moved to another time" in out["message"]


def test_request_callback_lands_in_the_outbox(conn):
    out = booking.request_callback(conn, ANA, "Wants a quote for a new clutch")
    assert out["ok"]
    assert conn.execute("SELECT kind, body FROM outbox").fetchone() == (
        "callback", f"Call back {ANA}: Wants a quote for a new clutch")
