"""The scripted calls replayed by evals/run.py. Plain data: one dict per call.

Each step is one tool call Vapi would send after the caller said the `say` line. The `say` line
is context for the reader only: no LLM is involved, so what's tested is the BACKEND's answer.
Pass criteria per step: HTTP status, `result` (ok=True) or `error` (ok=False), and phrases the
reply must contain. Per scenario: the database afterwards (confirmed bookings, outbox rows).

The shop clock is frozen at Monday 16 November 2026, 8:30 AM New York time, so "tomorrow" is
always Tuesday the 17th. Mechanics: Dana, Luis, Priya. Hours: Mon-Fri 8-5, Sat 9-1, Sun closed.
Labels were hand-written from the shop rules above, before the harness first ran.
"""

ANA, BEN, CAL, DEE = "+15550100001", "+15550100002", "+15550100003", "+15550100004"


def call(tool, say="", ok=True, says=(), caller=ANA, deliveries=1, auth="good", status=200,
         retry=False, **args):
    """One step. deliveries>1 sends the same request that many times at once (platform retry);
    retry=True reuses the previous step's tool-call id (a retry after a timeout)."""
    return {"tool": tool, "args": args, "say": say, "ok": ok, "says": list(says),
            "caller": caller, "deliveries": deliveries, "auth": auth, "status": status,
            "retry": retry}


SCENARIOS = [
    # --- book ---------------------------------------------------------------------------------
    {"name": "Books tomorrow morning", "covers": "book",
     "steps": [call("create_booking", "Can I bring my car in tomorrow at 10am for an oil change?",
                    when="tomorrow at 10am",
                    says=["Booked Tuesday, November 17 at 10:00 AM", "Booking number 1"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    {"name": "Checks, hears it read back, books a named mechanic with email", "covers": "book",
     "steps": [call("check_availability", "Is Friday at 2pm free?", when="Friday at 2pm",
                    says=["Friday, November 20 at 2:00 PM is open with Dana, Luis and Priya"]),
               call("create_booking", "Yes, with Luis please. It's ana@example.com.",
                    when="Friday at 2pm", mechanic="Luis", email="ana@example.com",
                    says=["with Luis", "confirmation email is on its way"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    # --- availability -------------------------------------------------------------------------
    {"name": "Asks what's open on a whole day", "covers": "availability",
     "steps": [call("check_availability", "What do you have on Wednesday?", when="Wednesday",
                    says=["Open times on Wednesday, November 18: 8:00 AM, 9:00 AM", "4:00 PM"])],
     "db": {"confirmed": 0, "outbox": []}},

    # --- slot taken -> alternatives -----------------------------------------------------------
    {"name": "Wants a mechanic who's busy, offered his other times, takes one",
     "covers": "slot taken",
     "steps": [call("create_booking", "(another caller) Luis, Friday at 2.", caller=BEN,
                    when="Friday at 2pm", mechanic="Luis", says=["Booked"]),
               call("create_booking", "Can Luis do Friday at 2pm?", when="Friday at 2pm",
                    mechanic="Luis", ok=False,
                    says=["already booked", "1:00 PM with Luis", "3:00 PM with Luis"]),
               call("create_booking", "Then 3pm.", when="Friday at 3pm", mechanic="Luis",
                    says=["Booked Friday, November 20 at 3:00 PM with Luis"])],
     "db": {"confirmed": 2, "outbox": ["confirmed", "confirmed"]}},

    {"name": "Slot full for every mechanic, nearest times offered", "covers": "slot taken",
     "steps": [call("create_booking", "(caller 1)", caller=BEN, when="Thursday at 10am"),
               call("create_booking", "(caller 2)", caller=CAL, when="Thursday at 10am"),
               call("create_booking", "(caller 3)", caller=DEE, when="Thursday at 10am"),
               call("check_availability", "Anything Thursday at 10?", when="Thursday at 10am",
                    says=["already booked", "Thursday, November 19 at 9:00 AM",
                          "Thursday, November 19 at 11:00 AM"]),
               call("create_booking", "Just book me in at 10 anyway.", when="Thursday at 10am",
                    ok=False, says=["already booked", "Nearest open times"])],
     "db": {"confirmed": 3, "outbox": ["confirmed"] * 3}},

    # --- ambiguous dates: ask, never guess ----------------------------------------------------
    {"name": "'Next Tuesday' said on a Monday is clarified, not guessed",
     "covers": "ambiguous date",
     "steps": [call("create_booking", "Next Tuesday at 9, please.", when="next Tuesday at 9am",
                    ok=False, says=["November 17", "November 24", "which one"]),
               call("create_booking", "The 24th.", when="Tuesday the 24th at 9am",
                    says=["Booked Tuesday, November 24 at 9:00 AM"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    {"name": "Weekday doesn't match the date", "covers": "ambiguous date",
     "steps": [call("create_booking", "Wednesday the 19th at 10.",
                    when="Wednesday the 19th at 10am", ok=False,
                    says=["November 19 is a Thursday, not a Wednesday"]),
               call("create_booking", "Oh, Thursday then.", when="Thursday the 19th at 10am",
                    says=["Booked Thursday, November 19 at 10:00 AM"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    {"name": "Names two days at once", "covers": "ambiguous date",
     "steps": [call("check_availability", "Monday or Tuesday, whichever.",
                    when="Monday or Tuesday", ok=False, says=["Monday or Tuesday"])],
     "db": {"confirmed": 0, "outbox": []}},

    {"name": "Gives no day at all", "covers": "ambiguous date",
     "steps": [call("create_booking", "Sometime next week?", when="sometime next week",
                    ok=False, says=["Ask for a day"])],
     "db": {"confirmed": 0, "outbox": []}},

    {"name": "Gives a day but no time, is offered the open times", "covers": "ambiguous date",
     "steps": [call("create_booking", "Saturday works.", when="Saturday", ok=False,
                    says=["what time", "9:00 AM, 10:00 AM, 11:00 AM, 12:00 PM"])],
     "db": {"confirmed": 0, "outbox": []}},

    # --- relative and spoken dates ------------------------------------------------------------
    {"name": "'Next Monday at 11' (already next week, so not ambiguous)",
     "covers": "relative date",
     "steps": [call("create_booking", "Next Monday at 11.", when="next Monday at 11",
                    says=["Booked Monday, November 23 at 11:00 AM"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    {"name": "A bare 'at 3' means 3 PM at a garage", "covers": "relative date",
     "steps": [call("create_booking", "Thursday at 3.", when="Thursday at 3",
                    says=["Booked Thursday, November 19 at 3:00 PM"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    {"name": "A spoken calendar date", "covers": "relative date",
     "steps": [call("create_booking", "November 30th, 8 in the morning.",
                    when="November 30th at 8am", says=["Booked Monday, November 30 at 8:00 AM"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    # --- outside business hours ---------------------------------------------------------------
    {"name": "Evening request", "covers": "outside hours",
     "steps": [call("create_booking", "Tomorrow at 7pm after work?", when="tomorrow at 7pm",
                    ok=False, says=["open 8:00 AM to 5:00 PM", "Tuesday, November 17 at 4:00 PM"])],
     "db": {"confirmed": 0, "outbox": []}},

    {"name": "Sunday", "covers": "outside hours",
     "steps": [call("create_booking", "Sunday at 10?", when="Sunday at 10am", ok=False,
                    says=["closed on Sundays", "Monday, November 23 at 8:00 AM"])],
     "db": {"confirmed": 0, "outbox": []}},

    {"name": "Saturday afternoon (short Saturday hours)", "covers": "outside hours",
     "steps": [call("create_booking", "Saturday at 2pm.", when="Saturday at 2pm", ok=False,
                    says=["open 9:00 AM to 1:00 PM", "Saturday, November 21 at 12:00 PM"])],
     "db": {"confirmed": 0, "outbox": []}},

    {"name": "A time earlier today", "covers": "outside hours",
     "steps": [call("create_booking", "Today at 8?", when="today at 8am", ok=False,
                    says=["already passed", "Monday, November 16 at 9:00 AM"])],
     "db": {"confirmed": 0, "outbox": []}},

    {"name": "Half past the hour", "covers": "outside hours",
     "steps": [call("create_booking", "Tomorrow at 10:30?", when="tomorrow at 10:30am", ok=False,
                    says=["on the hour", "10:00 AM", "11:00 AM"])],
     "db": {"confirmed": 0, "outbox": []}},

    # --- reschedule ---------------------------------------------------------------------------
    {"name": "Moves an appointment", "covers": "reschedule",
     "steps": [call("create_booking", "(earlier call) Wednesday at 9.", when="Wednesday at 9am",
                    email="ana@example.com"),
               call("reschedule", "Can I move my Wednesday one to Thursday at 1?",
                    when="Thursday at 1pm",
                    says=["Moved booking number 1", "to Thursday, November 19 at 1:00 PM",
                          "new booking number is 2"])],
     "db": {"confirmed": 1, "outbox": ["confirmed", "rescheduled"]}},

    {"name": "Move into a taken slot keeps the original booking", "covers": "reschedule",
     "steps": [call("create_booking", "(another caller)", caller=BEN, when="Thursday at 1pm",
                    mechanic="Luis"),
               call("create_booking", "(earlier call)", when="Wednesday at 9am", mechanic="Luis"),
               call("reschedule", "Move me to Thursday at 1 with Luis.", when="Thursday at 1pm",
                    mechanic="Luis", ok=False,
                    says=["already booked", "stays at Wednesday, November 18 at 9:00 AM"])],
     "db": {"confirmed": 2, "outbox": ["confirmed", "confirmed"]}},

    # --- cancel -------------------------------------------------------------------------------
    {"name": "Cancels the only booking", "covers": "cancel",
     "steps": [call("create_booking", "(earlier call)", when="Friday at 2pm"),
               call("cancel", "I need to cancel my appointment.",
                    says=["Cancelled booking number 1 on Friday, November 20 at 2:00 PM"])],
     "db": {"confirmed": 0, "outbox": ["confirmed", "cancelled"]}},

    {"name": "Has two bookings, is asked which to cancel", "covers": "cancel",
     "steps": [call("create_booking", "(earlier call)", when="Tuesday at 9am"),
               call("create_booking", "(earlier call)", when="Thursday at 9am"),
               call("cancel", "Cancel my appointment.", ok=False,
                    says=["several bookings", "number 1 on Tuesday", "number 2 on Thursday"]),
               call("cancel", "The Thursday one.", booking_id=2,
                    says=["Cancelled booking number 2"])],
     "db": {"confirmed": 1, "outbox": ["confirmed", "confirmed", "cancelled"]}},

    # --- caller changes their mind ------------------------------------------------------------
    {"name": "Books, then switches to Saturday in the same call", "covers": "change of mind",
     "steps": [call("create_booking", "Friday at 2 then.", when="Friday at 2pm"),
               call("reschedule", "Actually, Saturday at 10 would be better.",
                    when="Saturday at 10am",
                    says=["Moved booking number 1", "Saturday, November 21 at 10:00 AM"])],
     "db": {"confirmed": 1, "outbox": ["confirmed", "rescheduled"]}},

    {"name": "Books, then asks for a different mechanic", "covers": "change of mind",
     "steps": [call("create_booking", "Friday at 2.", when="Friday at 2pm", says=["with Dana"]),
               call("reschedule", "Could Priya do it instead?", when="Friday at 2pm",
                    mechanic="Priya", says=["Friday, November 20 at 2:00 PM with Priya"])],
     "db": {"confirmed": 1, "outbox": ["confirmed", "rescheduled"]}},

    {"name": "Hears one time, picks another before confirming", "covers": "change of mind",
     "steps": [call("check_availability", "Friday at 2?", when="Friday at 2pm",
                    says=["is open"]),
               call("check_availability", "Hmm, what about Thursday instead?", when="Thursday",
                    says=["Open times on Thursday, November 19"]),
               call("create_booking", "Thursday at 11 then.", when="Thursday at 11am",
                    says=["Booked Thursday, November 19 at 11:00 AM"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    # --- platform retries ---------------------------------------------------------------------
    {"name": "Platform delivers the same booking request 3 times at once",
     "covers": "platform retry",
     "steps": [call("create_booking", "Friday at 2.", when="Friday at 2pm", deliveries=3,
                    says=["ooking number 1"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    {"name": "Platform re-sends a booking request after a timeout", "covers": "platform retry",
     "steps": [call("create_booking", "Friday at 2.", when="Friday at 2pm",
                    says=["Booking number 1"]),
               call("create_booking", "(same request, re-sent)", when="Friday at 2pm",
                    retry=True, says=["Already booked", "booking number 1"])],
     "db": {"confirmed": 1, "outbox": ["confirmed"]}},

    {"name": "Platform delivers a cancel twice at once", "covers": "platform retry",
     "steps": [call("create_booking", "(earlier call)", when="Friday at 2pm"),
               call("cancel", "Cancel it, please.", deliveries=2, says=["booking number 1"])],
     "db": {"confirmed": 0, "outbox": ["confirmed", "cancelled"]}},

    # --- webhook auth -------------------------------------------------------------------------
    {"name": "Wrong or missing secret gets 401 and books nothing", "covers": "auth",
     "steps": [call("create_booking", "(forged request)", when="Friday at 2pm", auth="wrong",
                    status=401),
               call("create_booking", "(no credentials)", when="Friday at 2pm", auth="missing",
                    status=401)],
     "db": {"confirmed": 0, "outbox": []}},

    # --- human hand-off -----------------------------------------------------------------------
    {"name": "Asks for a person", "covers": "handoff",
     "steps": [call("request_callback", "Can I just talk to someone about a quote?",
                    reason="Wants a quote for a clutch replacement",
                    says=["Callback request logged"])],
     "db": {"confirmed": 0, "outbox": ["callback"]}},
]
