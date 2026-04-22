"""Test-scoped env defaults + helper-module sys.path injection.

`Settings` requires `ARCHMENTOR_AGENT_INGEST_TOKEN` and
`ARCHMENTOR_ANTHROPIC_API_KEY` at construction. Tests cannot depend on
the developer's `.env` file existing, so we seed dummy values here
before any `archmentor_agent` module is imported.

Also inserts `apps/agent/tests/` onto `sys.path` so `_helpers` resolves
as a top-level package. Why not `from tests._helpers import ...` or
`from .fakes import ...`? Pytest's `--import-mode=importlib` (root
`pyproject.toml`) makes each test file a top-level module without a
parent package, AND `apps/api/tests/` already claims the `tests`
package name when both apps' tests are collected together. A single
sys.path insertion side-steps both issues without polluting prod
source with test helpers.

Mirrors `apps/api/tests/conftest.py` for the env-var part.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault(
    "ARCHMENTOR_AGENT_INGEST_TOKEN",
    "test_agent_token_test_agent_token_test_agent_token",
)
os.environ.setdefault(
    "ARCHMENTOR_ANTHROPIC_API_KEY",
    "sk-ant-test-fixture-not-a-real-key",
)

_TESTS_DIR = Path(__file__).parent / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
