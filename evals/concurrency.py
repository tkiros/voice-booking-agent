"""Concurrency eval: what each version of create_booking does under a real race, N runs.

    python -m evals.concurrency --runs 5     (needs `make up`; writes evals/concurrency.md)

Each run fires two races, every request on its own connection, released together by a barrier:
  - 20 different callers book the same mechanic and slot  -> how many bookings exist after?
  - 1 caller's request delivered 5 times (platform retries) -> how many rows exist after?
Correct is 1 booking, 1 row, and every request answered (no exceptions). Three versions:
  - baseline: check-then-insert, no constraint (baseline.bookings)
  - guide: the build guide's create_booking, verbatim (exclusion constraint)
  - app: app.booking.create_booking (INSERT ... ON CONFLICT DO NOTHING, same constraint)
WARNING: wipes the bookings, outbox and baseline.bookings tables of the database it runs on.
"""

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import errors

from app.booking import create_booking
from common import db

DURING = "[2026-11-20 14:00:00+00,2026-11-20 15:00:00+00)"  # any fixed slot will do
RESOURCE = 2
HERE = Path(__file__).parent


def baseline_create_booking(conn, resource_id, during, caller):
    """The naive version: look for a clash, then insert. No constraint backs it up, so two
    requests that both look before either inserts will both insert."""
    with conn.transaction():
        clash = conn.execute(
            "SELECT id FROM baseline.bookings WHERE resource_id = %s"
            " AND during && %s::tstzrange AND status = 'confirmed'",
            (resource_id, during)).fetchone()
        if clash:
            return {"ok": False, "reason": "slot_taken"}
        booking_id = conn.execute(
            "INSERT INTO baseline.bookings (resource_id, during, caller)"
            " VALUES (%s, %s::tstzrange, %s) RETURNING id",
            (resource_id, during, caller)).fetchone()[0]
    return {"ok": True, "booking_id": booking_id}


def guide_create_booking(conn, resource_id, during, caller):
    """The build guide's version, verbatim: no deadlock retry."""
    find = ("SELECT id FROM bookings WHERE resource_id=%s AND during=%s::tstzrange "
            "AND caller=%s AND status='confirmed'")
    with conn.transaction():
        if row := conn.execute(find, (resource_id, during, caller)).fetchone():
            return {"ok": True, "booking_id": row[0], "duplicate": True}
    try:
        with conn.transaction():
            bid = conn.execute(
                "INSERT INTO bookings (resource_id, during, caller) VALUES (%s, %s::tstzrange, %s) RETURNING id",  # noqa: E501
                (resource_id, during, caller)).fetchone()[0]
        return {"ok": True, "booking_id": bid, "duplicate": False}
    except errors.ExclusionViolation:
        with conn.transaction():  # lost a race -- maybe to our own retry
            if row := conn.execute(find, (resource_id, during, caller)).fetchone():
                return {"ok": True, "booking_id": row[0], "duplicate": True}
        return {"ok": False, "reason": "slot_taken"}


def answered(fn):
    """Wrap fn so an exception becomes a result: the caller heard an error, not an answer."""
    def wrapper(*args):
        try:
            return fn(*args)
        except psycopg.Error as e:
            return {"ok": False, "reason": type(e).__name__}
    return wrapper


def race(fn, arg_tuples: list[tuple]) -> list:
    """Call fn(conn, *args) once per tuple, all at the same moment, each on its own connection.

    Connections are opened BEFORE the barrier, so the race is between the SQL statements and
    not between connection handshakes.
    """
    barrier = threading.Barrier(len(arg_tuples))

    def one(args):
        with db.connect() as conn:
            barrier.wait(timeout=30)
            return fn(conn, *args)

    with ThreadPoolExecutor(max_workers=len(arg_tuples)) as pool:
        return list(pool.map(one, arg_tuples))


def wipe() -> None:
    with db.connect() as conn:
        conn.execute("TRUNCATE bookings, outbox, baseline.bookings RESTART IDENTITY")


def count_rows(table: str) -> int:
    """Confirmed bookings in `table` (a fixed name from SYSTEMS, never user input)."""
    with db.connect() as conn:
        sql = f"SELECT count(*) FROM {table} WHERE status = 'confirmed'"
        return conn.execute(sql).fetchone()[0]


def one_run(fn, table: str) -> dict:
    """Both races for one version. Returns rows left behind and requests that raised."""
    fn = answered(fn)
    wipe()
    results = race(fn, [(RESOURCE, DURING, f"+1555010{n:04d}") for n in range(20)])
    rows_20 = count_rows(table)
    errors_20 = sum(r["reason"] != "slot_taken" for r in results if not r["ok"])
    wipe()
    results = race(fn, [(RESOURCE, DURING, "+15550109999")] * 5)
    rows_5 = count_rows(table)
    errors_5 = sum(not r["ok"] for r in results)  # raised, or wrongly told "slot taken"
    return {"rows_20": rows_20, "errors_20": errors_20, "rows_5": rows_5, "errors_5": errors_5}


SYSTEMS = {
    "baseline: check-then-insert, no constraint": (baseline_create_booking, "baseline.bookings"),
    "guide: exclusion constraint, verbatim": (guide_create_booking, "bookings"),
    "app: exclusion constraint + ON CONFLICT DO NOTHING": (create_booking, "bookings"),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args(argv)

    rows, totals = [], []
    for name, (fn, table) in SYSTEMS.items():
        runs = [one_run(fn, table) for _ in range(args.runs)]
        for i, r in enumerate(runs, 1):
            rows.append(f"| {name} | {i} | {r['rows_20']} | {r['errors_20']} "
                        f"| {r['rows_5']} | {r['errors_5']} |")
        totals.append(
            f"| {name} | {args.runs} | {sum(r['rows_20'] - 1 for r in runs)} "
            f"| {sum(r['rows_5'] - 1 for r in runs)} "
            f"| {sum(r['errors_20'] for r in runs)} of {20 * args.runs} "
            f"| {sum(r['errors_5'] for r in runs)} of {5 * args.runs} |")
    wipe()

    lines = [
        "# Concurrency eval",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by "
        f"`python -m evals.concurrency --runs {args.runs}`. Do not edit by hand.",
        "",
        "Each run: 20 different callers book the same mechanic and slot at once (correct: 1 "
        "booking, 19 told \"slot taken\"), then one caller's request is delivered 5 times at once "
        "(correct: 1 row, all 5 told \"booked\"). A request that raised an exception got no "
        "answer at all; on a call, the agent would have nothing to say.",
        "",
        "| System | Runs | Double-bookings | Extra rows from retries "
        "| 20-caller requests that raised | Retries not told \"booked\" |",
        "|---|---|---|---|---|---|",
        *totals,
        "",
        "Per run:",
        "",
        "| System | Run | Bookings after 20 callers | Raised | Rows after 5 retries "
        "| Not told \"booked\" |",
        "|---|---|---|---|---|---|",
        *rows,
    ]
    out = HERE / "concurrency.md"
    out.write_text("\n".join(lines) + "\n")
    print(out.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
