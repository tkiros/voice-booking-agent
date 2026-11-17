"""Confirmations and callback requests, written to the outbox table and (optionally) emailed.

Rows are written inside the same transaction as the booking change, so a booking can't exist
without its confirmation. Emailing happens later, after the webhook has answered, so a slow mail
server never adds to the caller's wait. Without SMTP_HOST the outbox row is the confirmation.
No SMS: US texting from a regular number needs carrier registration (A2P 10DLC).
"""

import logging
import os
import re
import smtplib
from email.message import EmailMessage

import psycopg

from app import shop
from common import db

log = logging.getLogger(__name__)

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def add(conn, kind: str, caller: str, body: str, booking_id=None, email: str = "") -> None:
    """Queue one message. An email that doesn't look like one is dropped, not sent."""
    address = email.strip() if EMAIL.fullmatch(email.strip()) else None
    conn.execute(
        "INSERT INTO outbox (booking_id, kind, caller, email, body) VALUES (%s, %s, %s, %s, %s)",
        (booking_id, kind, caller, address, body),
    )


def email_for(conn, booking_id: int) -> str:
    """The address an earlier confirmation for this booking went to, or ''."""
    row = conn.execute(
        "SELECT email FROM outbox WHERE booking_id = %s AND email IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (booking_id,),
    ).fetchone()
    return row[0] if row else ""


def send_pending() -> int:
    """Email every unsent outbox row that has an address. Returns how many were sent.

    One transaction per row: the row is locked (so two senders never both send it), the email
    goes out, then sent_at is set. A crash between those last two steps sends it again on the
    next run -- a duplicate email is better than a missing confirmation.
    """
    if not os.environ.get("SMTP_HOST"):
        return 0
    sent = 0
    with db.connect() as conn:
        while True:
            with conn.transaction():
                row = conn.execute(
                    "SELECT id, email, body FROM outbox"
                    " WHERE sent_at IS NULL AND last_error IS NULL AND email IS NOT NULL"
                    " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
                ).fetchone()
                if row is None:
                    return sent
                outbox_id, address, body = row
                try:
                    send_email(address, body)
                except (smtplib.SMTPException, OSError) as e:
                    log.exception("outbox %s: email to %s failed", outbox_id, address)
                    conn.execute(
                        "UPDATE outbox SET last_error = %s WHERE id = %s", (str(e), outbox_id)
                    )
                    continue
                conn.execute("UPDATE outbox SET sent_at = now() WHERE id = %s", (outbox_id,))
                sent += 1


def send_email(address: str, body: str) -> None:
    """One plain-text email through the SMTP server in the environment."""
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "bookings@example.com")
    msg["To"] = address
    msg["Subject"] = f"Your appointment at {shop.NAME}"
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=10) as smtp:
        if os.environ.get("SMTP_USER"):
            smtp.starttls()
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)


def send_pending_quietly() -> None:
    """Background-task wrapper: a mail problem must never surface as a webhook error."""
    try:
        send_pending()
    except psycopg.Error:
        log.exception("outbox: could not read pending messages")
