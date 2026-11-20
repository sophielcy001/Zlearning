from alembic import op
import sqlalchemy as sa


revision = "0013_model_assessments"
down_revision = "0012_course_verification"


def upgrade():
    op.add_column("model_uses", sa.Column("assessment_job_id", sa.String(36)))
    op.create_index("ix_model_uses_assessment_job_id", "model_uses", ["assessment_job_id"])
    op.create_table(
        "assessment_plans",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), primary_key=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("primary_model", sa.JSON(), nullable=False),
        sa.Column("review_model", sa.JSON(none_as_null=True)),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("max_calls", sa.Integer()),
    )
    op.create_table(
        "assessment_objectives",
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), primary_key=True),
        sa.Column("key", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("ability", sa.String(), nullable=False),
        sa.Column("criterion", sa.String(), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("held", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "assessment_rounds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("key", sa.String(36), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("course_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("group", sa.JSON(none_as_null=True)),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("hinted", sa.Boolean(), nullable=False),
        sa.Column("submission", sa.JSON(none_as_null=True)),
        sa.Column("submitted_on", sa.Date()),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("previous_stage", sa.Integer()),
        sa.Column("previous_due_on", sa.Date()),
        sa.UniqueConstraint("lesson_id", "key", "number", "mode"),
    )
    op.create_index("ix_assessment_rounds_lesson_id", "assessment_rounds", ["lesson_id"])
    op.create_table(
        "assessment_grades",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("round_id", sa.String(36), sa.ForeignKey("assessment_rounds.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reviewer", sa.JSON(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )
    op.create_index("ix_assessment_grades_round_id", "assessment_grades", ["round_id"])
    op.create_table(
        "assessment_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("round_id", sa.String(36), sa.ForeignKey("assessment_rounds.id")),
        sa.Column("basis", sa.JSON(none_as_null=True)),
        sa.Column("first_due_on", sa.Date()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("active_call", sa.String(36)),
        sa.Column("error", sa.String()),
    )
    for field in ("lesson_id", "round_id"):
        op.create_index(f"ix_assessment_jobs_{field}", "assessment_jobs", [field])


def downgrade():
    for table in ("assessment_jobs", "assessment_grades", "assessment_rounds", "assessment_objectives", "assessment_plans"):
        op.drop_table(table)
    op.drop_index("ix_model_uses_assessment_job_id", table_name="model_uses")
    op.drop_column("model_uses", "assessment_job_id")
