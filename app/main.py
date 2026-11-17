"""The HTTP surface: a health check and the one webhook Vapi calls for every tool call."""

import hmac
import logging
import os
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException

from app import outbox, shop, vapi
from common import db

log = logging.getLogger(__name__)
app = FastAPI(title=f"{shop.NAME} booking backend")

if os.environ.get("FAKE_NOW"):
    log.warning("FAKE_NOW is set: the shop clock is frozen at %s", os.environ["FAKE_NOW"])


def require_secret(authorization: Annotated[str, Header()] = "") -> None:
    """401 unless the request carries 'Authorization: Bearer <VAPI_SECRET>'.

    Fails closed: with VAPI_SECRET unset, every request is refused. compare_digest takes the
    same time however much of the secret matches, so it can't be guessed byte by byte.
    """
    secret = os.environ.get("VAPI_SECRET", "")
    if not secret or not hmac.compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
        raise HTTPException(status_code=401, detail="missing or wrong credentials")


@app.get("/health")
def health() -> dict[str, str]:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/vapi/tool-calls", dependencies=[Depends(require_secret)])
def vapi_tool_calls(webhook: vapi.Webhook, background: BackgroundTasks) -> dict:
    """Vapi's tool-calls webhook. Once authenticated it always answers 200; failures go in
    each result's `error` field. Confirmation emails go out after the reply is sent."""
    reply = vapi.handle(webhook, shop.now())
    background.add_task(outbox.send_pending_quietly)
    return reply
