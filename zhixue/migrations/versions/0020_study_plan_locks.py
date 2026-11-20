from alembic import op
import sqlalchemy as sa


revision = "0020_study_plan_locks"
down_revision = "0019_daily_study_plan"


def upgrade():
    op.create_table(
        "study_plan_locks",
        sa.Column("task_id", sa.String(160), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("review_key", sa.String()),
        sa.Column("review_round", sa.Integer()),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("estimate_source", sa.String(), nullable=False),
        sa.Column("completed_on", sa.Date()),
        sa.CheckConstraint("start_minute >= 0 AND minutes > 0 AND start_minute + minutes <= 1440",
                           name="ck_study_plan_locks_time"),
        sa.CheckConstraint("(review_key IS NULL) = (review_round IS NULL)", name="ck_study_plan_locks_round"),
    )
    op.create_index("ix_study_plan_locks_day", "study_plan_locks", ["day"])
    op.create_index("ix_study_plan_locks_lesson_id", "study_plan_locks", ["lesson_id"])


def downgrade():
    op.drop_table("study_plan_locks")
