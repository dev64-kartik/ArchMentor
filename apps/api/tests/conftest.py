"""Test-scoped env defaults.

Settings requires a non-empty `API_JWT_SECRET` at startup. The test suite
cannot depend on the developer's `.env` file existing, so we seed a
fixed dummy value here before any archmentor_api module is imported.
"""

from __future__ import annotations

import os

os.environ.setdefault("API_JWT_SECRET", "test_secret_test_secret_test_secret_test_secret")
os.environ.setdefault("API_JWT_ISSUER", "http://localhost:9999")
os.environ.setdefault("API_LIVEKIT_API_KEY", "devkey")
os.environ.setdefault(
    "API_LIVEKIT_API_SECRET", "test_lk_secret_test_lk_secret_test_lk_secret_test_lk"
)
os.environ.setdefault(
    "API_AGENT_INGEST_TOKEN", "test_agent_token_test_agent_token_test_agent_token"
)
