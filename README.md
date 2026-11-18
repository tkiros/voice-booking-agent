# Voice Booking Agent: the backend behind a Vapi phone agent

A small business that lets a voice AI take bookings by phone needs the system behind it never to double-book when two callers want the same slot. It also has to survive the voice platform re-sending a request, and resolve "next Tuesday at 2" in the shop's own time zone. This repo is that backend for a fictional car repair shop, built for implementers who wire voice agents into real scheduling.

**Status (2026-11-17):** the backend, its tests and its evals all run. **No real voice call has been made**, because there is no Vapi account here. The Vapi wiring follows Vapi's docs and is unverified. See [Limitations](#limitations).

## How it works

```mermaid
flowchart LR
    caller(["Caller on the phone"]) -->|speech| vapi["Vapi<br/>speech-to-text, LLM, text-to-speech"]
    vapi -->|"POST tool-calls + Bearer secret"| tunnel["cloudflared / ngrok<br/>public HTTPS"]
    tunnel --> api["FastAPI<br/>POST /vapi/tool-calls"]
    api -->|"wrong or missing secret"| reject["401, nothing runs"]
    api --> adapter["app/vapi.py<br/>Vapi format in and out<br/>logs per-tool latency"]
    adapter --> core["app/booking.py<br/>check, book, reschedule,<br/>cancel, request callback"]
    core --> dates["app/dates.py<br/>caller's words to a date<br/>in America/New_York"]
    core --> pg[("Postgres<br/>bookings + exclusion constraint<br/>outbox, tool_calls")]
    adapter -->|"result or error, always HTTP 200"| vapi
    pg -.->|"after the reply"| mailer["outbox sender"]
    mailer -.->|"optional SMTP"| email(["Confirmation email"])
    pg -.->|"callback requests"| staff(["Staff member calls back"])
```

- **Vapi runs the conversation** with its own LLM, speech-to-text and text-to-speech. When the caller wants something done, Vapi POSTs a `tool-calls` webhook here and reads the reply to the caller.
- **Five tools:**
  - `check_availability`, which offers the nearest alternatives when a slot is taken or the shop is closed;
  - `create_booking`, `reschedule` and `cancel`;
  - `request_callback`, the human hand-off.
- **Postgres makes double-booking impossible.** An exclusion constraint refuses overlapping confirmed bookings for the same mechanic, however many requests arrive at once. The Python checks only decide what to *say*.
- **Dates are resolved on the server.** The model passes the caller's own words ("next Tuesday at 2pm") and never does date arithmetic. Anything ambiguous comes back as a question to ask the caller.
- **The core is platform-agnostic.** `app/booking.py` takes plain arguments and returns `{"ok", "message"}`. Only `app/vapi.py` knows Vapi's format, so a Retell adapter would be one more file ([note](#porting-to-retell)).
- **The fictional shop,** "Maple Street Auto Repair", has three mechanics (Dana, Luis, Priya), one-hour slots on the hour, and opens Mon–Fri 8–5 and Sat 9–1, closed Sunday.

## Quick start

You need Docker and [uv](https://docs.astral.sh/uv/). The app runs on the host with uv; only Postgres runs in Docker.

```bash
make up        # Postgres 17 on localhost:5437 (also creates .env from .env.example)
make test      # 98 tests: dates and DST, booking rules, concurrency races, webhook contract
make demo      # starts the app on :8004, replays the 30 scripted calls as transcripts with PASS/FAIL
make eval      # writes evals/results.md (scenarios + latency) and evals/concurrency.md
make run       # the API on http://localhost:8004, to send your own requests
```

`make test`, `make demo` and `make eval` **wipe the booking tables** of the local dev database. `make help` lists every target, and `make down` stops Postgres (data kept).

You can also send a tool call by hand while `make run` is up. The secret is the placeholder from `.env.example`:

```bash
curl -s localhost:8004/vapi/tool-calls \
  -H "Authorization: Bearer change-me-before-exposing-a-tunnel" -H "Content-Type: application/json" \
  -d '{"message": {"type": "tool-calls", "call": {"id": "try-1", "customer": {"number": "+15550100001"}},
       "toolCallList": [{"id": "t1", "type": "function",
         "function": {"name": "check_availability", "arguments": {"when": "tomorrow at 10am"}}}]}}'
```

To connect a real Vapi assistant (tunnel, server URL, Bearer credential), follow [vapi/README.md](vapi/README.md).

## Results

All numbers below come from `make eval` on 2026-11-17, at 16:22–16:23 UTC, on one dev machine with Postgres 17 (`paradedb/paradedb:0.26.7-pg17`) in Docker. The raw files are [evals/results.md](evals/results.md) and [evals/concurrency.md](evals/concurrency.md).

### 1. Call scenarios: backend contract testing, not conversation quality

`python -m evals.run --start-server` → **30/30 scenarios passed.** They were replayed as 56 webhook POSTs.

- **What a scenario is.**
  - Each of the 30 scripted calls in [evals/scenarios.py](evals/scenarios.py) is a sequence of Vapi-shaped `tool-calls` webhook POSTs, sent to the running app over HTTP.
  - The shop clock is frozen at Monday 2026-11-16 08:30 New York time, so "tomorrow" is always Tuesday.
  - Each step names a scripted caller line, the tool call Vapi would make, and its pass criteria: HTTP status, `result` or `error`, and phrases the reply must contain.
  - Each scenario also checks the database afterwards: confirmed bookings and outbox rows.
- **How the labels were made.** I wrote them by hand from the shop rules. They are not generated, and not checked by anyone else.
- **What it does *not* test.** No LLM or voice is involved. The caller lines are context for the reader, and nothing turns them into tool calls. So this proves the backend answers each tool call correctly, not that an agent would make the right calls.
- **The harness can fail.** Against a server whose clock was one day off, 4 of 30 failed, each naming the missing phrase. `tests/test_evals.py` also checks every kind of wrong reply.

| Category | Scenarios | Passed |
|---|---|---|
| book | 2 | 2 |
| availability | 1 | 1 |
| slot taken → alternatives | 2 | 2 |
| ambiguous date (asks instead of guessing) | 5 | 5 |
| relative / spoken date | 3 | 3 |
| outside business hours | 5 | 5 |
| reschedule | 2 | 2 |
| cancel | 2 | 2 |
| caller changes their mind | 3 | 3 |
| platform retry (same request again, or 3 at once) | 3 | 3 |
| bad auth (401) | 1 | 1 |
| asks for a person | 1 | 1 |

Here is what `make demo` prints for one scenario (real output):

```
PASS  06  'Next Tuesday' said on a Monday is clarified, not guessed  [ambiguous date]
    caller  "Next Tuesday at 9, please."
    tool    create_booking {"when": "next Tuesday at 9am"}
    reply   error: 'next Tuesday' could mean Tuesday, November 17 or Tuesday, November 24. Ask the caller which one.
    caller  "The 24th."
    tool    create_booking {"when": "Tuesday the 24th at 9am"}
    reply   result: Booked Tuesday, November 24 at 9:00 AM with Dana. Booking number 1.
    db      1 confirmed, outbox ['confirmed']
```

### 2. Double-bookings under concurrency

`python -m evals.concurrency --runs 10` runs two races per run, with every request on its own connection and all released together by a barrier:
- **20 different callers** book the same mechanic and slot. Correct: 1 booking.
- **One caller's request is delivered 5 times** at once, as a platform retry would. Correct: 1 row, and all 5 told "booked".

| Version | Runs | Double-bookings | Extra rows from retries | 20-caller requests that raised | Retries not told "booked" |
|---|---|---|---|---|---|
| Baseline: check-then-insert, no constraint | 10 | **164** | **40** | 0 of 200 | 0 of 50 |
| The build guide's `create_booking`, verbatim | 10 | 0 | 0 | 0 of 200 | **8 of 50** |
| This app: exclusion constraint + `ON CONFLICT DO NOTHING` | 10 | **0** | **0** | **0 of 200** | **0 of 50** |

- **The baseline** is the obvious code: SELECT for a clash, then INSERT. It double-books almost every run, because all 20 requests look before any of them inserts. It isn't idempotent either: in an earlier run (16:16 UTC), 2 of 50 of a caller's own retries were told "slot taken"; in the run above, 0 of 50. It depends on timing.

- **Finding: the guide's own tested function deadlocks.**
  - That function catches `ExclusionViolation`. Under this barrier-synchronised race, some requests instead raised `DeadlockDetected`.
  - The cause: an exclusion constraint is checked *after* the row is inserted, so two in-flight inserts can each wait for the other, and Postgres aborts one of them.
  - It never double-booked. But the aborted request got no answer, so on a call the agent would have nothing to say. In the 5-retry race it broke the "all 5 succeed" requirement.
  - The count varies with timing, and a run can show it in only one of the two races. All runs were today:
    - a 20-run probe: 57 of 400 requests raised;
    - an earlier 10-run eval: 38 of 200 and 4 of 50;
    - a 10-run eval at 16:16 UTC: 19 of 200 and 8 of 50;
    - the run in the table (16:23 UTC): 0 of 200, but still 8 of 50 retries not told "booked".
- **What fixed it.**
  - I first tried retrying after the deadlock. It livelocked: the surviving insert re-waited on the retry's new row, and 5 retries were exhausted.
  - `INSERT … ON CONFLICT DO NOTHING` works. With no conflict target it also covers exclusion constraints, and Postgres's speculative insertion waits for the other insert instead of deadlocking.
  - The function got shorter: no exception handling at all.
  - Result: 0 errors in a 50-run probe and in every eval since. The guide's version stays in `evals/concurrency.py` so this comparison keeps being measured.

**The repeated tests caught a second bug: a duplicate reschedule sent two confirmations.**
- Five copies of the same reschedule, delivered at once, normally move the booking once.
- In 6 of 30 repeated runs of `tests/test_concurrency.py`, though, one copy checked "already at the new time?" *before* the first copy committed, then picked the caller's booking *after* it committed. It therefore picked the *new* booking and "moved" it onto itself.
- Bookings stayed correct (1 confirmed), but a second confirmation email was queued.
- **The fix:** a picked booking that is already at the target slot counts as a repeat. After it, 40 of 40 repeated runs passed (240 concurrent test executions).

### 3. Latency

Server-side time inside each tool, from the `tool_calls` table (`percentile_cont`), during the scenario run:

| Tool | Calls | p50 ms | p95 ms |
|---|---|---|---|
| check_availability | 6 | 4.4 | 5.4 |
| create_booking | 38 | 8.6 | 11.4 |
| reschedule | 4 | 11.5 | 12.9 |
| cancel | 5 | 7.7 | 9.5 |
| request_callback | 1 | 3.9 | 3.9 |
| **all tools** | **54** | **8.5** | **11.6** |

- **HTTP round trip seen by the harness:** p50 26.5 ms, p95 35.3 ms over 56 requests.
  - It includes opening a new database connection per request, which is most of the gap.
  - Same machine, no tunnel. 54 tool calls vs 56 requests, because the 2 rejected-auth requests never reach a tool.
- **Small sample:** these are 54 tool calls on one machine.
- **Not measured:** turn latency and cost per call. Both come from the voice platform's logs, and no call was made.

## Failure modes

| Failure | What happens | Tested by |
|---|---|---|
| Two callers want the same slot at once | The constraint lets exactly one in. The others hear "already booked" and the 3 nearest open times | `test_concurrency.py`, scenarios 4–5, concurrency eval |
| Platform re-sends a tool call (timeout retry, or several copies at once) | Idempotent on (mechanic, slot, caller): same booking number, one row, one confirmation. Repeated cancels and reschedules are harmless too | scenarios 26–28, `test_concurrency.py` |
| Wrong or missing webhook secret | 401 before anything runs. If `VAPI_SECRET` is unset, every request is refused (fails closed) | scenario 29, `test_webhook.py` |
| Ambiguous date ("next Tuesday" said on a Monday, "Monday or Tuesday", "Wednesday the 19th" when the 19th is a Thursday, no day at all) | `error` containing the question to ask; nothing booked | scenarios 6–10, `test_dates.py` |
| Outside hours, Sunday, already passed, not on the hour | `error` with the reason and the nearest open times | scenarios 14–18 |
| Reschedule into a taken slot | One transaction, rolled back: the original booking is kept | scenario 20, `test_booking.py` |
| A tool crashes (a bug) | Still HTTP 200, with an `error` telling the agent to call `request_callback`. The next tool call in the same webhook still runs, and the crash is logged | `test_webhook.py` |
| Database unreachable | HTTP 200 with an `error` for every tool call ("apologise, try again later"). Nothing is recorded on our side | `test_webhook.py` |
| Platform times out waiting on a tool | The prompt tells the agent to call `request_callback`. If the booking did go through, its confirmation is already in the outbox | prompt only: needs a live call |
| Caller wants a person | `request_callback` writes an outbox row (`kind = 'callback'`) for staff | scenario 30 |
| Crash after booking, before the email | The outbox row commits in the same transaction as the booking. The mailer is at-least-once, so a duplicate email is possible but a missing one isn't | design; `test_webhook.py` covers sending |
| Email bounces | The row is parked with `last_error` and later rows still send | `test_webhook.py` |
| No caller number on the call (e.g. a dashboard web call) | `check_availability` works; booking, reschedule and cancel are refused. A warning logs which keys the `call` object had | `test_webhook.py` |
| Malformed webhook body | 422 | `test_webhook.py` |
| Spoofed caller ID | **Not handled:** the phone number is the identity | see Limitations |

## Design decisions

These include every deviation from the build guide (`portfolio_projects_build_guide.md`, section P4).

- **Scope: this repo is the backend only.** Vapi runs the conversation and its own LLM, and the backend makes no model calls. So the template's `common/llm.py`, its tests, the `llm_calls` table, the `anthropic` dependency and the LLM eval harness were deleted rather than left as dead code. The eval became the call-scenario harness.
- **No live call testing (deviation from M2/M3).** With no Vapi account, the guide's "a real test call books a slot" and "recordings saved" are replaced by the scripted scenario harness. It is labelled backend contract testing.
- **`create_booking` is not verbatim (deviation).** The guide asks for its tested function exactly. That function deadlocks under a real race ([results](#2-double-bookings-under-concurrency)), so this one uses `ON CONFLICT DO NOTHING` and keeps the same constraint and the same idempotency rule. The bookings table *is* exactly the guide's.
- **Platform: Vapi, not Retell.** The guide says to pick one.
- **Every check reads committed data.** Postgres's default READ COMMITTED lets each statement see rows committed a moment ago. That is how a losing request finds the winner's booking.
- **Booking and confirmation commit together.** `book()` wraps the insert and the outbox row in one transaction.
- **Rescheduling never strands the caller.**
  - It releases the old booking and takes the new slot in one transaction. If the new slot is taken, `raise Rollback()` puts the old booking back.
  - Old bookings get status `rescheduled`, not `cancelled`. That way a repeated *cancel* can still truthfully answer "already cancelled".
- **"Any mechanic" tries the mechanics in a fixed order.** Two copies of one request then race for the *same* row: one inserts it and the other finds it, rather than each grabbing a different mechanic.
- **Bookings belong to the caller's phone number from the platform payload, never to a tool argument.** Otherwise a caller could cancel someone else's booking by reading out their number.
- **Date policy:**
  - A bare weekday means the next one after today.
  - "Next Friday" is asked about only when that Friday is later in the current week; if it's already in next week, it's taken as meant.
  - A bare hour from 1 to 7 means PM, since a garage isn't open at 3 AM.
  - A weekday that contradicts a date ("Wednesday the 19th") is asked about.
  - Arithmetic is done on calendar dates, never on "now + 24 hours".
  - Times are stored in UTC and spoken in shop time. DST tests cover both 2026 changes (8 March and 1 November).
- **Failures go in `error`, everything else in `result`.** It is one rule: `ok = False` → `error`. A taken slot is `ok = False`, but its message still lists the alternatives.
- **Tool definitions are generated** from the Pydantic argument models (`make vapi-config` → `vapi/*.json`). A test fails if the committed JSON drifts from the code.
- **Auth is the Bearer token** that Vapi's Bearer Token credential sends by default (`Authorization: Bearer …`). It is compared with `hmac.compare_digest` and fails closed.
- **Confirmation by email (deviation).** The guide's email confirmation became an outbox row, plus SMTP only if `SMTP_HOST` is set, sent in a background task after the webhook replies so a slow mail server never adds to the caller's wait. No SMS: US texting from a regular number needs carrier registration (A2P 10DLC).
- **One extra tool, `request_callback`.** The spec named four tools. This fifth one implements the guide's "log a callback request" for "caller wants a human" and for tool failures.
- **The harness needs a frozen clock: `FAKE_NOW`.** It is the only test seam in the app; the app logs a warning at startup when it is set. The harness also wipes the booking tables before each scenario, so every scenario starts clean.
- **No `make seed`.** Every scenario and test creates its own state, so there is nothing to seed. The quick start is `make up && make test && make demo` instead of the guide's `docker compose up && make seed && make demo`.
- **No API registration script.** Vapi's docs confirm the tool-creation body, but not the credential API (that docs page returned 404). [vapi/README.md](vapi/README.md) therefore gives dashboard steps only.
- **Environment (deviations from the template):**
  - The app runs on the host with uv and only Postgres runs in Docker, because containers on the build machine had no DNS.
  - The Dockerfile and the compose `app` service are kept, mapped to host port 8004, but their image build is unverified.
  - Ports are 5437 for Postgres and 8004 for the app.
  - `PROJECT_NAME` sets the Docker Compose project name, keeping this stack's containers and volume apart from sibling projects.
  - The ParadeDB image is the shared template's pin. This project only needs plain Postgres plus `btree_gist`.
- **Dependencies:**
  - `tzdata`, so `zoneinfo` works on machines without a system time-zone database (Windows, slim images);
  - `httpx2` (dev), which this Starlette version's `TestClient` uses.

## Porting to Retell

Only `app/vapi.py` would change. From Retell's custom-function docs:
- Retell POSTs `{name, call, args}`, or just the arguments if "args only" is set.
- You authenticate with a custom header, so `require_secret` would read that header.
- The URL must be public, because Retell blocks localhost and private IPs. Use the same tunnel.
- The timeout defaults to 120 s, and Retell doesn't retry unless `max_retry` is set. The idempotent booking already covers retries either way.

A `retell.py` would map `name` and `args` onto the same `dispatch()` and return the message in whatever response shape Retell expects (not checked here). The caller's number would come from the `call` object; I haven't checked that field name.

## What I'd do next

- **Make the real calls.**
  - Wire up a Vapi number and confirm the caller-number field path, which is the first thing to check.
  - Measure turn latency and cost per call from Vapi's logs.
  - Record the 2-minute demo the guide asks for: a call books a slot, a second call is offered alternatives, then show the rows and the email.
- **Evaluate the conversation, not just the backend.**
  - Run the same 30 scenarios with a text LLM in the loop, using the system prompt and the tool definitions from `vapi/`.
  - Grade whether it makes the right tool calls, reads the date back before booking, and never invents times.
  - Then add the off-topic scenario from the guide, which only makes sense with a model in the loop.
- **Harden it for a real shop.**
  - A `psycopg_pool` connection pool, since opening a connection is most of the ~29 ms round trip.
  - A small host instead of a tunnel.
  - A staff view of callback requests.
  - SMS confirmations once A2P 10DLC registration is done.

## Limitations

- **No real voice call has been made, and no Vapi account was used.** Everything about the live integration is taken from Vapi's docs and unverified: the dashboard steps, the Bearer header arriving as documented, and where the caller's number sits in the payload (`call.customer.number` is an assumption).
- **The 30/30 is a backend contract result, not a conversation-quality result.** No model processed the scripted caller lines.
- **Not measured:** turn latency and cost per call. The tool latency comes from one machine, with no tunnel and a sample of 54 tool calls.
- **Caller ID is the identity,** and caller ID can be spoofed. A real deployment would add a check before cancelling, such as the booking number plus a code from the confirmation.
- **Narrow model of a shop:** one shop, one time zone, fixed one-hour slots, and no service durations or mechanic skills.
- **The date parser is regex-based English.** It needs digits ("3pm", not "three pm") and covers the phrasings in `tests/test_dates.py`, not every way people say dates.
- **No demo video yet.**
- **Unverified:** the Docker app image build hasn't been tried. (The GitHub Actions workflow passes on GitHub: ruff, 98 tests and the 30 call scenarios.)
- **One database connection per request.** It's fine at this scale; a pool would be the next step.
- **`make test` and `make eval` wipe the booking tables** of whatever database `DATABASE_URL` points at. Only point them at a dev database.

## What's where

| Path | What it is |
|---|---|
| `app/shop.py` | The fictional shop: mechanics, hours, time zone, slots, how times are read out |
| `app/dates.py` | The caller's words → day and time; raises `NeedsClarification` with the question to ask |
| `app/booking.py` | The tools, platform-agnostic, and `create_booking` |
| `app/outbox.py` | Confirmations and callback requests; the optional SMTP sender |
| `app/vapi.py` | Vapi webhook format, argument validation, latency logging, tool and assistant definitions |
| `app/main.py` | FastAPI: `/health` and `POST /vapi/tool-calls` with the shared-secret check |
| `db/init/20_app_schema.sql` | `bookings` (the exclusion constraint), `outbox`, `tool_calls`, `baseline.bookings` |
| `evals/scenarios.py`, `evals/run.py` | The 30 scripted calls and the harness → `evals/results.md` |
| `evals/concurrency.py` | Baseline vs guide vs app under the same race → `evals/concurrency.md` |
| `vapi/` | Generated tool and assistant JSON, and the dashboard setup guide |
| `tests/` | 98 tests; the database ones run while `make up` is running |
