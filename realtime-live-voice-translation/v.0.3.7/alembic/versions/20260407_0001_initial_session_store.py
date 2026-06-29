"""initial session store

Revision ID: 20260407_0001
Revises:
Create Date: 2026-04-07 00:00:01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260407_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("room_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("recording_state", sa.String(length=32), nullable=False, server_default="idle"),
        sa.Column("src_language", sa.String(length=16), nullable=False),
        sa.Column("presenter_target_language", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_call_sessions_room_id", "call_sessions", ["room_id"])
    op.create_index("ix_call_sessions_expires_at", "call_sessions", ["expires_at"])

    op.create_table(
        "call_segments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("call_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("segment_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_final", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_language", sa.String(length=16), nullable=False),
        sa.Column("translations_json", sa.JSON(), nullable=False),
        sa.Column("ts_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_call_segments_session_id", "call_segments", ["session_id"])
    op.create_index("ix_call_segments_segment_id", "call_segments", ["segment_id"])

    op.create_table(
        "recording_chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("call_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_recording_chunks_session_id", "recording_chunks", ["session_id"])

    op.create_table(
        "export_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("call_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(length=128), nullable=False, server_default="Queued"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("archive_name", sa.String(length=256), nullable=True),
        sa.Column("artifact_path", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_export_jobs_session_id", "export_jobs", ["session_id"])
    op.create_index("ix_export_jobs_expires_at", "export_jobs", ["expires_at"])

    op.create_table(
        "session_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("call_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("window_start_ms", sa.BigInteger(), nullable=True),
        sa.Column("window_end_ms", sa.BigInteger(), nullable=True),
        sa.Column("summary_en", sa.Text(), nullable=False, server_default=""),
        sa.Column("minutes_en", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_session_checkpoints_session_id", "session_checkpoints", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_session_checkpoints_session_id", table_name="session_checkpoints")
    op.drop_table("session_checkpoints")
    op.drop_index("ix_export_jobs_expires_at", table_name="export_jobs")
    op.drop_index("ix_export_jobs_session_id", table_name="export_jobs")
    op.drop_table("export_jobs")
    op.drop_index("ix_recording_chunks_session_id", table_name="recording_chunks")
    op.drop_table("recording_chunks")
    op.drop_index("ix_call_segments_segment_id", table_name="call_segments")
    op.drop_index("ix_call_segments_session_id", table_name="call_segments")
    op.drop_table("call_segments")
    op.drop_index("ix_call_sessions_expires_at", table_name="call_sessions")
    op.drop_index("ix_call_sessions_room_id", table_name="call_sessions")
    op.drop_table("call_sessions")
