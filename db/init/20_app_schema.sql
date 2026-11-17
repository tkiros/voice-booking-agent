-- Runs once, on a fresh volume, after the image's own 10_bootstrap_paradedb.sh.
-- Schema changes later: add a new numbered file here and `make reset` (dev data only).

-- btree_gist lets one GiST index mix "=" on resource_id with "&&" (overlaps) on the time range.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- The double-booking guard: Postgres itself refuses two confirmed bookings for the same
-- resource whose time ranges overlap, however many requests arrive at once.
CREATE TABLE IF NOT EXISTS bookings (
    id          bigserial PRIMARY KEY,
    resource_id int       NOT NULL,
    during      tstzrange NOT NULL,
    caller      text      NOT NULL,      -- caller's phone number from the platform payload
    status      text      NOT NULL DEFAULT 'confirmed',  -- confirmed | cancelled | rescheduled
    EXCLUDE USING gist (resource_id WITH =, during WITH &&) WHERE (status = 'confirmed')
);

-- Messages a human or the mailer must act on: booking confirmations and callback requests.
-- Written in the same transaction as the booking change, so a confirmed booking always has one.
CREATE TABLE IF NOT EXISTS outbox (
    id          bigserial   PRIMARY KEY,
    booking_id  bigint      REFERENCES bookings (id),  -- NULL for callback requests
    kind        text        NOT NULL,   -- confirmed | rescheduled | cancelled | callback
    caller      text        NOT NULL,
    email       text,                   -- NULL = nothing to email; the row is still the record
    body        text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz,
    last_error  text                    -- set when sending failed; cleared by hand to retry
);

-- One row per tool call handled by the webhook: where p50/p95 tool latency comes from.
CREATE TABLE IF NOT EXISTS tool_calls (
    id          bigserial        PRIMARY KEY,
    tool        text             NOT NULL,
    call_id     text,                       -- the platform's call id
    ok          boolean          NOT NULL,
    latency_ms  double precision NOT NULL,  -- time inside the tool, excluding HTTP and connect
    created_at  timestamptz      NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_calls_call_id ON tool_calls (call_id);

-- Baseline for the concurrency eval: the same table WITHOUT the exclusion constraint,
-- written by a naive check-then-insert (evals/concurrency.py). Never used by the app.
CREATE SCHEMA IF NOT EXISTS baseline;
CREATE TABLE IF NOT EXISTS baseline.bookings (
    id          bigserial PRIMARY KEY,
    resource_id int       NOT NULL,
    during      tstzrange NOT NULL,
    caller      text      NOT NULL,
    status      text      NOT NULL DEFAULT 'confirmed'
);
