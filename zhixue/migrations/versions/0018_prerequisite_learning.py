from alembic import op
import sqlalchemy as sa


revision = "0018_prerequisite_learning"
down_revision = "0017_lesson_consolidation"


def upgrade():
    op.create_table(
        "prerequisite_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="SET NULL")),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("topic_key", sa.String(), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("max_calls", sa.Integer()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("reason", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("active_call", sa.String(36)),
        sa.Column("draft", sa.JSON(none_as_null=True)),
        sa.Column("author", sa.JSON(none_as_null=True)),
        sa.Column("findings", sa.JSON(none_as_null=True)),
        sa.Column("reviewer", sa.JSON(none_as_null=True)),
        sa.Column("revision", sa.String(64)),
        sa.Column("published_at", sa.DateTime()),
    )
    op.create_index("ix_prerequisite_tasks_lesson_id", "prerequisite_tasks", ["lesson_id"])
    op.create_index("ix_prerequisite_tasks_topic", "prerequisite_tasks", ["topic_key", "created_at"])
    op.create_index("ix_prerequisite_tasks_queue", "prerequisite_tasks", ["state", "available_at", "created_at"])
    op.create_table(
        "prerequisite_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("prerequisite_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("connection", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("billing_day", sa.Date(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("auto_retry", sa.Boolean(), nullable=False),
    )
    for column in ("task_id", "billing_day", "finished_at"):
        op.create_index(f"ix_prerequisite_calls_{column}", "prerequisite_calls", [column])


def downgrade():
    op.drop_table("prerequisite_calls")
    op.drop_table("prerequisite_tasks")
