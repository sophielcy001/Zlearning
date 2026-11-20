from alembic import op
import sqlalchemy as sa


revision = "0014_review_remediation"
down_revision = "0013_model_assessments"


def upgrade():
    op.add_column("assessment_plans", sa.Column("remediation", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "remediation_cards",
        sa.Column("grade_id", sa.String(36), sa.ForeignKey("assessment_grades.id"), primary_key=True),
        sa.Column("lesson_id", sa.String(36), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("round_id", sa.String(36), sa.ForeignKey("assessment_rounds.id"), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("draft", sa.JSON(none_as_null=True)),
        sa.Column("author", sa.JSON(none_as_null=True)),
        sa.Column("findings", sa.JSON(none_as_null=True)),
        sa.Column("reviewer", sa.JSON(none_as_null=True)),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("revision", sa.String(64)),
        sa.Column("publish_error", sa.String()),
    )
    for field in ("lesson_id", "round_id"):
        op.create_index(f"ix_remediation_cards_{field}", "remediation_cards", [field])
    op.execute("ALTER TABLE assessment_jobs ADD COLUMN remediation_id VARCHAR(36) REFERENCES remediation_cards(grade_id)")
    op.create_index("ix_assessment_jobs_remediation_id", "assessment_jobs", ["remediation_id"])


def downgrade():
    op.drop_index("ix_assessment_jobs_remediation_id", table_name="assessment_jobs")
    op.drop_column("assessment_jobs", "remediation_id")
    op.drop_table("remediation_cards")
    op.drop_column("assessment_plans", "remediation")
