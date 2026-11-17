"""Shared test setup. Database tests skip themselves unless `make up` is running."""

import os
from datetime import datetime

import psycopg
import pytest

from app.shop import TZ

# Every DB test runs "on" Monday 16 November 2026 at 8:30 AM, shop time.
NOW = datetime(2026, 11, 16, 8, 30, tzinfo=TZ)


def db_up() -> bool:
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=2):
            return True
    except (KeyError, psycopg.Error):
        return False


@pytest.fixture
def conn():
    """A connection to the dev database with empty booking tables (tool_calls is kept)."""
    if not db_up():
        pytest.skip("needs `make up` (DATABASE_URL reachable)")
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        c.execute("TRUNCATE bookings, outbox, baseline.bookings RESTART IDENTITY")
        yield c


def confirmed(conn) -> list[tuple]:
    """(id, resource_id, caller) of every confirmed booking, oldest first."""
    return conn.execute(
        "SELECT id, resource_id, caller FROM bookings WHERE status = 'confirmed' ORDER BY id"
    ).fetchall()


def outbox_kinds(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT kind FROM outbox ORDER BY id").fetchall()]
