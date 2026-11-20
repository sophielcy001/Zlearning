from alembic import op
import sqlalchemy as sa


revision = "0019_daily_study_plan"
down_revision = "0018_prerequisite_learning"


def upgrade():
    op.create_table(
        "study_plan_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("weekly", sa.JSON(), nullable=False),
    )
    op.create_table(
        "study_day_overrides",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("windows", sa.JSON(), nullable=False),
    )
    op.create_index("ix_lessons_learned_on", "lessons", ["learned_on"])
    op.create_index("ix_attempts_submitted_on", "attempts", ["submitted_on"])
    op.create_index("ix_assessment_rounds_submitted_on", "assessment_rounds", ["submitted_on"])


def downgrade():
    op.drop_index("ix_assessment_rounds_submitted_on", table_name="assessment_rounds")
    op.drop_index("ix_attempts_submitted_on", table_name="attempts")
    op.drop_index("ix_lessons_learned_on", table_name="lessons")
    op.drop_table("study_day_overrides")
    op.drop_table("study_plan_settings")
