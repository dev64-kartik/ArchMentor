"""drop canvas_snapshots.diff_from_prev_json

M3 ships full-scene-only canvas transport (refinements R4); the
`diff_from_prev_json` column was unused. Dropping it now keeps the
schema honest — speculative columns get filled with placeholder data
from later migrations and silently mislead future readers.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("canvas_snapshots", "diff_from_prev_json")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        column_type: sa.types.TypeEngine[object] = postgresql.JSONB(astext_type=sa.Text())
    else:
        column_type = sa.JSON()
    op.add_column(
        "canvas_snapshots",
        sa.Column("diff_from_prev_json", column_type, nullable=True),
    )
