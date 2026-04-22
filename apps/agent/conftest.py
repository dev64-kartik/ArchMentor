"""Test-scoped env defaults for the agent worker.

`Settings` requires `ARCHMENTOR_AGENT_INGEST_TOKEN` and
`ARCHMENTOR_ANTHROPIC_API_KEY` at construction. Tests cannot depend on
the developer's `.env` file existing, so we seed dummy values here
before any `archmentor_agent` module is imported.

Mirrors `apps/api/tests/conftest.py`.
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "ARCHMENTOR_AGENT_INGEST_TOKEN",
    "test_agent_token_test_agent_token_test_agent_token",
)
os.environ.setdefault(
    "ARCHMENTOR_ANTHROPIC_API_KEY",
    "sk-ant-test-fixture-not-a-real-key",
)
