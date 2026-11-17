import os

import psycopg


def connect() -> psycopg.Connection:
    # ponytail: a new connection per call; switch to psycopg_pool when throughput matters.
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
