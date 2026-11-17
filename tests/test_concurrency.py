"""Simultaneous requests: the guide's two concurrency tests, plus the same races through the tools.

Every request runs on its own connection and all are released together by a barrier
(evals/concurrency.py:race). Needs `make up`; wipes the booking tables.
"""

from datetime import datetime

from app import booking, shop
from evals.concurrency import race
from tests.conftest import NOW, confirmed, outbox_kinds

SLOT = shop.slot_range(datetime(2026, 11, 20, 14, tzinfo=shop.TZ))
CALLERS = [f"+1555010{n:04d}" for n in range(20)]


def test_20_callers_same_slot_exactly_one_wins(conn):
    results = race(booking.create_booking, [(2, SLOT, c) for c in CALLERS])
    winners = [r for r in results if r["ok"]]
    assert len(winners) == 1
    assert [r for r in results if not r["ok"]] == [{"ok": False, "reason": "slot_taken"}] * 19
    assert len(confirmed(conn)) == 1


def test_5_duplicate_deliveries_one_row_all_succeed(conn):
    results = race(booking.create_booking, [(2, SLOT, CALLERS[0])] * 5)
    assert all(r["ok"] for r in results)
    assert len({r["booking_id"] for r in results}) == 1
    assert sum(not r["duplicate"] for r in results) == 1
    assert len(confirmed(conn)) == 1


def book_friday_2pm(conn, caller):
    return booking.book(conn, caller, "Friday at 2pm", now=NOW)


def test_20_callers_any_mechanic_fill_all_three_and_no_more(conn):
    results = race(book_friday_2pm, [(c,) for c in CALLERS])
    assert sum(r["ok"] for r in results) == 3
    assert sorted(r[1] for r in confirmed(conn)) == [1, 2, 3]  # one booking per mechanic
    assert outbox_kinds(conn) == ["confirmed"] * 3
    losers = [r for r in results if not r["ok"]]
    assert all("Nearest open times" in r["message"] for r in losers)


def test_5_duplicate_tool_calls_one_booking_one_email(conn):
    results = race(book_friday_2pm, [(CALLERS[0],)] * 5)
    assert all(r["ok"] for r in results)
    assert all("ooking number 1" in r["message"] for r in results)
    assert len(confirmed(conn)) == 1
    assert outbox_kinds(conn) == ["confirmed"]


def test_5_duplicate_reschedules_move_once(conn):
    booking.book(conn, CALLERS[0], "Wednesday at 9am", now=NOW)
    results = race(lambda c: booking.reschedule(c, CALLERS[0], "Thursday at 1pm", now=NOW),
                   [()] * 5)
    assert all(r["ok"] for r in results), results
    assert len(confirmed(conn)) == 1
    assert outbox_kinds(conn) == ["confirmed", "rescheduled"]


def test_5_duplicate_cancels_cancel_once(conn):
    booking.book(conn, CALLERS[0], "Wednesday at 9am", now=NOW)
    results = race(lambda c: booking.cancel(c, CALLERS[0], now=NOW), [()] * 5)
    assert all(r["ok"] for r in results), results
    assert confirmed(conn) == []
    assert outbox_kinds(conn) == ["confirmed", "cancelled"]
