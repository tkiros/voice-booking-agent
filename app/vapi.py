"""Vapi adapter: turns Vapi's `tool-calls` webhook into calls on app.booking, and back.

This is the only file that knows Vapi's format (docs.vapi.ai/tools/custom-tools). Request:
  {"message": {"type": "tool-calls", "call": {"id": "..."}, "toolCallList": [
      {"id": "call_1", "type": "function", "function": {"name": "...", "arguments": {...}}}]}}
Reply, always HTTP 200, one entry per tool call, in order. `result` and `error` are flat,
single-line strings; a failure goes in `error` instead of `result`:
  {"results": [{"toolCallId": "call_1", "result": "Booked ..."}]}
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import psycopg
from pydantic import BaseModel, Field, ValidationError

from app import booking, shop
from common import db

log = logging.getLogger(__name__)

# --- the webhook envelope (FastAPI rejects anything that doesn't fit with a 422) ----------------


class Function(BaseModel):
    name: str
    arguments: dict | str = {}  # Vapi sends an object; a JSON string is accepted too


class ToolCall(BaseModel):
    id: str
    function: Function


class Message(BaseModel):
    type: str
    toolCallList: list[ToolCall] = []  # Vapi's field name, hence the camelCase
    call: dict = {}
    customer: dict = {}


class Webhook(BaseModel):
    message: Message


# --- the tools' arguments: validated here, and turned into the JSON schema Vapi shows the model -

WHEN = ("The caller's own words for the day and time, e.g. 'next Tuesday at 2pm' or 'tomorrow "
        "morning'. Pass them unchanged; never work out a date yourself.")
MECHANIC = "Dana, Luis or Priya, only if the caller asked for one. Empty means anyone."
BOOKING_ID = "The booking number, if the caller knows it or a previous answer gave it. 0 if not."


class CheckAvailability(BaseModel):
    """Check whether a day, or an exact time, is open. Returns open times, or the nearest
    alternatives when the time is taken or the shop is closed."""

    when: str = Field(description=WHEN)
    mechanic: str = Field("", description=MECHANIC)


class CreateBooking(BaseModel):
    """Book an appointment. Only call this after reading the day, time and mechanic back to
    the caller and hearing them say yes."""

    when: str = Field(description=WHEN)
    mechanic: str = Field("", description=MECHANIC)
    email: str = Field("", description="Email address for the confirmation, if the caller "
                                       "gave one. Empty if not.")


class Reschedule(BaseModel):
    """Move the caller's existing appointment to a new time. Read the new day and time back
    to the caller first."""

    when: str = Field(description="The NEW day and time, in the caller's own words.")
    booking_id: int = Field(0, description=BOOKING_ID)
    mechanic: str = Field("", description=MECHANIC)


class Cancel(BaseModel):
    """Cancel the caller's appointment. Confirm with the caller first."""

    booking_id: int = Field(0, description=BOOKING_ID)


class RequestCallback(BaseModel):
    """Ask a staff member to call the caller back: they want a person, need something these
    tools can't do, or a tool failed."""

    reason: str = Field(description="One sentence on what the caller needs.")


TOOLS = {
    "check_availability": CheckAvailability,
    "create_booking": CreateBooking,
    "reschedule": Reschedule,
    "cancel": Cancel,
    "request_callback": RequestCallback,
}


# --- handling one webhook ---------------------------------------------------------------------


def handle(webhook: Webhook, now: datetime) -> dict:
    """Answer every tool call in the webhook, in order."""
    message = webhook.message
    calls = message.toolCallList if message.type == "tool-calls" else []
    caller = caller_number(message)
    if calls and not caller:  # the number's field path isn't in Vapi's docs: this shows what came
        log.warning("no caller number found; call object keys: %s", sorted(message.call))
    call_id = message.call.get("id")
    try:
        with db.connect() as conn:
            return {"results": [run_tool(conn, c, caller, call_id, now) for c in calls]}
    except psycopg.OperationalError:
        log.exception("database unreachable")
        down = booking.fail("The booking system is down. Apologise and ask the caller to try "
                            "again later.")
        return {"results": [vapi_result(c.id, down) for c in calls]}


def run_tool(conn, tool_call: ToolCall, caller: str, call_id: str | None, now: datetime) -> dict:
    """Run one tool call and log how long it took. Never raises: the call must go on."""
    name = tool_call.function.name
    started = time.perf_counter()
    try:
        outcome = dispatch(conn, name, arguments(tool_call.function), caller, now)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
                             for err in e.errors())
        outcome = booking.fail(f"Bad arguments for {name}: {problems}.")
    except BadArguments as e:
        outcome = booking.fail(f"Bad arguments for {name}: {e}.")
    except Exception:
        log.exception("tool %s crashed", name)
        outcome = booking.fail("Something went wrong on our side. Tell the caller a staff "
                               "member will follow up, and call request_callback.")
    latency_ms = (time.perf_counter() - started) * 1000
    log_tool_call(conn, name, call_id, outcome["ok"], latency_ms)
    return vapi_result(tool_call.id, outcome)


def dispatch(conn, name: str, args: dict, caller: str, now: datetime) -> dict:
    """Validate the arguments for tool `name` and call the matching booking function."""
    if name not in TOOLS:
        return booking.fail(f"Unknown tool '{name}'. Use one of: {', '.join(TOOLS)}.")
    a = TOOLS[name].model_validate(args)
    if name == "check_availability":
        return booking.check_availability(conn, a.when, a.mechanic, now=now)
    if not caller:  # every other tool acts on the caller's own bookings
        return booking.fail("This call has no caller phone number, so nothing can be booked or "
                            "changed. Offer a callback instead.")
    if name == "create_booking":
        return booking.book(conn, caller, a.when, a.mechanic, a.email, now=now)
    if name == "reschedule":
        return booking.reschedule(conn, caller, a.when, a.booking_id, a.mechanic, now=now)
    if name == "cancel":
        return booking.cancel(conn, caller, a.booking_id, now=now)
    return booking.request_callback(conn, caller, a.reason)


class BadArguments(ValueError):
    """The tool arguments weren't a JSON object."""


def arguments(function: Function) -> dict:
    """The tool arguments as a dict. Nulls are dropped: models send them for 'not given'."""
    args = function.arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            raise BadArguments("arguments are not valid JSON") from None
    if not isinstance(args, dict):
        raise BadArguments("arguments must be a JSON object")
    return {k: v for k, v in args.items() if v is not None}


def caller_number(message: Message) -> str:
    """The caller's phone number. Bookings belong to it, so it comes from the platform's
    payload, never from what the caller says. (Field path not yet confirmed on a live call.)"""
    for source in (message.call.get("customer"), message.customer):
        if isinstance(source, dict) and source.get("number"):
            return str(source["number"])
    return ""


def vapi_result(tool_call_id: str, outcome: dict) -> dict:
    """One entry of Vapi's `results` list. Vapi needs the text on a single line."""
    key = "result" if outcome["ok"] else "error"
    return {"toolCallId": tool_call_id, key: " ".join(outcome["message"].split())}


def log_tool_call(conn, tool: str, call_id: str | None, ok: bool, latency_ms: float) -> None:
    """One tool_calls row. Losing a latency row must not lose the caller's answer."""
    try:
        conn.execute(
            "INSERT INTO tool_calls (tool, call_id, ok, latency_ms) VALUES (%s, %s, %s, %s)",
            (tool, call_id, ok, latency_ms))
    except psycopg.Error:
        log.exception("could not log tool call")


# --- what to register with Vapi (python -m app.vapi writes it to vapi/*.json) -------------------

# Placeholders: replace with your tunnel URL and the id of your Bearer Token credential.
SERVER_URL = "https://YOUR-TUNNEL-HOST/vapi/tool-calls"
CREDENTIAL_ID = "YOUR-BEARER-CREDENTIAL-ID"

# Hours and mechanics repeat app/shop.py: keep them in step when the shop changes.
SYSTEM_PROMPT = f"""\
You answer the phone for {shop.NAME}, a car repair shop. This is a phone call: keep every \
reply to one or two short sentences.

You book one-hour appointments with our mechanics Dana, Luis and Priya. We're open Monday to \
Friday 8 AM to 5 PM and Saturday 9 AM to 1 PM, and closed on Sunday.

Rules:
- Never work out a date yourself. Put the caller's own words for the day and time in the \
`when` argument, for example "next Tuesday at 2pm". The tools turn them into dates.
- Before create_booking or reschedule, read the day, date, time and mechanic back to the \
caller, exactly as check_availability phrased them, and wait for a yes.
- When a tool answers with an error, do what it says: ask the question it contains, or offer \
the alternatives it lists. Never offer times a tool didn't give you.
- After booking, tell the caller their booking number.
- Ask for an email address only if the caller wants an email confirmation, and spell it back.
- If the caller wants a person, needs something these tools can't do, or a tool fails or \
doesn't answer, call request_callback and say a staff member will call them back.
- Politely decline anything that isn't about appointments at the shop."""


def assistant_definition() -> dict:
    """The assistant, with the field names shown in Vapi's docs. Attach the tools by id."""
    return {
        "name": f"{shop.NAME} booking line",
        "firstMessage": f"Thanks for calling {shop.NAME}. How can I help?",
        "model": {
            "provider": "openai",  # from Vapi's docs example; any tool-calling model works
            "model": "gpt-4o",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            "toolIds": [f"ID-OF-{name}" for name in TOOLS],
        },
    }


def registered_tools() -> list[dict]:
    """Each tool as Vapi's POST /tool body: the definition plus where to send tool-calls."""
    server = {"url": SERVER_URL, "credentialId": CREDENTIAL_ID}
    return [tool | {"server": server} for tool in tool_definitions()]


def write_config(folder: Path = Path("vapi")) -> None:
    """Regenerate vapi/tools.json and vapi/assistant.json from the code above."""
    for name, data in (("tools.json", registered_tools()), ("assistant.json",
                                                            assistant_definition())):
        (folder / name).write_text(json.dumps(data, indent=2) + "\n")


def tool_definitions() -> list[dict]:
    """Vapi function-tool definitions, built from the argument models so they can't drift."""
    return [
        {"type": "function",
         "function": {"name": name, "description": " ".join(model.__doc__.split()),
                      "parameters": parameters(model)}}
        for name, model in TOOLS.items()
    ]


def parameters(model: type[BaseModel]) -> dict:
    """The model's JSON schema without Pydantic's 'title' noise."""
    schema = model.model_json_schema()
    properties = {
        field: {k: v for k, v in spec.items() if k != "title"}
        for field, spec in schema["properties"].items()
    }
    return {"type": "object", "properties": properties, "required": schema.get("required", [])}


if __name__ == "__main__":
    write_config()
