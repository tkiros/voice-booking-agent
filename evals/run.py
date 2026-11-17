"""Call-scenario harness: BACKEND CONTRACT TESTING, not conversation quality.

    python -m evals.run --start-server [--trace]       (make eval / make demo)
    python -m evals.run --url http://localhost:8004    (a server you started with FAKE_NOW set)

Replays every scenario in evals/scenarios.py as Vapi-shaped `tool-calls` webhook POSTs against
the real app (FastAPI + Postgres), checks each reply and the database afterwards, and writes
evals/results.md: pass rate, results per category, and p50/p95 tool latency. No LLM and no voice
are involved. WARNING: wipes the bookings and outbox tables before every scenario.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from common import db
from evals.scenarios import SCENARIOS

HERE = Path(__file__).parent
FAKE_NOW = "2026-11-16T08:30"  # Monday morning: the scenarios' expected dates assume it
WEBHOOK = "/vapi/tool-calls"


# --- talking to the app --------------------------------------------------------------------


def start_server(url: str) -> subprocess.Popen:
    """Start uvicorn with the shop clock frozen, and wait until /health answers."""
    if healthy(url):
        sys.exit(f"Something is already serving {url}. Stop it first: this harness starts its "
                 "own server with FAKE_NOW set.")
    port = url.rsplit(":", 1)[-1]
    log = open(HERE / "server.log", "w")  # the child process owns it from here
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", port],
        env={**os.environ, "FAKE_NOW": FAKE_NOW}, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(50):
        if healthy(url):
            return server
        time.sleep(0.2)
    server.terminate()
    sys.exit(f"The app didn't start; see {HERE / 'server.log'}")


def healthy(url: str) -> bool:
    try:
        return httpx2.get(f"{url}/health", timeout=1).status_code == 200
    except httpx2.HTTPError:
        return False


def headers(auth: str, secret: str) -> dict:
    if auth == "missing":
        return {}
    return {"Authorization": f"Bearer {secret if auth == 'good' else 'wrong-secret'}"}


def webhook_body(step: dict, call_id: str, tool_call_id: str) -> dict:
    """The JSON Vapi would POST for this step (docs.vapi.ai/tools/custom-tools)."""
    return {"message": {
        "type": "tool-calls",
        "call": {"id": call_id, "customer": {"number": step["caller"]}},
        "toolCallList": [{"id": tool_call_id, "type": "function",
                          "function": {"name": step["tool"], "arguments": step["args"]}}],
    }}


def send(client: httpx2.Client, step: dict, body: dict, secret: str) -> list[tuple]:
    """POST the step `deliveries` times at once. Returns (status, json, milliseconds) each."""
    def one(_):
        started = time.perf_counter()
        r = client.post(WEBHOOK, json=body, headers=headers(step["auth"], secret))
        ms = (time.perf_counter() - started) * 1000
        return r.status_code, (r.json() if r.status_code == 200 else None), ms

    with ThreadPoolExecutor(max_workers=step["deliveries"]) as pool:
        return list(pool.map(one, range(step["deliveries"])))


# --- grading -------------------------------------------------------------------------------


def check_reply(step: dict, tool_call_id: str, status: int, body: dict | None) -> list[str]:
    """Everything wrong with one reply ([] = pass)."""
    if status != step["status"]:
        return [f"HTTP {status}, expected {step['status']}"]
    if status != 200:
        return []
    results = (body or {}).get("results")
    if not isinstance(results, list) or len(results) != 1:
        return ["expected exactly one entry in results"]
    entry = results[0]
    problems = []
    if entry.get("toolCallId") != tool_call_id:
        problems.append("toolCallId doesn't match the request")
    key = "result" if step["ok"] else "error"
    text = entry.get(key)
    if not isinstance(text, str):
        return problems + [f"expected '{key}', got keys {sorted(entry)}"]
    if "\n" in text:
        problems.append("reply is not a single line")
    problems += [f"missing '{s}'" for s in step["says"] if s.lower() not in text.lower()]
    return problems


def db_state() -> dict:
    with db.connect() as conn:
        confirmed = conn.execute(
            "SELECT count(*) FROM bookings WHERE status = 'confirmed'").fetchone()[0]
        kinds = [r[0] for r in conn.execute("SELECT kind FROM outbox ORDER BY id").fetchall()]
    return {"confirmed": confirmed, "outbox": kinds}


def wipe() -> None:
    with db.connect() as conn:
        conn.execute("TRUNCATE bookings, outbox RESTART IDENTITY")


# --- running ---------------------------------------------------------------------------------


def run_scenario(client, secret: str, number: int, scenario: dict, run_id: str) -> dict:
    """Replay one scenario. Returns pass/fail, the trace lines and the HTTP timings."""
    wipe()
    call_id = f"{run_id}-{number:02d}"
    trace, problems, timings = [], [], []
    tool_call_id = ""
    for i, step in enumerate(scenario["steps"], 1):
        if not step["retry"]:
            tool_call_id = f"{call_id}-t{i}"
        body = webhook_body(step, call_id, tool_call_id)
        if step["say"]:
            trace.append(f'    caller  "{step["say"]}"')
        extras = f"  x{step['deliveries']} at once" if step["deliveries"] > 1 else ""
        extras += "  (re-sent, same id)" if step["retry"] else ""
        extras += f"  auth={step['auth']}" if step["auth"] != "good" else ""
        trace.append(f"    tool    {step['tool']} {json.dumps(step['args'])}{extras}")
        for status, reply, ms in send(client, step, body, secret):
            timings.append(ms)
            trace.append(f"    reply   {describe(status, reply)}")
            problems += [f"step {i}: {p}" for p in check_reply(step, tool_call_id, status, reply)]
    state = db_state()
    db_ok = all(state[k] == v for k, v in scenario["db"].items())
    trace.append(f"    db      {state['confirmed']} confirmed, outbox {state['outbox']}"
                 + ("" if db_ok else f"  EXPECTED {scenario['db']}"))
    if not db_ok:
        problems.append(f"database: got {state}, expected {scenario['db']}")
    return {"number": number, "name": scenario["name"], "covers": scenario["covers"],
            "passed": not problems, "problems": problems, "trace": trace, "timings": timings}


def describe(status: int, reply: dict | None) -> str:
    if status != 200 or not reply or not reply.get("results"):
        return f"HTTP {status}"
    entry = reply["results"][0]
    return f"error: {entry['error']}" if "error" in entry else f"result: {entry.get('result')}"


def tool_latency(run_id: str) -> list[tuple]:
    """(tool, calls, p50 ms, p95 ms) from the tool_calls table, for this run's calls only."""
    with db.connect() as conn:
        return conn.execute(
            "SELECT coalesce(tool, 'all tools'), count(*),"
            " percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms),"
            " percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)"
            " FROM tool_calls WHERE call_id LIKE %s"
            " GROUP BY ROLLUP (tool) ORDER BY tool NULLS LAST",
            (f"{run_id}-%",)).fetchall()


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    s = sorted(values)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def report(outcomes: list[dict], latency: list[tuple], stamp: datetime, command: str) -> str:
    passed = sum(o["passed"] for o in outcomes)
    timings = [ms for o in outcomes for ms in o["timings"]]
    categories: dict[str, list[bool]] = {}
    for o in outcomes:
        categories.setdefault(o["covers"], []).append(o["passed"])
    lines = [
        "# Call-scenario results: backend contract test",
        "",
        f"Generated {stamp:%Y-%m-%d %H:%M} UTC by `{command}`. Do not edit by hand.",
        "",
        f"- **What ran:** {len(outcomes)} scripted calls, replayed as {len(timings)} Vapi-shaped "
        "`tool-calls` webhook POSTs against the running app (FastAPI + Postgres 17), shop clock "
        f"frozen at {FAKE_NOW} America/New_York (a Monday).",
        "- **Labels:** pass criteria hand-written per step in `evals/scenarios.py` (HTTP status, "
        "`result` vs `error`, phrases the reply must contain) plus the expected database state "
        "after each call.",
        "- **Not measured:** no LLM and no voice are involved. This tests the backend contract, "
        "not conversation quality.",
        "",
        f"**Passed: {passed}/{len(outcomes)} scenarios ({passed / len(outcomes):.0%}).**",
        "",
        "| Category | Scenarios | Passed |",
        "|---|---|---|",
        *[f"| {c} | {len(r)} | {sum(r)} |" for c, r in categories.items()],
        "",
        "| # | Scenario | Category | Result |",
        "|---|---|---|---|",
        *[f"| {o['number']} | {o['name']} | {o['covers']} | "
          f"{'PASS' if o['passed'] else 'FAIL: ' + '; '.join(o['problems'])} |" for o in outcomes],
        "",
        "## Tool latency",
        "",
        "Server-side time inside each tool (from the `tool_calls` table, `percentile_cont`). "
        "Includes requests that waited on a racing duplicate.",
        "",
        "| Tool | Calls | p50 ms | p95 ms |",
        "|---|---|---|---|",
        *[f"| {tool} | {n} | {p50:.1f} | {p95:.1f} |" for tool, n, p50, p95 in latency],
        "",
        f"HTTP round trip seen by the harness, same machine, no tunnel (includes opening a "
        f"database connection per request): p50 {pct(timings, 0.5):.1f} ms, "
        f"p95 {pct(timings, 0.95):.1f} ms over {len(timings)} requests.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://localhost:8004")
    ap.add_argument("--start-server", action="store_true", help="run the app with FAKE_NOW set")
    ap.add_argument("--trace", action="store_true", help="print every call as a transcript")
    args = ap.parse_args(argv)
    secret = os.environ.get("VAPI_SECRET") or sys.exit("Set VAPI_SECRET (make reads it from .env)")

    stamp = datetime.now(UTC)
    run_id = f"eval-{stamp:%Y%m%dT%H%M%SZ}"
    server = start_server(args.url) if args.start_server else None
    try:
        with httpx2.Client(base_url=args.url, timeout=30) as client:
            outcomes = []
            for number, scenario in enumerate(SCENARIOS, 1):
                o = run_scenario(client, secret, number, scenario, run_id)
                outcomes.append(o)
                print(f"{'PASS' if o['passed'] else 'FAIL'}  {number:02d}  {o['name']}"
                      f"  [{o['covers']}]")
                if args.trace or not o["passed"]:
                    print("\n".join(o["trace"] + [f"    !! {p}" for p in o["problems"]]) + "\n")
    finally:
        if server:
            server.terminate()
            server.wait(timeout=10)
    wipe()

    command = "python -m evals.run " + " ".join(argv if argv is not None else sys.argv[1:])
    out = HERE / "results.md"
    out.write_text(report(outcomes, tool_latency(run_id), stamp, command.strip()))
    passed = sum(o["passed"] for o in outcomes)
    print(f"\n{passed}/{len(outcomes)} scenarios passed. Details: {out}")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
