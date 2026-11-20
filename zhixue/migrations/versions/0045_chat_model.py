from alembic import op
import sqlalchemy as sa


revision = "0045_chat_model"
down_revision = "0044_reminder_push"


def upgrade():
    op.create_table(
        "chat_replies",
        sa.Column("id", sa.String(36), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("reply_id", sa.String(36), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("recipient", sa.JSON(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("daily_blocked_on", sa.Date()),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("active_call", sa.String(36)),
    )
    op.create_index("ix_chat_replies_queue", "chat_replies", ["state", "available_at", "created_at"])
    op.create_table(
        "chat_model_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reply_id", sa.String(36), sa.ForeignKey("chat_replies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("billing_day", sa.Date(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
    )
    for column in ("reply_id", "billing_day", "finished_at"):
        op.create_index(f"ix_chat_model_calls_{column}", "chat_model_calls", [column])


def downgrade():
    op.drop_table("chat_model_calls")
    op.drop_table("chat_replies")
