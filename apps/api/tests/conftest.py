"""Test-scoped env defaults + SQLite FK enforcement.

Settings requires a non-empty `API_JWT_SECRET` at startup. The test suite
cannot depend on the developer's `.env` file existing, so we seed a
fixed dummy value here before any archmentor_api module is imported.

We also globally enable `PRAGMA foreign_keys=ON` on every SQLite
connection so cascade-delete behaviour mirrors Postgres in the test
harness. Without this, SQLite parses FK declarations but silently
ignores them at runtime, which would let the cascade tests pass on a
broken schema.
"""

from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("API_JWT_SECRET", "test_secret_test_secret_test_secret_test_secret")
os.environ.setdefault("API_JWT_ISSUER", "http://localhost:9999")
os.environ.setdefault("API_LIVEKIT_API_KEY", "devkey")
os.environ.setdefault(
    "API_LIVEKIT_API_SECRET", "test_lk_secret_test_lk_secret_test_lk_secret_test_lk"
)
os.environ.setdefault(
    "API_AGENT_INGEST_TOKEN", "test_agent_token_test_agent_token_test_agent_token"
)


from sqlalchemy import event
from sqlalchemy.engine import Engine


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
