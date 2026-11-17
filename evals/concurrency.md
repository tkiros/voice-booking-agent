# Concurrency eval

Generated 2026-11-17 16:23 UTC by `python -m evals.concurrency --runs 10`. Do not edit by hand.

Each run: 20 different callers book the same mechanic and slot at once (correct: 1 booking, 19 told "slot taken"), then one caller's request is delivered 5 times at once (correct: 1 row, all 5 told "booked"). A request that raised an exception got no answer at all; on a call, the agent would have nothing to say.

| System | Runs | Double-bookings | Extra rows from retries | 20-caller requests that raised | Retries not told "booked" |
|---|---|---|---|---|---|
| baseline: check-then-insert, no constraint | 10 | 164 | 40 | 0 of 200 | 0 of 50 |
| guide: exclusion constraint, verbatim | 10 | 0 | 0 | 0 of 200 | 8 of 50 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 10 | 0 | 0 | 0 of 200 | 0 of 50 |

Per run:

| System | Run | Bookings after 20 callers | Raised | Rows after 5 retries | Not told "booked" |
|---|---|---|---|---|---|
| baseline: check-then-insert, no constraint | 1 | 19 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 2 | 15 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 3 | 17 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 4 | 18 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 5 | 19 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 6 | 18 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 7 | 18 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 8 | 20 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 9 | 13 | 0 | 5 | 0 |
| baseline: check-then-insert, no constraint | 10 | 17 | 0 | 5 | 0 |
| guide: exclusion constraint, verbatim | 1 | 1 | 0 | 1 | 0 |
| guide: exclusion constraint, verbatim | 2 | 1 | 0 | 1 | 0 |
| guide: exclusion constraint, verbatim | 3 | 1 | 0 | 1 | 0 |
| guide: exclusion constraint, verbatim | 4 | 1 | 0 | 1 | 0 |
| guide: exclusion constraint, verbatim | 5 | 1 | 0 | 1 | 4 |
| guide: exclusion constraint, verbatim | 6 | 1 | 0 | 1 | 0 |
| guide: exclusion constraint, verbatim | 7 | 1 | 0 | 1 | 4 |
| guide: exclusion constraint, verbatim | 8 | 1 | 0 | 1 | 0 |
| guide: exclusion constraint, verbatim | 9 | 1 | 0 | 1 | 0 |
| guide: exclusion constraint, verbatim | 10 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 1 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 2 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 3 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 4 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 5 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 6 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 7 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 8 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 9 | 1 | 0 | 1 | 0 |
| app: exclusion constraint + ON CONFLICT DO NOTHING | 10 | 1 | 0 | 1 | 0 |
