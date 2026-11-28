from alembic import op
import sqlalchemy as sa


revision = "0059_memory_inference"
down_revision = "0058_library_recent_order"


def upgrade():
    op.add_column("local_memories", sa.Column("origin", sa.String(), nullable=False, server_default="explicit"))
    op.create_table(
        "memory_inference_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_message_id", sa.String(36), nullable=False),
        sa.Column("memory_id", sa.String(36)),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("connection", sa.JSON(), nullable=False),
        sa.Column("scope", sa.JSON()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("daily_blocked_on", sa.Date()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("billing_day", sa.Date()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
    )
    op.create_index("ix_memory_inference_queue", "memory_inference_jobs", ["state", "created_at"])
    for field in ("source_message_id", "billing_day", "finished_at"):
        op.create_index(f"ix_memory_inference_jobs_{field}", "memory_inference_jobs", [field])


def downgrade():
    op.drop_table("memory_inference_jobs")
    op.drop_column("local_memories", "origin")