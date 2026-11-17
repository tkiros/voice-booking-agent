# Call-scenario results: backend contract test

Generated 2026-11-17 16:22 UTC by `python -m evals.run --start-server`. Do not edit by hand.

- **What ran:** 30 scripted calls, replayed as 56 Vapi-shaped `tool-calls` webhook POSTs against the running app (FastAPI + Postgres 17), shop clock frozen at 2026-11-16T08:30 America/New_York (a Monday).
- **Labels:** pass criteria hand-written per step in `evals/scenarios.py` (HTTP status, `result` vs `error`, phrases the reply must contain) plus the expected database state after each call.
- **Not measured:** no LLM and no voice are involved. This tests the backend contract, not conversation quality.

**Passed: 30/30 scenarios (100%).**

| Category | Scenarios | Passed |
|---|---|---|
| book | 2 | 2 |
| availability | 1 | 1 |
| slot taken | 2 | 2 |
| ambiguous date | 5 | 5 |
| relative date | 3 | 3 |
| outside hours | 5 | 5 |
| reschedule | 2 | 2 |
| cancel | 2 | 2 |
| change of mind | 3 | 3 |
| platform retry | 3 | 3 |
| auth | 1 | 1 |
| handoff | 1 | 1 |

| # | Scenario | Category | Result |
|---|---|---|---|
| 1 | Books tomorrow morning | book | PASS |
| 2 | Checks, hears it read back, books a named mechanic with email | book | PASS |
| 3 | Asks what's open on a whole day | availability | PASS |
| 4 | Wants a mechanic who's busy, offered his other times, takes one | slot taken | PASS |
| 5 | Slot full for every mechanic, nearest times offered | slot taken | PASS |
| 6 | 'Next Tuesday' said on a Monday is clarified, not guessed | ambiguous date | PASS |
| 7 | Weekday doesn't match the date | ambiguous date | PASS |
| 8 | Names two days at once | ambiguous date | PASS |
| 9 | Gives no day at all | ambiguous date | PASS |
| 10 | Gives a day but no time, is offered the open times | ambiguous date | PASS |
| 11 | 'Next Monday at 11' (already next week, so not ambiguous) | relative date | PASS |
| 12 | A bare 'at 3' means 3 PM at a garage | relative date | PASS |
| 13 | A spoken calendar date | relative date | PASS |
| 14 | Evening request | outside hours | PASS |
| 15 | Sunday | outside hours | PASS |
| 16 | Saturday afternoon (short Saturday hours) | outside hours | PASS |
| 17 | A time earlier today | outside hours | PASS |
| 18 | Half past the hour | outside hours | PASS |
| 19 | Moves an appointment | reschedule | PASS |
| 20 | Move into a taken slot keeps the original booking | reschedule | PASS |
| 21 | Cancels the only booking | cancel | PASS |
| 22 | Has two bookings, is asked which to cancel | cancel | PASS |
| 23 | Books, then switches to Saturday in the same call | change of mind | PASS |
| 24 | Books, then asks for a different mechanic | change of mind | PASS |
| 25 | Hears one time, picks another before confirming | change of mind | PASS |
| 26 | Platform delivers the same booking request 3 times at once | platform retry | PASS |
| 27 | Platform re-sends a booking request after a timeout | platform retry | PASS |
| 28 | Platform delivers a cancel twice at once | platform retry | PASS |
| 29 | Wrong or missing secret gets 401 and books nothing | auth | PASS |
| 30 | Asks for a person | handoff | PASS |

## Tool latency

Server-side time inside each tool (from the `tool_calls` table, `percentile_cont`). Includes requests that waited on a racing duplicate.

| Tool | Calls | p50 ms | p95 ms |
|---|---|---|---|
| cancel | 5 | 7.7 | 9.5 |
| check_availability | 6 | 4.4 | 5.4 |
| create_booking | 38 | 8.6 | 11.4 |
| request_callback | 1 | 3.9 | 3.9 |
| reschedule | 4 | 11.5 | 12.9 |
| all tools | 54 | 8.5 | 11.6 |

HTTP round trip seen by the harness, same machine, no tunnel (includes opening a database connection per request): p50 26.5 ms, p95 35.3 ms over 56 requests.
