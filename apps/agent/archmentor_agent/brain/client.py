"""Claude Opus streaming + tool-use client.

Lands in M2. Responsible for:
- Prompt caching on static prefix (system prompt + problem + rubric + few-shots)
- Streaming tool-use output
- Per-call Langfuse trace
- Token accounting and cost cap enforcement
"""

from __future__ import annotations

# Implementation lands in M2.
