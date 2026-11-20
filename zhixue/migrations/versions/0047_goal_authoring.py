from alembic import op
import sqlalchemy as sa


revision = "0047_goal_authoring"
down_revision = "0046_teaching_style"


def upgrade():
    op.create_table(
        "goal_authoring_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("goal_id", sa.String(36), nullable=False),
        sa.Column("confirmation", sa.String(64), nullable=False),
        sa.Column("connection", sa.JSON(), nullable=False),
        sa.Column("scope", sa.JSON()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("daily_blocked_on", sa.Date()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("billing_day", sa.Date()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
    )
    op.create_index("ix_goal_authoring_queue", "goal_authoring_jobs", ["state", "created_at"])
    op.create_index("ix_goal_authoring_thread", "goal_authoring_jobs", ["conversation_id", "goal_id", "created_at"])
    for column in ("billing_day", "finished_at"):
        op.create_index(f"ix_goal_authoring_jobs_{column}", "goal_authoring_jobs", [column])


def downgrade():
    op.drop_table("goal_authoring_jobs")
