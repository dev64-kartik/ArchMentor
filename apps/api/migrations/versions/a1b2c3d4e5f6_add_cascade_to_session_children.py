"""add cascade to session children

Adds `ON DELETE CASCADE` to every FK that references `sessions.id`.
M3 promises that `DELETE /sessions/{id}` is a hard delete cascading to
all child rows; without this constraint the database happily orphans
event/snapshot/interruption/report rows pointing at a deleted session.

Affected tables: session_events, brain_snapshots, canvas_snapshots,
interruptions, reports.

Revision ID: a1b2c3d4e5f6
Revises: 7250b3970037
Create Date: 2026-04-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "7250b3970037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Postgres auto-names FKs `<table>_<column>_fkey`. SQLite ignores
# constraint names entirely; the migration is a no-op there.
_CHILDREN: tuple[tuple[str, str], ...] = (
    ("session_events", "session_events_session_id_fkey"),
    ("brain_snapshots", "brain_snapshots_session_id_fkey"),
    ("canvas_snapshots", "canvas_snapshots_session_id_fkey"),
    ("interruptions", "interruptions_session_id_fkey"),
    ("reports", "reports_session_id_fkey"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite tests rely on SQLModel.metadata.create_all() which now
        # picks up `ondelete="CASCADE"` from the model definitions.
        return
    for table, fk_name in _CHILDREN:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.create_foreign_key(
            fk_name,
            table,
            "sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, fk_name in _CHILDREN:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.create_foreign_key(
            fk_name,
            table,
            "sessions",
            ["session_id"],
            ["id"],
        )
