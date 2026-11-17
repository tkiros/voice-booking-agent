"""The Vapi webhook over HTTP (FastAPI TestClient): auth, reply format, logging, failure paths."""

import smtplib

import pytest
from fastapi.testclient import TestClient

from app import booking
from app.main import app
from tests.conftest import outbox_kinds

SECRET = "test-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}
ANA = "+15550100001"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("VAPI_SECRET", SECRET)
    monkeypatch.setenv("FAKE_NOW", "2026-11-16T08:30")
    monkeypatch.delenv("SMTP_HOST", raising=False)


client = TestClient(app)


def webhook(*calls, caller=ANA, call_id="test-call") -> dict:
    """A Vapi tool-calls message. Each call is (tool name, arguments)."""
    return {"message": {
        "type": "tool-calls",
        "call": {"id": call_id, "customer": {"number": caller}} if caller else {"id": call_id},
        "toolCallList": [
            {"id": f"tc{i}", "type": "function", "function": {"name": name, "arguments": args}}
            for i, (name, args) in enumerate(calls)
        ],
    }}


def post(body, headers=AUTH):
    return client.post("/vapi/tool-calls", json=body, headers=headers)


# --- auth (no database needed: rejected before any query) -------------------------------------


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong"},
    {"Authorization": SECRET},  # right secret, missing "Bearer "
    {"X-Vapi-Secret": SECRET},  # right secret, wrong header
])
def test_bad_or_missing_secret_is_401(headers):
    assert post(webhook(("check_availability", {"when": "Friday"})), headers).status_code == 401


def test_unset_server_secret_refuses_everything(monkeypatch):
    monkeypatch.setenv("VAPI_SECRET", "")
    assert post(webhook(), {"Authorization": "Bearer "}).status_code == 401


def test_malformed_envelope_is_422():
    assert post({"message": {"toolCallList": "nope"}}).status_code == 422


def test_database_down_still_answers_200_with_an_error_per_call(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@127.0.0.1:1/app?connect_timeout=1")
    r = post(webhook(("check_availability", {"when": "Friday"}), ("cancel", {})))
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["toolCallId"] for x in results] == ["tc0", "tc1"]
    assert all("booking system is down" in x["error"] for x in results)


# --- the reply format (database needed) --------------------------------------------------------


def test_result_shape_one_entry_per_call_in_order(conn):
    r = post(webhook(("check_availability", {"when": "Friday at 2pm"}),
                     ("create_booking", {"when": "Friday at 2pm"})))
    assert r.status_code == 200
    assert r.json() == {"results": [
        {"toolCallId": "tc0", "result": "Friday, November 20 at 2:00 PM is open with Dana, Luis "
                                        "and Priya."},
        {"toolCallId": "tc1", "result": "Booked Friday, November 20 at 2:00 PM with Dana. "
                                        "Booking number 1."},
    ]}


def test_failures_go_in_error_not_result_and_stay_on_one_line(conn):
    r = post(webhook(("create_booking", {"when": "next Tuesday at 9"})))
    (only,) = r.json()["results"]
    assert set(only) == {"toolCallId", "error"}
    assert "\n" not in only["error"] and "November 17 or Tuesday, November 24" in only["error"]


def test_arguments_may_arrive_as_a_json_string_and_nulls_mean_not_given(conn):
    r = post(webhook(("create_booking", '{"when": "Friday at 2pm", "mechanic": null}')))
    assert "Booked" in r.json()["results"][0]["result"]


@pytest.mark.parametrize("name, args, error", [
    ("create_booking", {}, "Bad arguments for create_booking: when: Field required."),
    ("create_booking", "{not json", "Bad arguments for create_booking: arguments are not valid"),
    ("cancel", {"booking_id": "seven"}, "booking_id: Input should be a valid integer"),
    ("transfer_to_mars", {}, "Unknown tool 'transfer_to_mars'"),
])
def test_bad_tool_calls_get_an_error_the_agent_can_read(conn, name, args, error):
    r = post(webhook((name, args)))
    assert r.status_code == 200
    assert error in r.json()["results"][0]["error"]


def test_no_caller_number_can_check_but_not_book(conn):
    r = post(webhook(("check_availability", {"when": "Friday at 2pm"}),
                     ("create_booking", {"when": "Friday at 2pm"}), caller=None))
    check, create = r.json()["results"]
    assert "result" in check
    assert "no caller phone number" in create["error"]


def test_other_message_types_are_acknowledged_and_ignored(conn):
    r = post({"message": {"type": "status-update", "call": {"id": "x"}}})
    assert r.status_code == 200 and r.json() == {"results": []}


def test_every_tool_call_is_timed_into_tool_calls(conn):
    conn.execute("DELETE FROM tool_calls WHERE call_id = 'latency-test'")
    post(webhook(("check_availability", {"when": "Friday"}), ("cancel", {}),
                 call_id="latency-test"))
    rows = conn.execute("SELECT tool, ok, latency_ms FROM tool_calls WHERE call_id = "
                        "'latency-test' ORDER BY id").fetchall()
    conn.execute("DELETE FROM tool_calls WHERE call_id = 'latency-test'")
    assert [(tool, ok) for tool, ok, _ in rows] == [("check_availability", True), ("cancel", False)]
    assert all(ms > 0 for _, _, ms in rows)


# --- confirmation email (SMTP faked; the outbox is real) ------------------------------------


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, msg):
        if msg["To"] == "bounce@example.com":
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no such user")})
        FakeSMTP.sent.append(msg)


def test_confirmation_is_emailed_after_the_reply_and_marked_sent(conn, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    post(webhook(("create_booking", {"when": "Friday at 2pm", "email": "ana@example.com"})))
    assert [m["To"] for m in FakeSMTP.sent] == ["ana@example.com"]
    assert "Friday, November 20 at 2:00 PM" in FakeSMTP.sent[0].get_content()
    assert conn.execute("SELECT sent_at IS NOT NULL FROM outbox").fetchone() == (True,)


def test_a_bounced_email_is_parked_and_does_not_block_the_next(conn, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    post(webhook(("create_booking", {"when": "Friday at 2pm", "email": "bounce@example.com"}),
                 ("create_booking", {"when": "Friday at 3pm", "email": "ana@example.com"})))
    assert [m["To"] for m in FakeSMTP.sent] == ["ana@example.com"]
    rows = conn.execute("SELECT email, sent_at IS NOT NULL, last_error IS NOT NULL FROM outbox"
                        " ORDER BY id").fetchall()
    assert rows == [("bounce@example.com", False, True), ("ana@example.com", True, False)]
    assert outbox_kinds(conn) == ["confirmed", "confirmed"]


def test_a_crashing_tool_still_answers_and_points_the_agent_to_a_callback(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(booking, "check_availability", boom)
    r = post(webhook(("check_availability", {"when": "Friday"}), ("cancel", {})))
    first, second = r.json()["results"]
    assert r.status_code == 200
    assert "call request_callback" in first["error"]
    assert "no upcoming booking" in second["error"]  # the next call in the list still ran
