"""segment fact check json

Revision ID: 20260414_0003
Revises: 20260408_0002
Create Date: 2026-04-14 00:03:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260414_0003"
down_revision = "20260408_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call_segments",
        sa.Column("fact_check_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("call_segments", "fact_check_json")
