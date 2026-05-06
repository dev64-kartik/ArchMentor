"""add summary_compressed + summary_compression_failed to sessioneventtype enum

M4 Unit 5/6: the Haiku compactor writes a `summary_compressed` ledger
row per successful compaction (carrying `{model, input_tokens,
output_tokens, cost_usd, dropped_turn_count, summary_chars_before,
summary_chars_after}`) and a `summary_compression_failed` row when the
Haiku call raises (carrying `{dropped_turn_count}`). Without these
enum values the agent's ledger client gets a 422 from the
`/sessions/{id}/events` route and the compaction record (or its
failure) is silently lost — the failure path is the one we most need
visibility on during a Haiku outage.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction in
PostgreSQL < 12. Postgres 16 (the docker-compose target) supports it
inside a transaction, but we use `op.get_context().autocommit_block()`
belt-and-braces so this migration ports cleanly to older deployments.

Downgrade is a no-op: PostgreSQL cannot remove enum values without
recreating the type, and the only callers are M4-or-newer agents.
Leaving the new values unused on a downgraded database is harmless.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / other engines have no native ENUM type — the value
        # is enforced at the application layer via Pydantic. Nothing
        # to do here.
        return
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE sessioneventtype ADD VALUE IF NOT EXISTS 'summary_compressed'")
        op.execute(
            "ALTER TYPE sessioneventtype ADD VALUE IF NOT EXISTS 'summary_compression_failed'"
        )


def downgrade() -> None:
    # No-op: PostgreSQL cannot drop enum values without rebuilding the
    # type. Leaving the new values unused on a downgraded database is
    # harmless.
    pass
