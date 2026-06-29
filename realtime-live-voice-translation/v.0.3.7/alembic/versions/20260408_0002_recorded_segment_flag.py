"""recorded segment flag

Revision ID: 20260408_0002
Revises: 20260407_0001
Create Date: 2026-04-08 00:02:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260408_0002"
down_revision = "20260407_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call_segments",
        sa.Column(
            "included_in_recording",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("call_segments", "included_in_recording")
